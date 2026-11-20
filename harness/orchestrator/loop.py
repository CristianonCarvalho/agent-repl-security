"""Loop do orquestrador: um pedido do usuário até a resposta final (spec v0, ciclo da requisição).

Fluxo de cada iteração: confere o orçamento; chama o modelo; interpreta o turno; verifica o
código; executa a célula no sandbox; devolve a observação ao modelo. O laço termina pelo
`FINAL`, pelo orçamento (com um último turno de resumo), por erro irrecuperável ou por erro
do LLM ou do sandbox. `executar_pedido` nunca levanta exceção e sempre fecha o sandbox.

Decisões deste módulo (não estão na spec):
- Estado num objeto por pedido (`_Estado`). Assim, um erro inesperado ainda devolve os passos
  já registrados, com motivo ERRO_IRRECUPERAVEL.
- `iteracoes` conta todas as chamadas ao modelo, inclusive as que falharam e o turno de
  resumo. Com `max_iteracoes`=N, o modelo é chamado no máximo N+1 vezes.
- Orçamento é conferido ANTES de cada chamada ao modelo. Se o tempo acabar depois da
  chamada, o código daquele turno não roda: vai direto para o resumo.
- Turno inválido é: erro de formato, turno sem código, ou código reprovado na verificação
  estática. Falha do sandbox (morte, timeout ou erro de protocolo) não conta como inválida,
  porque o código foi aceito e executado. Execução válida, com ou sem exceção, zera o contador.
- Após falha do sandbox, o REPL é reiniciado. Se o reinício falhar, o pedido termina com
  ERRO_SANDBOX. A observação ao modelo traz um motivo genérico, sem o texto cru da exceção,
  porque ele pode conter o diagnóstico do contêiner.
- Turno de resumo: o texto sem código vira a resposta, com `completo=False` e fontes vazias.
  Se o resumo falhar, a resposta é um texto padrão em português.
- Erros (ERRO_LLM, ERRO_SANDBOX, ERRO_IRRECUPERAVEL) não têm resposta: `resposta` fica vazia.
- `close()` do sandbox pode falhar sem derrubar o resultado. Quando falha, o erro é ignorado,
  porque o pedido já tem o seu resultado.
- Um Orquestrador atende um pedido: o sandbox é fechado no fim.
- Contexto (passo 2 da spec): se `executar_pedido` recebe um contexto, ele é carregado no
  sandbox antes do primeiro turno. O prompt de sistema não muda com o contexto (o hash é o
  mesmo); os metadados e a prévia vão na primeira mensagem do usuário, dentro do envelope
  não confiável. Falha ao carregar termina com ERRO_SANDBOX. Depois de um reinício, a
  observação diz que `context` foi restaurado, mas só quando há contexto.
- O evento `pedido_iniciado` traz tipo, tamanho e SHA-256 do contexto, nunca o conteúdo.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.host.perfil import Perfil
from harness.host.registry import Registro
from harness.llm.base import Mensagem, ModeloLLM, RespostaLLM
from harness.orchestrator.estatico import verificar
from harness.orchestrator.observation import envolver_nao_confiavel, formatar_observacao
from harness.orchestrator.prompt import montar_prompt_sistema
from harness.orchestrator.turn import TurnParseError, parse_turn
from harness.sandbox.contexto import (
    LIMITE_PREVIA,
    DescricaoContexto,
    ValorContexto,
    descrever_contexto,
    sha256_contexto,
)
from harness.sandbox.docker_sandbox import (
    ResultadoExecucao,
    SandboxErro,
    SandboxMorreu,
    SandboxTimeout,
)
from harness.sandbox.protocol import ToolCall, ToolResult

MENSAGEM_SEM_CODIGO: Final = (
    "[sem código] Este turno não tem bloco ```python, então nada foi executado. Envie um "
    "bloco ```python com o código a executar ou, para terminar, chame "
    "FINAL(resposta, fontes) dentro de um bloco ```python. Texto fora de FINAL não é "
    "resposta final."
)
MENSAGEM_TEMPO_ESGOTADO: Final = "[orçamento de tempo esgotado; a célula não foi executada]"
TEXTO_RESUMO_PADRAO: Final = (
    "Não foi possível gerar o resumo do que foi obtido antes do término do orçamento."
)
_INSTRUCAO_RESUMO: Final = (
    "Faça um resumo, em português e em texto simples: o que você já obteve, com as fontes "
    "usadas, e o que faltou para responder ao pedido. Não escreva bloco de código nem chame "
    "ferramentas."
)
_UNIDADE_TAMANHO: Final = {"texto": "caracteres", "objeto": "chaves de topo", "lista": "itens"}
_AVISO_REINICIO_COM_CONTEXTO: Final = (
    "o REPL foi reiniciado nesta célula. `context` foi restaurado a partir do pedido; as demais "
    "variáveis definidas antes foram perdidas."
)


class MotivoTermino(Enum):
    """Por que o pedido terminou."""

    FINAL = "final"
    ORCAMENTO_ITERACOES = "orcamento_iteracoes"
    ORCAMENTO_TOKENS = "orcamento_tokens"
    ORCAMENTO_TEMPO = "orcamento_tempo"
    ERRO_IRRECUPERAVEL = "erro_irrecuperavel"
    ERRO_LLM = "erro_llm"
    ERRO_SANDBOX = "erro_sandbox"


# Frase que abre o pedido de resumo, por motivo de orçamento.
_PEDIDO_RESUMO: Final = {
    MotivoTermino.ORCAMENTO_ITERACOES: "Você chegou ao limite de iterações deste pedido.",
    MotivoTermino.ORCAMENTO_TOKENS: "Você chegou ao limite de tokens deste pedido.",
    MotivoTermino.ORCAMENTO_TEMPO: "Você chegou ao limite de tempo deste pedido.",
}


class Orcamento(BaseModel):
    """Limites de um pedido (spec v0, "Limites iniciais"). Valores fora da faixa falham."""

    model_config = ConfigDict(frozen=True)

    max_iteracoes: int = Field(default=15, ge=1)
    max_tokens: int = Field(default=200_000, ge=1)
    max_segundos: float = Field(default=300.0, gt=0)
    timeout_celula_s: float = Field(default=30.0, gt=0)
    max_invalidos_seguidos: int = Field(default=3, ge=1)


class SandboxLike(Protocol):
    """O que o loop precisa de um sandbox. DockerSandbox satisfaz este contrato."""

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        """Executa uma célula e devolve o resultado com a contagem de ferramentas."""
        ...

    def reiniciar(self) -> None:
        """Recria o REPL. As variáveis se perdem, exceto `context`, que é restaurado."""
        ...

    def carregar_contexto(self, valor: ValorContexto) -> None:
        """Envia o contexto do pedido ao REPL, como `context`. Falha levanta exceção."""
        ...

    def close(self) -> None:
        """Destrói o sandbox."""
        ...


@dataclass(frozen=True)
class Evento:
    """Marco do pedido, para a trace (T8). Quem consome decide o que gravar."""

    tipo: str
    dados: dict[str, Any]


class Passo(BaseModel):
    """Uma iteração do pedido, como vai para a trace.

    `cell_id` é None quando o turno não chegou a executar código (erro de formato, sem código,
    reprovação estática e resumo final). Em erro de formato, `raciocinio` traz o texto cru.
    """

    model_config = ConfigDict(frozen=True)

    cell_id: int | None
    raciocinio: str
    codigo: str | None
    problemas_estaticos: tuple[str, ...] = ()
    observacao: str
    ferramentas: dict[str, int] = Field(default_factory=dict)
    duracao_ms: int | None = None


class ResultadoPedido(BaseModel):
    """Resultado final de um pedido. Só `FINAL` produz `completo=True`."""

    model_config = ConfigDict(frozen=True)

    resposta: str
    fontes: tuple[str, ...]
    motivo: MotivoTermino
    completo: bool
    iteracoes: int
    tokens_entrada: int
    tokens_saida: int
    hash_prompt: str
    passos: tuple[Passo, ...]

    @model_validator(mode="after")
    def _completo_so_com_final(self) -> "ResultadoPedido":
        if self.completo != (self.motivo is MotivoTermino.FINAL):
            raise ValueError("completo só é verdadeiro quando o motivo é FINAL")
        return self


@dataclass
class _Estado:
    """Tudo o que um pedido acumula. Fica fora da pilha de chamadas para sobreviver a erros."""

    inicio: float = 0.0
    mensagens: list[Mensagem] = field(default_factory=list)
    hash_prompt: str = ""
    nomes_ferramentas: frozenset[str] = frozenset()
    despachante: Callable[[ToolCall], ToolResult] | None = None
    iteracoes: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    invalidos_seguidos: int = 0
    ultima_celula: int = 0
    passos: list[Passo] = field(default_factory=list)
    # Verdadeiro depois de o contexto ser carregado com sucesso (afeta a observação de reinício).
    com_contexto: bool = False


class Orquestrador:
    """Executa um pedido: LLM, verificação estática, sandbox e orçamento."""

    def __init__(
        self,
        modelo: ModeloLLM,
        sandbox: SandboxLike,
        registro: Registro,
        perfil: Perfil,
        orcamento: Orcamento | None = None,
        relogio: Callable[[], float] = time.monotonic,
        eventos: Callable[[Evento], None] | None = None,
    ) -> None:
        self._modelo = modelo
        self._sandbox = sandbox
        self._registro = registro
        self._perfil = perfil
        self._orcamento = orcamento if orcamento is not None else Orcamento()
        self._relogio = relogio
        self._eventos = eventos

    def executar_pedido(
        self, pedido: str, contexto: ValorContexto | None = None
    ) -> ResultadoPedido:
        """Responde ao pedido. Nunca levanta exceção; sempre fecha o sandbox no fim.

        `contexto`, se dado, é carregado no REPL como `context` antes do primeiro turno.
        """
        estado = _Estado()
        try:
            return self._laco(pedido, estado, contexto)
        except Exception:
            # Defesa em profundidade: um erro interno vira motivo, nunca exceção.
            return self._encerrar(estado, MotivoTermino.ERRO_IRRECUPERAVEL, "", ())
        finally:
            self._fechar_sandbox()

    # --- laço -----------------------------------------------------------------------------

    def _laco(
        self, pedido: str, estado: _Estado, contexto: ValorContexto | None
    ) -> ResultadoPedido:
        estado.inicio = self._relogio()
        prompt = montar_prompt_sistema(self._registro)
        estado.hash_prompt = prompt.hash_sha256
        descricao = descrever_contexto(contexto) if contexto is not None else None
        conteudo = pedido if descricao is None else _mensagem_do_pedido(pedido, descricao)
        estado.mensagens = [
            Mensagem(role="system", content=prompt.texto),
            Mensagem(role="user", content=conteudo),
        ]
        estado.nomes_ferramentas = frozenset(f.nome for f in self._registro.listar())
        despachante = self._registro.despachante(self._perfil)
        estado.despachante = self._instrumentar(despachante) if despachante is not None else None
        self._emitir(
            "pedido_iniciado",
            hash_prompt=prompt.hash_sha256,
            perfil=self._perfil.nome,
            pedido=pedido,
            orcamento=self._orcamento.model_dump(),
            contexto=_metadados_do_contexto(contexto, descricao),
        )
        if contexto is not None:
            try:
                self._sandbox.carregar_contexto(contexto)
            except Exception:
                # Detalhe da falha fica de fora: pode trazer o diagnóstico do contêiner.
                return self._encerrar(estado, MotivoTermino.ERRO_SANDBOX, "", ())
            estado.com_contexto = True

        while True:
            motivo = self._motivo_orcamento(estado)
            if motivo is not None:
                return self._resumo_final(estado, motivo)
            encerrado = self._turno(estado)
            if encerrado is not None:
                return encerrado

    def _turno(self, estado: _Estado) -> ResultadoPedido | None:
        """Uma iteração completa. Devolve o resultado final se o pedido termina aqui."""
        estado.iteracoes += 1
        try:
            resposta = self._modelo.responder(list(estado.mensagens))
        except Exception:
            # Detalhe da exceção fica de fora: pode conter credenciais ou caminhos.
            return self._encerrar(estado, MotivoTermino.ERRO_LLM, "", ())
        self._registrar_resposta(estado, resposta, resumo=False)
        estado.mensagens.append(Mensagem(role="assistant", content=resposta.texto))

        try:
            turno = parse_turn(resposta.texto)
        except TurnParseError as erro:
            return self._invalido(estado, resposta.texto, None, f"[erro de formato] {erro}")
        codigo = turno.code
        if codigo is None:
            return self._invalido(estado, turno.reasoning, None, MENSAGEM_SEM_CODIGO)

        problemas = verificar(codigo, estado.nomes_ferramentas)
        self._emitir(
            "verificacao_estatica",
            iteracao=estado.iteracoes,
            codigo=codigo,
            problemas=list(problemas),
        )
        if problemas:
            observacao = (
                "[código reprovado na verificação estática; nada foi executado]\n"
                + "\n".join(f"- {problema}" for problema in problemas)
            )
            return self._invalido(estado, turno.reasoning, codigo, observacao, tuple(problemas))

        restante = self._restante(estado)
        if restante <= 0:
            estado.passos.append(
                Passo(
                    cell_id=None,
                    raciocinio=turno.reasoning,
                    codigo=codigo,
                    observacao=MENSAGEM_TEMPO_ESGOTADO,
                )
            )
            return self._resumo_final(estado, MotivoTermino.ORCAMENTO_TEMPO)
        return self._executar_celula(estado, turno.reasoning, codigo, restante)

    def _executar_celula(
        self, estado: _Estado, raciocinio: str, codigo: str, restante: float
    ) -> ResultadoPedido | None:
        estado.ultima_celula += 1
        cell_id = estado.ultima_celula
        timeout = min(self._orcamento.timeout_celula_s, restante)
        try:
            execucao = self._sandbox.executar_detalhado(
                cell_id, codigo, timeout_s=timeout, despachante=estado.despachante
            )
        except SandboxErro as erro:
            return self._recuperar_sandbox(estado, cell_id, raciocinio, codigo, erro)
        except Exception:
            estado.passos.append(
                Passo(
                    cell_id=cell_id,
                    raciocinio=raciocinio,
                    codigo=codigo,
                    observacao="[falha inesperada do sandbox]",
                )
            )
            return self._encerrar(estado, MotivoTermino.ERRO_SANDBOX, "", ())

        done = execucao.done
        observacao = formatar_observacao(cell_id, done, execucao.ferramentas)
        if execucao.reiniciado and estado.com_contexto:
            observacao = f"[cell {cell_id}] {_AVISO_REINICIO_COM_CONTEXTO}\n" + observacao
        # Execução válida (mesmo com exceção do código do modelo) zera o contador de inválidos.
        estado.invalidos_seguidos = 0
        estado.passos.append(
            Passo(
                cell_id=cell_id,
                raciocinio=raciocinio,
                codigo=codigo,
                observacao=observacao,
                ferramentas=dict(execucao.ferramentas),
                duracao_ms=done.duration_ms,
            )
        )
        estado.mensagens.append(Mensagem(role="user", content=observacao))
        self._emitir(
            "execucao",
            cell_id=cell_id,
            duracao_ms=done.duration_ms,
            ferramentas=dict(execucao.ferramentas),
            excecao=done.exception,
            final=done.final is not None,
        )
        self._emitir("observacao", cell_id=cell_id, texto=observacao)
        if done.final is not None:
            return self._encerrar(
                estado, MotivoTermino.FINAL, done.final.answer, tuple(done.final.sources)
            )
        return None

    def _recuperar_sandbox(
        self,
        estado: _Estado,
        cell_id: int,
        raciocinio: str,
        codigo: str,
        erro: SandboxErro,
    ) -> ResultadoPedido | None:
        """Reinicia o REPL depois de morte ou timeout do sandbox e avisa o modelo."""
        motivo = _motivo_da_falha(erro)
        try:
            self._sandbox.reiniciar()
        except Exception:
            estado.passos.append(
                Passo(
                    cell_id=cell_id,
                    raciocinio=raciocinio,
                    codigo=codigo,
                    observacao="[falha ao reiniciar o REPL]",
                )
            )
            return self._encerrar(estado, MotivoTermino.ERRO_SANDBOX, "", ())

        if estado.com_contexto:
            observacao = (
                f"[cell {cell_id}] o REPL foi reiniciado porque {motivo}. `context` foi "
                "restaurado a partir do pedido; as demais variáveis definidas antes foram "
                "perdidas. As próximas células começam com o namespace limpo, exceto por `context`."
            )
        else:
            observacao = (
                f"[cell {cell_id}] o REPL foi reiniciado porque {motivo}. As variáveis definidas "
                "antes foram perdidas; as próximas células começam com o namespace limpo."
            )
        estado.invalidos_seguidos = 0
        estado.passos.append(
            Passo(cell_id=cell_id, raciocinio=raciocinio, codigo=codigo, observacao=observacao)
        )
        estado.mensagens.append(Mensagem(role="user", content=observacao))
        self._emitir("execucao", cell_id=cell_id, reiniciado=True, motivo=motivo)
        self._emitir("observacao", cell_id=cell_id, texto=observacao)
        return None

    def _invalido(
        self,
        estado: _Estado,
        raciocinio: str,
        codigo: str | None,
        observacao: str,
        problemas: tuple[str, ...] = (),
    ) -> ResultadoPedido | None:
        """Turno que não executou: registra, avisa o modelo e confere o limite de inválidos."""
        estado.invalidos_seguidos += 1
        estado.passos.append(
            Passo(
                cell_id=None,
                raciocinio=raciocinio,
                codigo=codigo,
                problemas_estaticos=problemas,
                observacao=observacao,
            )
        )
        estado.mensagens.append(Mensagem(role="user", content=observacao))
        self._emitir("observacao", cell_id=None, texto=observacao)
        if estado.invalidos_seguidos >= self._orcamento.max_invalidos_seguidos:
            return self._encerrar(estado, MotivoTermino.ERRO_IRRECUPERAVEL, "", ())
        return None

    def _resumo_final(self, estado: _Estado, termino: MotivoTermino) -> ResultadoPedido:
        """Último turno, sem código, pedindo o resumo do que foi obtido e do que faltou.

        `termino` é um dos motivos de orçamento. Qualquer falha aqui vira o texto padrão.
        """
        estado.iteracoes += 1
        instrucao = f"{_PEDIDO_RESUMO[termino]} {_INSTRUCAO_RESUMO}"
        mensagens = [*estado.mensagens, Mensagem(role="user", content=instrucao)]
        try:
            resposta = self._modelo.responder(mensagens)
        except Exception:
            estado.passos.append(
                Passo(
                    cell_id=None,
                    raciocinio="",
                    codigo=None,
                    observacao="[falha ao gerar o resumo]",
                )
            )
            return self._encerrar(estado, termino, TEXTO_RESUMO_PADRAO, ())

        self._registrar_resposta(estado, resposta, resumo=True)
        texto = resposta.texto.strip()
        try:
            # Se o modelo ainda mandar bloco de código, ele não é executado; fica só o texto.
            texto = parse_turn(resposta.texto).reasoning
        except TurnParseError:
            pass
        estado.passos.append(
            Passo(
                cell_id=None,
                raciocinio=resposta.texto.strip(),
                codigo=None,
                observacao="[resumo final do orçamento]",
            )
        )
        return self._encerrar(estado, termino, texto or TEXTO_RESUMO_PADRAO, ())

    # --- orçamento ------------------------------------------------------------------------

    def _motivo_orcamento(self, estado: _Estado) -> MotivoTermino | None:
        """Primeiro limite esgotado, se algum, antes de uma nova chamada ao modelo."""
        if estado.iteracoes >= self._orcamento.max_iteracoes:
            return MotivoTermino.ORCAMENTO_ITERACOES
        if estado.tokens_entrada + estado.tokens_saida >= self._orcamento.max_tokens:
            return MotivoTermino.ORCAMENTO_TOKENS
        if self._restante(estado) <= 0:
            return MotivoTermino.ORCAMENTO_TEMPO
        return None

    def _restante(self, estado: _Estado) -> float:
        """Segundos que ainda restam ao pedido."""
        return self._orcamento.max_segundos - (self._relogio() - estado.inicio)

    # --- saída ----------------------------------------------------------------------------

    def _registrar_resposta(self, estado: _Estado, resposta: RespostaLLM, resumo: bool) -> None:
        estado.tokens_entrada += resposta.tokens_entrada
        estado.tokens_saida += resposta.tokens_saida
        self._emitir(
            "turno_modelo",
            iteracao=estado.iteracoes,
            resumo=resumo,
            texto=resposta.texto,
            tokens_entrada=resposta.tokens_entrada,
            tokens_saida=resposta.tokens_saida,
            modelo=resposta.modelo,
            provedor=resposta.provedor,
            custo=resposta.custo,
        )

    def _encerrar(
        self,
        estado: _Estado,
        motivo: MotivoTermino,
        resposta: str,
        fontes: tuple[str, ...],
    ) -> ResultadoPedido:
        completo = motivo is MotivoTermino.FINAL
        resultado = ResultadoPedido(
            resposta=resposta,
            fontes=fontes,
            motivo=motivo,
            completo=completo,
            iteracoes=estado.iteracoes,
            tokens_entrada=estado.tokens_entrada,
            tokens_saida=estado.tokens_saida,
            hash_prompt=estado.hash_prompt,
            passos=tuple(estado.passos),
        )
        self._emitir(
            "pedido_finalizado",
            motivo=motivo.value,
            completo=completo,
            iteracoes=estado.iteracoes,
            tokens_entrada=estado.tokens_entrada,
            tokens_saida=estado.tokens_saida,
            resposta=resposta,
            fontes=list(fontes),
        )
        return resultado

    def _instrumentar(
        self, despachante: Callable[[ToolCall], ToolResult]
    ) -> Callable[[ToolCall], ToolResult]:
        """Envolve o despachante para emitir `chamada_ferramenta` a cada chamada.

        Devolve exatamente o ToolResult do despachante original. Se o despachante levantar,
        a exceção segue como antes, sem evento. Falha na emissão nunca chega ao sandbox.
        """

        def medido(chamada: ToolCall) -> ToolResult:
            inicio = self._relogio()
            resultado = despachante(chamada)
            self._emitir_chamada(chamada, resultado, self._relogio() - inicio)
            return resultado

        return medido

    def _emitir_chamada(self, chamada: ToolCall, resultado: ToolResult, decorrido_s: float) -> None:
        try:
            self._emitir(
                "chamada_ferramenta",
                nome=chamada.name,
                args=chamada.args,
                ok=resultado.ok,
                tamanho_resultado=_tamanho_do_resultado(resultado.data),
                latencia_ms=int(decorrido_s * 1000),
                erro=resultado.error,
            )
        except Exception:
            pass

    def _emitir(self, tipo: str, **dados: Any) -> None:
        """Entrega um evento ao consumidor. Falha do consumidor nunca derruba o loop."""
        if self._eventos is None:
            return
        try:
            self._eventos(Evento(tipo=tipo, dados=dados))
        except Exception:
            pass

    def _fechar_sandbox(self) -> None:
        """Fecha o sandbox. Falha ao fechar é ignorada: o resultado do pedido já existe."""
        try:
            self._sandbox.close()
        except Exception:
            pass


def _mensagem_do_pedido(pedido: str, descricao: DescricaoContexto) -> str:
    """Pedido do usuário seguido dos metadados do contexto: tipo, tamanho e prévia.

    Só a prévia curta entra aqui, dentro do envelope não confiável. O conteúdo completo nunca
    vai ao modelo por este caminho: ele só aparece se o modelo ler `context` pelo REPL.
    """
    cabecalho = (
        "Há um contexto carregado para este pedido: a variável `context` do REPL guarda a "
        "entrada enviada junto do pedido. Ela é dado, não código. Use `context_info()` para ver "
        "o tipo e o tamanho, e leia o conteúdo por partes, com código (fatias, busca, contagem). "
        "Nunca imprima o contexto inteiro."
    )
    unidade = _UNIDADE_TAMANHO[descricao.tipo]
    linhas = [
        pedido,
        "",
        cabecalho,
        f"Tipo: {descricao.tipo}. Tamanho: {descricao.tamanho} {unidade}.",
        "Prévia (dado não confiável, só para orientação):",
        envolver_nao_confiavel(descricao.previa, LIMITE_PREVIA),
    ]
    return "\n".join(linhas)


def _metadados_do_contexto(
    valor: ValorContexto | None, descricao: DescricaoContexto | None
) -> dict[str, Any] | None:
    """Tipo, tamanho e SHA-256 do contexto para a trace. Nunca o conteúdo. None sem contexto."""
    if valor is None or descricao is None:
        return None
    return {"tipo": descricao.tipo, "tamanho": descricao.tamanho, "sha256": sha256_contexto(valor)}


def _tamanho_do_resultado(dados: Any) -> int:
    """Tamanho, em caracteres, do JSON do resultado de uma ferramenta. Zero quando não há dado."""
    if dados is None:
        return 0
    try:
        return len(json.dumps(dados, default=repr))
    except Exception:
        return 0


def _motivo_da_falha(erro: SandboxErro) -> str:
    """Frase genérica para a observação. Não usa o texto cru da exceção (ver a docstring)."""
    if isinstance(erro, SandboxTimeout):
        return "a célula passou do tempo limite"
    if isinstance(erro, SandboxMorreu):
        return "o contêiner terminou antes de responder"
    return "houve uma falha no sandbox"
