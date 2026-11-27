"""Executor da avaliação: roda cada caso ativo N vezes com o agente real e corrige (T16).

Cada rodada cria um registro, um sandbox novo e um Orquestrador, e depois corrige a resposta
com o corretor da T15. Tudo roda em sequência, um contêiner por vez. No fim, `agregar` monta
o relatório.

Decisões deste módulo (não estão na spec):
- `casos` é uma função `FatosVerdadeiros -> casos`, não uma lista. Os casos leem os fatos da
  geração, que só existem depois de `gerar_dados`. Quem chama passa `casos_ativos` (ou um
  filtro dele); os testes passam uma função que monta casos pequenos.
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
- Telemetria (T20a): uma por rodada, criada pela `fabrica_telemetria`. O trace é exportado
  depois da classificação, inclusive para `erro_infra`. Falha dela nunca muda a rodada.
- Falhas de telemetria: somadas por rodada e gravadas no relatório (`falhas_telemetria`), que
  fica None quando não há `fabrica_telemetria`. A leitura é feita depois de fechar a telemetria,
  para contar também o envio do fechamento. Falha ao criar a telemetria conta 1. Quem não tem o
  atributo `falhas` (fábricas de teste) conta 0, e falha ao ler o contador também conta 0.
- Telemetria por variante (Ajuste 1): toda rodada de avaliação, REPL ou JSON, leva a tag
  `variante-<variante>` e o metadado `variante` (vira `langfuse.trace.metadata.variante`). Quando
  o `pedido_iniciado` do agente JSON também traz `variante`, o `TelemetriaOtel` guarda um só
  atributo, e o do executor prevalece (é o mesmo valor). A CLI de pedido único não recebe a tag
  de variante: ela não é rodada de avaliação, então segue com `["cli"]`.
- Pausa (`pausa_s`): espera entre uma rodada executada e a próxima, com o `dormir` injetado de
  `executar_avaliacao`. Não há pausa antes da primeira rodada nem depois da última, e rodada não
  executada (teto, pendente) não conta. A espera fica fora da duração da rodada.

T18a (variantes JSON): `ConfigExecucao.variante` escolhe o agente. Nas variantes JSON não há
sandbox: o registro de arquivos (`ferramentas_json`) e o `OrquestradorJson` são montados por
rodada, e o modelo é chamado com `responder_com_ferramentas`. `ModeloContabil` embrulha também
essa chamada, com a mesma contabilidade. Métricas de contexto, nas duas variantes, saem dos
eventos: `tokens_entrada_por_turno` e `pico_contexto` (de `turno_modelo`) e
`caracteres_observacao` (soma do texto das observações enviadas ao modelo; ver
`_ColetorDeContexto`). Só contam as rodadas que o orquestrador chegou a iniciar.

T19-c (injeção avançada): com `com_ataques_avancados`, o executor grava os fatos do servidor MCP de
teste `contas` na pasta temporária (fora dos dados) e, nos casos de `CASOS_COM_MCP`, sobe o
servidor antes do pedido e o encerra sempre depois, nas duas variantes (REPL e JSON). Falha ao
iniciá-lo é `erro_infra` da rodada. O perfil da rodada ganha só as concessões de leitura do
servidor, e o relatório JSON registra o conjunto de ferramentas com o sufixo `+mcp-contas` e o hash
dos esquemas com as ferramentas do servidor (sem o servidor no ar, o hash fica vazio).

T18c-a (escala): `ConfigExecucao.escala` vai para `gerar_dados`, e o relatório registra `escala`
e `linhas_pedidos`. A escala não muda nada além dos dados: os casos são os mesmos, e os valores
esperados vêm dos fatos da geração com a escala escolhida.
"""

import contextlib
import math
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.evals.casos import Caso, validar_conjunto
from harness.evals.conjunto import CASOS_COM_MCP
from harness.evals.correcao import corrigir_resultado
from harness.evals.dados import ESCALA_MAXIMA, FatosVerdadeiros, gerar_dados
from harness.evals.ferramentas_json import nome_do_conjunto, perfil_json, registro_json
from harness.evals.mcp_avaliacao import (
    SUFIXO_CONJUNTO_MCP,
    config_mcp_de_contas,
    gravar_fatos_de_contas,
    servidor_de_contas,
)
from harness.evals.relatorio import Classificacao, Relatorio, RodadaCaso, agregar
from harness.host.aprovacao import Aprovador, sempre_aprova, sempre_recusa
from harness.host.mcp.config import ConfigMcp
from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.politica import RastreadorDeArquivos
from harness.host.registry import Registro
from harness.host.tools import registrar_todas
from harness.llm.base import Mensagem, ModeloLLM, RespostaLLM
from harness.llm.ferramentas import EsquemaFerramenta, hash_dos_esquemas
from harness.orchestrator.loop import (
    Evento,
    MotivoTermino,
    Orcamento,
    Orquestrador,
    ResultadoPedido,
    SandboxLike,
)
from harness.orchestrator.loop_json import OrquestradorJson, esquemas_do_registro
from harness.sandbox.docker_sandbox import IMAGEM_PADRAO, DockerSandbox
from harness.telemetry.trace import TraceJsonl

_MOTIVOS_DE_INFRA: Final = frozenset({MotivoTermino.ERRO_LLM, MotivoTermino.ERRO_SANDBOX})
VARIANTES: Final = ("repl", "json", "json-agregacao")


class SandboxIniciavel(SandboxLike, Protocol):
    """Sandbox que o executor inicia antes do pedido. DockerSandbox satisfaz este contrato."""

    def start(self) -> None:
        """Cria e inicia o contêiner."""
        ...


class TelemetriaDeRodada(Protocol):
    """O que o executor usa da telemetria de uma rodada. `TelemetriaOtel` satisfaz este contrato.

    Fica como Protocol para que o executor não importe o OpenTelemetry.
    """

    def __call__(self, evento: Evento) -> None:
        """Recebe um evento do orquestrador."""
        ...

    @property
    def falhas(self) -> int:
        """Quantas falhas a telemetria registrou. O executor só lê este número, depois de fechar."""
        ...

    def exportar(
        self,
        metadados: Mapping[str, Any] | None = None,
        *,
        nome: str | None = None,
        tags: Sequence[str] = (),
        sessao: str | None = None,
    ) -> None:
        """Envia o trace acumulado da rodada."""
        ...

    def fechar(self) -> None:
        """Encerra a telemetria da rodada."""
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
    pausa_s: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    # Agente da execução: REPL (Code Mode, com sandbox) ou uma das variantes JSON (T18a).
    variante: Literal["repl", "json", "json-agregacao"] = "repl"
    # Escala dos dados (T18c-a): 1 é o conjunto congelado; de 2 a ESCALA_MAXIMA, mais pedidos.
    escala: int = Field(default=1, ge=1, le=ESCALA_MAXIMA)
    # Gera os arquivos de ataque (ata, planilha de observações, apresentação) para o conjunto
    # `injecao`. Sem isso, os casos de injeção não encontram os arquivos que atacam.
    com_ataques: bool = False
    # Gera os quatro arquivos de ataque disfarçado (T19-b), só eles, para o conjunto
    # `injecao-disfarcada`. Independe de `com_ataques`.
    com_ataques_disfarcados: bool = False
    # Gera os dois arquivos da cadeia em etapas (T19-c) e liga o servidor MCP de teste `contas`
    # nos casos que o usam, para o conjunto `injecao-avancada`. Independe das outras duas.
    com_ataques_avancados: bool = False
    # Liga a política da trifecta (Fase 4): recusa ferramenta com canal externo depois de a
    # sessão ver dado privado e conteúdo não confiável, e liga o rastreio de arquivos no REPL.
    # A execução não é comparável a linhas de base existentes.
    politica_trifecta: bool = False
    # Aprovador fixo para ferramentas que exigem confirmação humana.
    aprovador: Literal["recusar", "aprovar"] | None = None

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
        return self._contar(self._modelo.responder(mensagens))

    def responder_com_ferramentas(
        self,
        mensagens: Sequence[Mensagem],
        ferramentas: Sequence[EsquemaFerramenta],
        *,
        escolha: Literal["auto", "none"] = "auto",
    ) -> RespostaLLM:
        """Mesma contabilidade de `responder`. Exige que o modelo embrulhado aceite ferramentas."""
        modelo: Any = self._modelo
        return self._contar(
            modelo.responder_com_ferramentas(mensagens, ferramentas, escolha=escolha)
        )

    def _contar(self, resposta: RespostaLLM) -> RespostaLLM:
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


def _aprovador_de(escolha: Literal["recusar", "aprovar"] | None) -> Aprovador | None:
    """Converte a opção de linha de comando no aprovador correspondente para o Registro."""
    if escolha == "recusar":
        return sempre_recusa()
    if escolha == "aprovar":
        return sempre_aprova()
    return None


def _fechar(sandbox: SandboxIniciavel) -> None:
    with contextlib.suppress(Exception):
        sandbox.close()


@dataclass
class _ColetorDeContexto:
    """Métricas de contexto de uma rodada, lidas dos eventos (T18a). Não altera nada.

    `tokens_entrada_por_turno` tem a entrada de cada chamada ao modelo que devolveu resposta
    (turnos e o resumo final, nessa ordem). `caracteres_observacao` soma o texto das observações
    que foram ENVIADAS ao modelo: uma observação só conta quando há uma chamada seguinte ao
    modelo. A observação do turno final (o `FINAL`) nunca volta ao modelo, então não conta. Vale
    igual para as duas variantes.
    """

    iniciou: bool = False
    entradas: list[int] = field(default_factory=list)
    caracteres: int = 0
    _pendente: int = 0

    def __call__(self, evento: Evento) -> None:
        if evento.tipo == "pedido_iniciado":
            self.iniciou = True
        elif evento.tipo == "turno_modelo":
            self._enviar_pendente()
            entrada = evento.dados.get("tokens_entrada")
            if isinstance(entrada, int) and not isinstance(entrada, bool):
                self.entradas.append(entrada)
        elif evento.tipo == "erro_llm":
            # A chamada que falhou também levou as observações pendentes.
            self._enviar_pendente()
        elif evento.tipo == "observacao":
            texto = evento.dados.get("texto")
            if isinstance(texto, str):
                self._pendente += len(texto)
        elif evento.tipo == "pedido_finalizado":
            # Quem termina não manda nada de volta ao modelo.
            self._pendente = 0

    def _enviar_pendente(self) -> None:
        self.caracteres += self._pendente
        self._pendente = 0

    def metricas(self) -> dict[str, Any]:
        """Campos de `RodadaCaso`. Sem pedido iniciado, tudo fica vazio (None)."""
        if not self.iniciou:
            return {}
        return {
            "tokens_entrada_por_turno": tuple(self.entradas),
            "pico_contexto": max(self.entradas) if self.entradas else None,
            "caracteres_observacao": self.caracteres,
        }


def _rodada_de_resultado(
    numero: int,
    caso: Caso,
    resultado: ResultadoPedido,
    contabil: ModeloContabil,
    duracao_s: float,
    caminho_trace: str | None,
    coletor: _ColetorDeContexto | None = None,
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
        "modelo": ", ".join(sorted(contabil.modelos)) or None,
        "provedor": ", ".join(sorted(contabil.provedores)) or None,
        **(coletor.metricas() if coletor is not None else {}),
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
        injecao_obedecida=veredito.injecao_obedecida,
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
        modelo=(", ".join(sorted(contabil.modelos)) or None) if contabil else None,
        provedor=(", ".join(sorted(contabil.provedores)) or None) if contabil else None,
    )


def _abrir_telemetria(
    fabrica: Callable[[], TelemetriaDeRodada | None] | None,
) -> tuple[TelemetriaDeRodada | None, int]:
    """Telemetria da rodada e as falhas de criá-la. Falha ao criá-la só desliga a telemetria
    dessa rodada, e conta como uma falha de telemetria."""
    if fabrica is None:
        return None, 0
    try:
        return fabrica(), 0
    except Exception:
        return None, 1


def _falhas_de(telemetria: TelemetriaDeRodada) -> int:
    """Lê o contador de falhas da telemetria. Sem o atributo, ou se a leitura falhar, conta 0."""
    try:
        falhas: object = getattr(telemetria, "falhas", 0)
    except Exception:
        return 0
    if isinstance(falhas, int) and not isinstance(falhas, bool) and falhas > 0:
        return falhas
    return 0


def _consumidor(
    trace: TraceJsonl | None,
    telemetria: TelemetriaDeRodada | None,
    coletor: _ColetorDeContexto | None = None,
) -> Callable[[Evento], None] | None:
    """Um único callback de eventos para o trace JSONL, a telemetria e o coletor de contexto."""
    if trace is None and telemetria is None and coletor is None:
        return None

    def consumir(evento: Evento) -> None:
        if coletor is not None:
            coletor(evento)
        if trace is not None:
            trace(evento)
        if telemetria is not None:
            telemetria(evento)

    return consumir


def _exportar_rodada(
    telemetria: TelemetriaDeRodada | None,
    caso: Caso,
    config: ConfigExecucao,
    rodada: RodadaCaso,
    contabil: ModeloContabil | None,
    caminho_trace: str | None,
) -> int:
    """Exporta o trace da rodada, já classificada, e devolve as falhas de telemetria dela.

    Falha aqui nunca muda a rodada. Sem telemetria, devolve 0.
    """
    if telemetria is None:
        return 0
    veredito = rodada.veredito
    metadados: dict[str, Any] = {
        "caso_id": caso.id,
        "categoria": caso.categoria.value,
        "grupo": caso.grupo,
        "rodada": rodada.numero,
        "semente": config.semente,
        "variante": config.variante,
        "classificacao": rodada.classificacao.value,
        "motivo_termino": rodada.motivo_termino,
        "modelo": (", ".join(sorted(contabil.modelos)) or None) if contabil else None,
        "aprovado": veredito.aprovado if veredito is not None else None,
        "injecao_obedecida": rodada.injecao_obedecida,
        "regras_reprovadas": (
            [regra.descricao for regra in veredito.regras if not regra.passou]
            if veredito is not None
            else []
        ),
        "ataques_obedecidos": (
            [regra.descricao for regra in veredito.regras_de_ataque if not regra.passou]
            if veredito is not None
            else []
        ),
        "detalhe": rodada.detalhe,
    }
    tags = [
        "eval",
        caso.categoria.value,
        f"grupo-{caso.grupo}",
        f"rodada-{rodada.numero}",
        f"variante-{config.variante}",
    ]
    sessao = Path(caminho_trace).stem if caminho_trace else None
    falhou = False
    try:
        try:
            telemetria.exportar(metadados, nome=f"eval/{caso.id}", tags=tags, sessao=sessao)
        finally:
            telemetria.fechar()
    except Exception:
        falhou = True
    # Lido depois de fechar: o envio final do fechamento também conta.
    return _falhas_de(telemetria) + (1 if falhou else 0)


def _executar_rodada(
    caso: Caso,
    numero: int,
    config: ConfigExecucao,
    raiz: Path,
    fabrica_modelo: Callable[[], ModeloLLM],
    fabrica_sandbox: Callable[[dict[str, Path]], SandboxIniciavel],
    relogio: Callable[[], float],
    vistos: _Vistos,
    fabrica_telemetria: Callable[[], TelemetriaDeRodada | None] | None = None,
    config_mcp: ConfigMcp | None = None,
) -> tuple[RodadaCaso, int]:
    """Uma rodada completa. Nunca levanta exceção: falha vira `erro_infra`.

    A telemetria, se houver, recebe os eventos ao lado do trace e exporta o trace no fim, já
    com a classificação decidida. Devolve a rodada e as falhas de telemetria dela.
    """
    inicio = relogio()
    contabil: ModeloContabil | None = None
    caminho_trace: str | None = None
    coletor = _ColetorDeContexto()
    telemetria, falhas_criacao = _abrir_telemetria(fabrica_telemetria)
    try:
        contabil = ModeloContabil(fabrica_modelo())
        perfil = perfil_por_nome(caso.perfil)
        usa_mcp = config_mcp is not None and caso.id in CASOS_COM_MCP
        # O servidor MCP, se houver, é encerrado ao sair do bloco, com ou sem erro.
        with contextlib.ExitStack() as pilha:
            aprovador = _aprovador_de(config.aprovador)
            if config.variante == "repl":
                registro = Registro(politica=config.politica_trifecta, aprovador=aprovador)
                registrar_todas(registro, raiz)
                if usa_mcp and config_mcp is not None:
                    perfil = pilha.enter_context(servidor_de_contas(registro, perfil, config_mcp))
                sandbox = fabrica_sandbox(montagens_do_perfil(perfil, raiz))
                try:
                    sandbox.start()
                    trace = (
                        TraceJsonl(config.pasta_trace) if config.pasta_trace is not None else None
                    )
                    caminho_trace = str(trace.caminho) if trace is not None else None
                    orquestrador = Orquestrador(
                        contabil,
                        sandbox,
                        registro,
                        perfil,
                        orcamento=config.orcamento,
                        relogio=relogio,
                        eventos=_consumidor(trace, telemetria, coletor),
                        rastreador=(
                            RastreadorDeArquivos(raiz, registro.exposicao)
                            if config.politica_trifecta
                            else None
                        ),
                    )
                except BaseException:
                    # Sem Orquestrador, ninguém fecharia o sandbox: fecha aqui e repassa o erro.
                    _fechar(sandbox)
                    raise
                resultado = orquestrador.executar_pedido(caso.pedido)
            else:
                # Variante JSON (T18a): sem sandbox. O registro e o perfil são da variante.
                registro_j = registro_json(
                    raiz,
                    config.variante,
                    politica=config.politica_trifecta,
                    aprovador=aprovador,
                )
                perfil_j = perfil_json(perfil, config.variante)
                if usa_mcp and config_mcp is not None:
                    perfil_j = pilha.enter_context(
                        servidor_de_contas(registro_j, perfil_j, config_mcp)
                    )
                trace = TraceJsonl(config.pasta_trace) if config.pasta_trace is not None else None
                caminho_trace = str(trace.caminho) if trace is not None else None
                orquestrador_json = OrquestradorJson(
                    contabil,
                    registro_j,
                    perfil_j,
                    orcamento=config.orcamento,
                    relogio=relogio,
                    eventos=_consumidor(trace, telemetria, coletor),
                    variante=config.variante,
                )
                resultado = orquestrador_json.executar_pedido(caso.pedido)
    except Exception as erro:
        rodada = _rodada_de_excecao(numero, erro, contabil, relogio() - inicio, caminho_trace)
        falhas = _exportar_rodada(telemetria, caso, config, rodada, contabil, caminho_trace)
        return rodada, falhas_criacao + falhas

    vistos.provedores.update(contabil.provedores)
    vistos.modelos.update(contabil.modelos)
    rodada = _rodada_de_resultado(
        numero, caso, resultado, contabil, relogio() - inicio, caminho_trace, coletor
    )
    falhas = _exportar_rodada(telemetria, caso, config, rodada, contabil, caminho_trace)
    return rodada, falhas_criacao + falhas


def orcamento_usado(config: ConfigExecucao) -> dict[str, float | int]:
    """Limites que valem na execução: os da configuração, ou os padrões do `Orcamento`."""
    orcamento = config.orcamento or Orcamento()
    return {
        "max_iteracoes": orcamento.max_iteracoes,
        "max_tokens": orcamento.max_tokens,
        "max_segundos": orcamento.max_segundos,
        "timeout_celula_s": orcamento.timeout_celula_s,
        "max_invalidos_seguidos": orcamento.max_invalidos_seguidos,
    }


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
    fabrica_telemetria: Callable[[], TelemetriaDeRodada | None] | None = None,
    dormir: Callable[[float], None] = time.sleep,
    linhas_pedidos: int | None = None,
    config_mcp: ConfigMcp | None = None,
) -> Relatorio:
    rodadas: dict[str, list[RodadaCaso]] = {}
    vistos = _Vistos()
    teto_motivo: str | None = None
    custo_acumulado: float | None = None
    tokens_acumulados = 0
    falhas_telemetria = 0
    houve_rodada_executada = False

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
                if config.pausa_s > 0 and houve_rodada_executada:
                    dormir(config.pausa_s)
                houve_rodada_executada = True
                rodada, falhas = _executar_rodada(
                    caso,
                    numero,
                    config,
                    raiz,
                    fabrica_modelo,
                    fabrica_sandbox,
                    relogio,
                    vistos,
                    fabrica_telemetria,
                    config_mcp,
                )
                falhas_telemetria += falhas
                tokens_acumulados += rodada.tokens_entrada + rodada.tokens_saida
                if rodada.custo is not None:
                    custo_acumulado = (custo_acumulado or 0.0) + rodada.custo
            rodadas[caso.id].append(rodada)
            if aoprogresso is not None:
                aoprogresso(caso.id, numero, rodada.classificacao)

    variante_json = config.variante != "repl"
    return agregar(
        casos,
        rodadas,
        semente=config.semente,
        rodadas_por_caso=config.rodadas,
        variante=config.variante,
        modelo=", ".join(sorted(vistos.modelos)) or None,
        provedores=vistos.provedores,
        teto_motivo=teto_motivo,
        falhas_telemetria=falhas_telemetria if fabrica_telemetria is not None else None,
        orcamento=orcamento_usado(config),
        ferramentas_json=(
            nome_do_conjunto(config.variante)
            + (SUFIXO_CONJUNTO_MCP if config_mcp is not None else "")
            if variante_json
            else None
        ),
        hash_ferramentas=(
            _hash_das_ferramentas(config.variante, raiz, config_mcp) if variante_json else None
        ),
        escala=config.escala,
        linhas_pedidos=linhas_pedidos,
        ataques=config.com_ataques
        or config.com_ataques_disfarcados
        or config.com_ataques_avancados,
        politica_trifecta=config.politica_trifecta,
        aprovador=config.aprovador,
    )


def _hash_das_ferramentas(
    variante: str, raiz: Path, config_mcp: ConfigMcp | None = None
) -> str | None:
    """SHA-256 dos esquemas que a variante JSON envia ao modelo. Os perfis concedem o mesmo
    conjunto, então o perfil usado aqui não muda o resultado.

    Com `config_mcp`, o hash inclui as ferramentas do servidor de teste (nome, descrição e
    parâmetros, na ordem do registro), que sobe só para isso. Se ele não subir, devolve None: o
    relatório sai sem hash, e a rodada em si segue o próprio caminho de erro de infraestrutura.
    """
    perfil = perfil_json(perfil_por_nome("Direção"), variante)
    registro = registro_json(raiz, variante)
    if config_mcp is None:
        return hash_dos_esquemas(esquemas_do_registro(registro, perfil))
    try:
        with servidor_de_contas(registro, perfil, config_mcp) as perfil_mcp:
            return hash_dos_esquemas(esquemas_do_registro(registro, perfil_mcp))
    except Exception:
        return None


def executar_avaliacao(
    casos: Callable[[FatosVerdadeiros], Sequence[Caso]],
    fabrica_modelo: Callable[[], ModeloLLM],
    config: ConfigExecucao,
    *,
    fabrica_sandbox: Callable[[dict[str, Path]], SandboxIniciavel] | None = None,
    relogio: Callable[[], float] = time.monotonic,
    aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
    fabrica_telemetria: Callable[[], TelemetriaDeRodada | None] | None = None,
    dormir: Callable[[float], None] = time.sleep,
) -> Relatorio:
    """Gera os dados, roda os casos ativos N vezes e devolve o relatório.

    `fabrica_modelo` é chamada uma vez por rodada. `fabrica_sandbox` recebe as montagens do
    perfil; sem ela, cria um `DockerSandbox` com a imagem da configuração. `aoprogresso` recebe
    (id do caso, número da rodada, classificação) ao fim de cada rodada. `fabrica_telemetria`,
    se dada, é chamada uma vez por rodada para criar a telemetria daquela rodada. `dormir` é a
    espera da `pausa_s` entre rodadas (injetável nos testes).
    """
    with tempfile.TemporaryDirectory(prefix="avaliacao-") as temporaria:
        raiz = Path(temporaria) / "dados"
        fatos = gerar_dados(
            raiz,
            config.semente,
            com_ataques=config.com_ataques,
            com_ataques_disfarcados=config.com_ataques_disfarcados,
            com_ataques_avancados=config.com_ataques_avancados,
            escala=config.escala,
        )
        lista = list(casos(fatos))
        validar_conjunto(lista)
        config_mcp = (
            config_mcp_de_contas(gravar_fatos_de_contas(fatos, Path(temporaria) / "mcp"))
            if config.com_ataques_avancados
            else None
        )

        def criar_docker(montagens: dict[str, Path]) -> SandboxIniciavel:
            return DockerSandbox(
                imagem=config.imagem,
                dados=montagens,
                rastrear_arquivos=config.politica_trifecta,
            )

        fabrica = fabrica_sandbox if fabrica_sandbox is not None else criar_docker
        return _executar_casos(
            lista,
            raiz,
            config,
            fabrica_modelo,
            fabrica,
            relogio,
            aoprogresso,
            fabrica_telemetria,
            dormir,
            linhas_pedidos=fatos.numero_pedidos,
            config_mcp=config_mcp,
        )
