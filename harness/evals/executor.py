"""Executor da avaliação: roda cada caso ativo N vezes com o agente real e corrige (T16).

Cada rodada cria um registro, um sandbox novo e um Orquestrador, e depois corrige a resposta
com o corretor da T15. Tudo roda em sequência, um contêiner por vez. No fim, `agregar` monta
o relatório.

Decisões deste módulo (não estão na spec):
- `casos` é uma função `FatosVerdadeiros -> casos`, não uma lista. Os casos de exemplo leem os
  fatos da geração, que só existem depois de `gerar_dados`. Quem chama passa
  `casos_de_exemplo` (ou um filtro dele); os testes passam uma função que monta casos pequenos.
- Os dados são gerados numa pasta temporária, que some no fim, com ou sem erro.
- Tetos: antes de cada rodada, confere-se o acumulado das rodadas já feitas. Custo conta só
  os valores conhecidos. Ao estourar, esta rodada e todas as seguintes viram `nao_executada`.
  Casos pendentes não geram rodadas.
- Classificação: `FINAL` com veredito aprovado é `aprovada`; `FINAL` reprovado e os orçamentos
  e o erro irrecuperável são `reprovada`; `ERRO_LLM` e `ERRO_SANDBOX` são `erro_infra`. Falha
  ao criar, iniciar ou montar a rodada também é `erro_infra`, com o nome do tipo da exceção
  como detalhe. A mensagem da exceção nunca entra no relatório, exceto a de `ErroLLM`, que o
  cliente do OpenRouter já restringe a tipo e status. Em ERRO_LLM o detalhe fica
  `erro_llm: <detalhe_erro>`.
- `ModeloContabil` embrulha o modelo de cada rodada. Ele repassa a resposta intacta e não
  inventa custo: sem custo informado, o custo fica None.
- O relógio injetado vale para a duração da rodada e para o orçamento de tempo do orquestrador.
"""

import contextlib
import math
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.evals.casos import Caso, validar_conjunto
from harness.evals.correcao import corrigir_resultado
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.relatorio import Classificacao, Relatorio, RodadaCaso, agregar
from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.registry import Registro
from harness.host.tools import registrar_todas
from harness.llm.base import Mensagem, ModeloLLM, RespostaLLM
from harness.orchestrator.loop import (
    MotivoTermino,
    Orcamento,
    Orquestrador,
    ResultadoPedido,
    SandboxLike,
)
from harness.sandbox.docker_sandbox import IMAGEM_PADRAO, DockerSandbox
from harness.telemetry.trace import TraceJsonl

_MOTIVOS_DE_INFRA: Final = frozenset({MotivoTermino.ERRO_LLM, MotivoTermino.ERRO_SANDBOX})


class SandboxIniciavel(SandboxLike, Protocol):
    """Sandbox que o executor inicia antes do pedido. DockerSandbox satisfaz este contrato."""

    def start(self) -> None:
        """Cria e inicia o contêiner."""
        ...


class ConfigExecucao(BaseModel):
    """Parâmetros de uma execução de avaliação. Valores fora da faixa falham."""

    model_config = ConfigDict(frozen=True)

    rodadas: int = Field(default=3, ge=1)
    semente: int = 42
    teto_usd: float | None = 2.0
    teto_tokens: int | None = 2_000_000
    imagem: str = IMAGEM_PADRAO
    pasta_trace: Path | None = None
    orcamento: Orcamento | None = None

    @field_validator("teto_usd")
    @classmethod
    def _teto_usd_valido(cls, valor: float | None) -> float | None:
        if valor is not None and (not math.isfinite(valor) or valor <= 0):
            raise ValueError("teto_usd precisa ser um número positivo e finito")
        return valor

    @field_validator("teto_tokens")
    @classmethod
    def _teto_tokens_valido(cls, valor: int | None) -> int | None:
        if valor is not None and valor < 1:
            raise ValueError("teto_tokens precisa ser pelo menos 1")
        return valor


class ModeloContabil:
    """Embrulha um `ModeloLLM` e soma tokens, custo e provedores das respostas.

    Um contador novo vale para uma rodada. Exceção do modelo passa sem alteração, e a resposta
    é devolvida como chegou.
    """

    def __init__(self, modelo: ModeloLLM) -> None:
        self._modelo = modelo
        self.tokens_entrada = 0
        self.tokens_saida = 0
        self.respostas = 0
        self.respostas_sem_custo = 0
        self._custo = 0.0
        self._custo_conhecido = False
        self._provedores: set[str] = set()
        self._modelos: set[str] = set()

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        resposta = self._modelo.responder(mensagens)
        self.respostas += 1
        self.tokens_entrada += resposta.tokens_entrada
        self.tokens_saida += resposta.tokens_saida
        if resposta.custo is None:
            self.respostas_sem_custo += 1
        else:
            self._custo += resposta.custo
            self._custo_conhecido = True
        if resposta.provedor:
            self._provedores.add(resposta.provedor)
        if resposta.modelo:
            self._modelos.add(resposta.modelo)
        return resposta

    @property
    def custo_total(self) -> float | None:
        """Soma dos custos informados, ou None se nenhuma resposta trouxe custo."""
        return self._custo if self._custo_conhecido else None

    @property
    def provedores(self) -> frozenset[str]:
        return frozenset(self._provedores)

    @property
    def modelos(self) -> frozenset[str]:
        return frozenset(self._modelos)


@dataclass
class _Vistos:
    """Provedores e modelos efetivos vistos em toda a execução."""

    provedores: set[str] = field(default_factory=set)
    modelos: set[str] = field(default_factory=set)


def _fechar(sandbox: SandboxIniciavel) -> None:
    with contextlib.suppress(Exception):
        sandbox.close()


def _rodada_de_resultado(
    numero: int,
    caso: Caso,
    resultado: ResultadoPedido,
    contabil: ModeloContabil,
    duracao_s: float,
    caminho_trace: str | None,
) -> RodadaCaso:
    base: dict[str, Any] = {
        "numero": numero,
        "motivo_termino": resultado.motivo.value,
        "iteracoes": resultado.iteracoes,
        "tokens_entrada": contabil.tokens_entrada,
        "tokens_saida": contabil.tokens_saida,
        "custo": contabil.custo_total,
        "duracao_s": duracao_s,
        "caminho_trace": caminho_trace,
        "hash_prompt": resultado.hash_prompt or None,
    }
    if resultado.motivo in _MOTIVOS_DE_INFRA:
        # Motivo do término. Em ERRO_LLM, o detalhe seguro do orquestrador (tipo e, para
        # ErroLLM, a mensagem que o cliente já restringe a tipo e status) vai junto.
        detalhe = resultado.motivo.value
        if resultado.motivo is MotivoTermino.ERRO_LLM and resultado.detalhe_erro:
            detalhe = f"{detalhe}: {resultado.detalhe_erro}"
        return RodadaCaso(classificacao=Classificacao.ERRO_INFRA, detalhe=detalhe, **base)
    veredito = corrigir_resultado(caso, resultado)
    aprovada = resultado.motivo is MotivoTermino.FINAL and veredito.aprovado
    return RodadaCaso(
        classificacao=Classificacao.APROVADA if aprovada else Classificacao.REPROVADA,
        veredito=veredito,
        **base,
    )


def _rodada_de_excecao(
    numero: int,
    erro: Exception,
    contabil: ModeloContabil | None,
    duracao_s: float,
    caminho_trace: str | None,
) -> RodadaCaso:
    """Falha fora do pedido: só o nome do tipo da exceção entra no relatório."""
    return RodadaCaso(
        numero=numero,
        classificacao=Classificacao.ERRO_INFRA,
        detalhe=type(erro).__name__,
        tokens_entrada=contabil.tokens_entrada if contabil else 0,
        tokens_saida=contabil.tokens_saida if contabil else 0,
        custo=contabil.custo_total if contabil else None,
        duracao_s=duracao_s,
        caminho_trace=caminho_trace,
    )


def _executar_rodada(
    caso: Caso,
    numero: int,
    config: ConfigExecucao,
    raiz: Path,
    fabrica_modelo: Callable[[], ModeloLLM],
    fabrica_sandbox: Callable[[dict[str, Path]], SandboxIniciavel],
    relogio: Callable[[], float],
    vistos: _Vistos,
) -> RodadaCaso:
    """Uma rodada completa. Nunca levanta exceção: falha vira `erro_infra`."""
    inicio = relogio()
    contabil: ModeloContabil | None = None
    caminho_trace: str | None = None
    try:
        contabil = ModeloContabil(fabrica_modelo())
        perfil = perfil_por_nome(caso.perfil)
        registro = Registro()
        registrar_todas(registro, raiz)
        sandbox = fabrica_sandbox(montagens_do_perfil(perfil, raiz))
        try:
            sandbox.start()
            trace = TraceJsonl(config.pasta_trace) if config.pasta_trace is not None else None
            caminho_trace = str(trace.caminho) if trace is not None else None
            orquestrador = Orquestrador(
                contabil,
                sandbox,
                registro,
                perfil,
                orcamento=config.orcamento,
                relogio=relogio,
                eventos=trace,
            )
        except BaseException:
            # Sem Orquestrador, ninguém fecharia o sandbox: fecha aqui e repassa o erro.
            _fechar(sandbox)
            raise
        resultado = orquestrador.executar_pedido(caso.pedido)
    except Exception as erro:
        return _rodada_de_excecao(numero, erro, contabil, relogio() - inicio, caminho_trace)

    vistos.provedores.update(contabil.provedores)
    vistos.modelos.update(contabil.modelos)
    return _rodada_de_resultado(
        numero, caso, resultado, contabil, relogio() - inicio, caminho_trace
    )


def _motivo_de_teto(config: ConfigExecucao, custo: float | None, tokens: int) -> str | None:
    """Motivo do teto estourado pelo acumulado, ou None se ainda cabe."""
    if config.teto_usd is not None and custo is not None and custo > config.teto_usd:
        return f"custo acumulado de US$ {custo:.4f} passou do teto de US$ {config.teto_usd:.2f}"
    if config.teto_tokens is not None and tokens > config.teto_tokens:
        return f"tokens acumulados ({tokens}) passaram do teto de {config.teto_tokens}"
    return None


def _executar_casos(
    casos: Sequence[Caso],
    raiz: Path,
    config: ConfigExecucao,
    fabrica_modelo: Callable[[], ModeloLLM],
    fabrica_sandbox: Callable[[dict[str, Path]], SandboxIniciavel],
    relogio: Callable[[], float],
    aoprogresso: Callable[[str, int, Classificacao], None] | None,
) -> Relatorio:
    rodadas: dict[str, list[RodadaCaso]] = {}
    vistos = _Vistos()
    teto_motivo: str | None = None
    custo_acumulado: float | None = None
    tokens_acumulados = 0

    for caso in casos:
        rodadas[caso.id] = []
        if caso.situacao.pendente:
            continue
        for numero in range(1, config.rodadas + 1):
            if teto_motivo is None:
                teto_motivo = _motivo_de_teto(config, custo_acumulado, tokens_acumulados)
            if teto_motivo is not None:
                rodada = RodadaCaso(
                    numero=numero, classificacao=Classificacao.NAO_EXECUTADA, detalhe=teto_motivo
                )
            else:
                rodada = _executar_rodada(
                    caso, numero, config, raiz, fabrica_modelo, fabrica_sandbox, relogio, vistos
                )
                tokens_acumulados += rodada.tokens_entrada + rodada.tokens_saida
                if rodada.custo is not None:
                    custo_acumulado = (custo_acumulado or 0.0) + rodada.custo
            rodadas[caso.id].append(rodada)
            if aoprogresso is not None:
                aoprogresso(caso.id, numero, rodada.classificacao)

    return agregar(
        casos,
        rodadas,
        semente=config.semente,
        rodadas_por_caso=config.rodadas,
        modelo=", ".join(sorted(vistos.modelos)) or None,
        provedores=vistos.provedores,
        teto_motivo=teto_motivo,
    )


def executar_avaliacao(
    casos: Callable[[FatosVerdadeiros], Sequence[Caso]],
    fabrica_modelo: Callable[[], ModeloLLM],
    config: ConfigExecucao,
    *,
    fabrica_sandbox: Callable[[dict[str, Path]], SandboxIniciavel] | None = None,
    relogio: Callable[[], float] = time.monotonic,
    aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
) -> Relatorio:
    """Gera os dados, roda os casos ativos N vezes e devolve o relatório.

    `fabrica_modelo` é chamada uma vez por rodada. `fabrica_sandbox` recebe as montagens do
    perfil; sem ela, cria um `DockerSandbox` com a imagem da configuração. `aoprogresso` recebe
    (id do caso, número da rodada, classificação) ao fim de cada rodada.
    """
    with tempfile.TemporaryDirectory(prefix="avaliacao-") as temporaria:
        raiz = Path(temporaria) / "dados"
        fatos = gerar_dados(raiz, config.semente)
        lista = list(casos(fatos))
        validar_conjunto(lista)

        def criar_docker(montagens: dict[str, Path]) -> SandboxIniciavel:
            return DockerSandbox(imagem=config.imagem, dados=montagens)

        fabrica = fabrica_sandbox if fabrica_sandbox is not None else criar_docker
        return _executar_casos(lista, raiz, config, fabrica_modelo, fabrica, relogio, aoprogresso)
