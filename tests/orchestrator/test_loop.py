"""Testes do loop do orquestrador com sandbox falso e LLM falso (Tarefa 7).

Nenhum teste aqui usa Docker: o SandboxFalso devolve células roteirizadas. Os testes com
contêiner real estão em test_loop_docker.py.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.llm.base import LLMFalso, Mensagem, ModeloLLM, RespostaLLM
from harness.orchestrator.loop import (
    Evento,
    MotivoTermino,
    Orcamento,
    Orquestrador,
    ResultadoPedido,
)
from harness.orchestrator.prompt import montar_prompt_sistema
from harness.sandbox.contexto import ValorContexto
from harness.sandbox.docker_sandbox import (
    ResultadoExecucao,
    SandboxMorreu,
    SandboxTimeout,
)
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

PERFIL: Perfil = perfil_por_nome("Vendas")
FINAL_OK = "```python\nFINAL('Resposta pronta', ['vendas/pedidos.csv'])\n```"
RESPOSTA_FINAL = RespostaFinal(answer="Resposta pronta", sources=["vendas/pedidos.csv"])


# --- Dublês ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Celula:
    """Resposta roteirizada de uma célula do sandbox falso."""

    stdout: str = ""
    exception: str | None = None
    final: RespostaFinal | None = None
    ferramentas: dict[str, int] = field(default_factory=dict)
    reiniciado: bool = False


@dataclass(frozen=True)
class Execucao:
    """Registro de uma chamada ao sandbox falso."""

    cell_id: int
    code: str
    timeout_s: float
    com_despachante: bool


class SandboxFalso:
    """Sandbox sem Docker: devolve o roteiro na ordem; sem roteiro, devolve célula vazia."""

    def __init__(self, roteiro: Sequence[Celula | BaseException] = ()) -> None:
        self._roteiro = list(roteiro)
        self.execucoes: list[Execucao] = []
        self.reinicios = 0
        self.fechamentos = 0
        self.falhar_reinicio = False
        self.cargas: list[ValorContexto] = []

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        self.execucoes.append(Execucao(cell_id, code, timeout_s, despachante is not None))
        item: Celula | BaseException = self._roteiro.pop(0) if self._roteiro else Celula()
        if isinstance(item, BaseException):
            raise item
        return ResultadoExecucao(
            done=ExecDone(
                cell_id=cell_id,
                stdout=item.stdout,
                stderr="",
                exception=item.exception,
                duration_ms=1,
                final=item.final,
            ),
            ferramentas=dict(item.ferramentas),
            reiniciado=item.reiniciado,
        )

    def reiniciar(self) -> None:
        self.reinicios += 1
        if self.falhar_reinicio:
            raise RuntimeError("reinício indisponível")

    def carregar_contexto(self, valor: ValorContexto) -> None:
        self.cargas.append(valor)

    def close(self) -> None:
        self.fechamentos += 1


class RelogioFalso:
    """Relógio controlado pelo teste."""

    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


class ModeloComRelogio:
    """LLM falso que gasta `gasto_s` segundos de relógio a cada chamada."""

    def __init__(self, relogio: RelogioFalso, textos: Sequence[str], gasto_s: float) -> None:
        self._relogio = relogio
        self._textos = list(textos)
        self._gasto = gasto_s
        self.chamadas: list[list[Mensagem]] = []

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        self.chamadas.append(list(mensagens))
        self._relogio.agora += self._gasto
        return RespostaLLM(texto=self._textos.pop(0), tokens_entrada=1, tokens_saida=1)


class ModeloQueFalha:
    """LLM que levanta exceção em toda chamada."""

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        raise RuntimeError("serviço do modelo indisponível")


class _SemArgs(BaseModel):
    pass


def _zero() -> float:
    return 0.0


def _registro() -> Registro:
    registro = Registro()
    registro.ferramenta(
        nome="catalogo.listar",
        nivel=Nivel.LEITURA,
        descricao="Lista os arquivos visíveis ao perfil.",
        args=_SemArgs,
    )(lambda args, perfil: [])
    return registro


def _orquestrador(
    modelo: ModeloLLM,
    sandbox: SandboxFalso,
    orcamento: Orcamento | None = None,
    relogio: Callable[[], float] = _zero,
    eventos: Callable[[Evento], None] | None = None,
) -> Orquestrador:
    return Orquestrador(
        modelo,
        sandbox,
        _registro(),
        PERFIL,
        orcamento=orcamento,
        relogio=relogio,
        eventos=eventos,
    )


def _pedido(
    modelo: ModeloLLM,
    sandbox: SandboxFalso,
    orcamento: Orcamento | None = None,
    relogio: Callable[[], float] = _zero,
    eventos: Callable[[Evento], None] | None = None,
) -> ResultadoPedido:
    return _orquestrador(modelo, sandbox, orcamento, relogio, eventos).executar_pedido("Pedido")


def _ultima_mensagem_do_usuario(mensagens: Sequence[Mensagem]) -> str:
    return [m.content for m in mensagens if m.role == "user"][-1]


# --- Caminho feliz e formato ----------------------------------------------------------------


def test_caminho_feliz_com_duas_celulas_e_final() -> None:
    modelo = LLMFalso(
        [
            "Vou listar.\n```python\nprint(tools.catalogo.listar())\n```",
            "Agora termino.\n" + FINAL_OK,
        ]
    )
    sandbox = SandboxFalso(
        [
            Celula(stdout="[]\n", ferramentas={"catalogo.listar": 1}),
            Celula(final=RESPOSTA_FINAL),
        ]
    )
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.completo is True
    assert resultado.resposta == "Resposta pronta"
    assert resultado.fontes == ("vendas/pedidos.csv",)
    assert resultado.iteracoes == 2
    assert [p.cell_id for p in resultado.passos] == [1, 2]
    assert resultado.passos[0].raciocinio == "Vou listar."
    assert resultado.passos[0].ferramentas == {"catalogo.listar": 1}
    assert [e.cell_id for e in sandbox.execucoes] == [1, 2]
    assert all(e.com_despachante for e in sandbox.execucoes)
    assert sandbox.fechamentos == 1


def test_mensagens_ao_modelo_incluem_sistema_com_grupos_e_observacao_delimitada() -> None:
    modelo = LLMFalso(["```python\nprint('oi')\n```", FINAL_OK])
    sandbox = SandboxFalso([Celula(stdout="oi\n")])
    _pedido(modelo, sandbox)

    primeira = modelo.chamadas[0]
    assert primeira[0].role == "system"
    # O prompt lista só o grupo; o nome completo fica para `search_tools` e `describe_tool`.
    assert "- catalogo" in primeira[0].content
    assert "search_tools" in primeira[0].content
    assert primeira[1] == Mensagem(role="user", content="Pedido")

    segunda = modelo.chamadas[1]
    assert segunda[-1].role == "user"
    assert "<saida_nao_confiavel>" in segunda[-1].content
    assert "oi" in segunda[-1].content


def test_contagem_de_tokens_e_somada_e_hash_do_prompt_registrado() -> None:
    modelo = LLMFalso(
        [
            RespostaLLM(texto="```python\nprint(1)\n```", tokens_entrada=10, tokens_saida=5),
            RespostaLLM(texto=FINAL_OK, tokens_entrada=20, tokens_saida=7),
        ]
    )
    sandbox = SandboxFalso([Celula(stdout="1\n"), Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox)

    assert resultado.tokens_entrada == 30
    assert resultado.tokens_saida == 12
    esperado = montar_prompt_sistema(_registro()).hash_sha256
    assert resultado.hash_prompt == esperado


def test_passos_registram_codigo_observacao_e_duracao() -> None:
    modelo = LLMFalso(["Analiso.\n```python\nprint(1)\n```", FINAL_OK])
    sandbox = SandboxFalso([Celula(stdout="1\n"), Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox)

    primeiro = resultado.passos[0]
    assert primeiro.codigo == "print(1)"
    assert primeiro.problemas_estaticos == ()
    assert primeiro.duracao_ms == 1
    assert "[cell 1]" in primeiro.observacao


# --- Erros de formato e verificação ---------------------------------------------------------


def test_excecao_do_codigo_nao_conta_como_invalida() -> None:
    # Com max_invalidos_seguidos=1, qualquer turno inválido encerraria o pedido. Chegar ao
    # FINAL prova que a execução com exceção zerou o contador.
    modelo = LLMFalso(["```python\n1 / 0\n```", FINAL_OK])
    sandbox = SandboxFalso(
        [
            Celula(exception="ZeroDivisionError: division by zero"),
            Celula(final=RESPOSTA_FINAL),
        ]
    )
    resultado = _pedido(modelo, sandbox, Orcamento(max_invalidos_seguidos=1))

    assert resultado.motivo is MotivoTermino.FINAL
    segunda_chamada = modelo.chamadas[1]
    assert "ZeroDivisionError" in _ultima_mensagem_do_usuario(segunda_chamada)


def test_dois_blocos_python_viram_erro_de_formato_sem_executar() -> None:
    modelo = LLMFalso(["```python\na = 1\n```\n```python\nb = 2\n```", FINAL_OK])
    sandbox = SandboxFalso([Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.FINAL
    observacao = _ultima_mensagem_do_usuario(modelo.chamadas[1])
    assert observacao.startswith("[erro de formato]")
    assert "2 blocos" in observacao
    assert resultado.passos[0].cell_id is None
    assert [e.cell_id for e in sandbox.execucoes] == [1]


def test_turno_sem_codigo_orienta_o_modelo_e_conta_como_invalido() -> None:
    modelo = LLMFalso(["Só texto, sem bloco.", FINAL_OK])
    sandbox = SandboxFalso([Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.FINAL
    observacao = _ultima_mensagem_do_usuario(modelo.chamadas[1])
    assert "```python" in observacao
    assert "FINAL" in observacao
    assert resultado.passos[0].codigo is None
    assert [e.cell_id for e in sandbox.execucoes] == [1]


def test_reprovacao_estatica_nao_executa_e_orienta_o_modelo() -> None:
    modelo = LLMFalso(["```python\nimport subprocess\n```", FINAL_OK])
    sandbox = SandboxFalso([Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.FINAL
    assert len(resultado.passos[0].problemas_estaticos) == 1
    assert "reprovado" in _ultima_mensagem_do_usuario(modelo.chamadas[1])
    # Só a célula do FINAL chegou ao sandbox.
    assert [e.code for e in sandbox.execucoes] == [FINAL_OK.split("\n")[1]]


def test_tres_invalidos_seguidos_terminam_com_erro_irrecuperavel() -> None:
    modelo = LLMFalso(["só texto"] * 3)
    sandbox = SandboxFalso()
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.ERRO_IRRECUPERAVEL
    assert resultado.completo is False
    assert resultado.iteracoes == 3
    assert len(modelo.chamadas) == 3
    assert sandbox.execucoes == []
    assert sandbox.fechamentos == 1


def test_execucao_valida_zera_o_contador_de_invalidos() -> None:
    # Com limite 2: inválido, execução válida, inválido, FINAL. Sem o zeramento o segundo
    # inválido já encerraria o pedido.
    modelo = LLMFalso(["só texto", "```python\n1 / 0\n```", "só texto", FINAL_OK])
    sandbox = SandboxFalso(
        [Celula(exception="ZeroDivisionError: division by zero"), Celula(final=RESPOSTA_FINAL)]
    )
    resultado = _pedido(modelo, sandbox, Orcamento(max_invalidos_seguidos=2))

    assert resultado.motivo is MotivoTermino.FINAL


# --- Orçamento ------------------------------------------------------------------------------


def test_orcamento_de_iteracoes_faz_um_turno_final_sem_codigo() -> None:
    modelo = LLMFalso(
        [
            "```python\nprint(1)\n```",
            "```python\nprint(2)\n```",
            "Obtive 1 e 2. Faltou o total. Fontes: nenhuma.",
        ]
    )
    sandbox = SandboxFalso([Celula(stdout="1\n"), Celula(stdout="2\n")])
    resultado = _pedido(modelo, sandbox, Orcamento(max_iteracoes=2))

    assert resultado.motivo is MotivoTermino.ORCAMENTO_ITERACOES
    assert resultado.completo is False
    assert resultado.fontes == ()
    assert resultado.resposta == "Obtive 1 e 2. Faltou o total. Fontes: nenhuma."
    assert resultado.iteracoes == 3
    assert [e.cell_id for e in sandbox.execucoes] == [1, 2]
    pedido_de_resumo = _ultima_mensagem_do_usuario(modelo.chamadas[2])
    assert "resumo" in pedido_de_resumo.lower()


def test_orcamento_de_tokens_encerra_com_motivo_proprio() -> None:
    modelo = LLMFalso(
        [
            RespostaLLM(texto="```python\nprint(1)\n```", tokens_entrada=50, tokens_saida=50),
            RespostaLLM(texto="Resumo do que obtive.", tokens_entrada=1, tokens_saida=1),
        ]
    )
    sandbox = SandboxFalso([Celula(stdout="1\n")])
    resultado = _pedido(modelo, sandbox, Orcamento(max_tokens=100))

    assert resultado.motivo is MotivoTermino.ORCAMENTO_TOKENS
    assert resultado.resposta == "Resumo do que obtive."
    assert resultado.completo is False
    assert len(modelo.chamadas) == 2


def test_orcamento_de_tempo_com_relogio_falso() -> None:
    relogio = RelogioFalso()
    # Cada chamada do modelo gasta 40 s. Limite de 100 s.
    modelo = ModeloComRelogio(
        relogio,
        [
            "```python\nprint(1)\n```",
            "```python\nprint(2)\n```",
            "```python\nprint(3)\n```",
            "Resumo final.",
        ],
        gasto_s=40.0,
    )
    sandbox = SandboxFalso([Celula(stdout="1\n"), Celula(stdout="2\n")])
    resultado = _pedido(
        modelo,
        sandbox,
        Orcamento(max_segundos=100.0, timeout_celula_s=30.0),
        relogio=relogio,
    )

    assert resultado.motivo is MotivoTermino.ORCAMENTO_TEMPO
    assert resultado.completo is False
    # Célula 1 recebe min(30, 60) s; célula 2 recebe min(30, 20) s do tempo que sobrou.
    assert [e.timeout_s for e in sandbox.execucoes] == [30.0, 20.0]
    # A terceira célula não roda: o tempo acabou depois da chamada do modelo.
    assert len(modelo.chamadas) == 4
    assert resultado.resposta == "Resumo final."


def test_resumo_que_falha_devolve_texto_padrao_e_mantem_o_motivo() -> None:
    # Roteiro sem resposta para o resumo: a chamada do resumo levanta RoteiroEsgotado.
    modelo = LLMFalso(["```python\nprint(1)\n```"])
    sandbox = SandboxFalso([Celula(stdout="1\n")])
    resultado = _pedido(modelo, sandbox, Orcamento(max_iteracoes=1))

    assert resultado.motivo is MotivoTermino.ORCAMENTO_ITERACOES
    assert resultado.resposta.startswith("Não foi possível gerar o resumo")
    assert resultado.completo is False


# --- Sandbox: morte, timeout e reinício -----------------------------------------------------


def test_morte_do_sandbox_reinicia_e_avisa_sem_vazar_o_diagnostico() -> None:
    modelo = LLMFalso(["```python\nx = 1\n```", FINAL_OK])
    sandbox = SandboxFalso(
        [
            SandboxMorreu("OOMKilled=True; diagnóstico: segredo do contêiner"),
            Celula(final=RESPOSTA_FINAL),
        ]
    )
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.FINAL
    assert sandbox.reinicios == 1
    aviso = _ultima_mensagem_do_usuario(modelo.chamadas[1])
    assert "reiniciado" in aviso
    assert "variáveis" in aviso
    # O texto cru da exceção (que pode trazer o diagnóstico do contêiner) não chega ao modelo.
    assert "segredo do contêiner" not in aviso


def test_timeout_do_sandbox_reinicia_e_avisa() -> None:
    modelo = LLMFalso(["```python\nwhile True: pass\n```", FINAL_OK])
    sandbox = SandboxFalso([SandboxTimeout("sem resposta"), Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.FINAL
    assert sandbox.reinicios == 1
    aviso = _ultima_mensagem_do_usuario(modelo.chamadas[1])
    assert "tempo limite" in aviso
    assert "reiniciado" in aviso


def test_reinicio_que_falha_termina_com_erro_do_sandbox() -> None:
    modelo = LLMFalso(["```python\nx = 1\n```"])
    sandbox = SandboxFalso([SandboxMorreu("morreu")])
    sandbox.falhar_reinicio = True
    resultado = _pedido(modelo, sandbox)

    assert resultado.motivo is MotivoTermino.ERRO_SANDBOX
    assert resultado.completo is False
    assert sandbox.fechamentos == 1


def test_timeout_da_celula_nunca_passa_do_que_o_orcamento_permite() -> None:
    relogio = RelogioFalso()
    modelo = ModeloComRelogio(
        relogio,
        ["```python\nprint(1)\n```", FINAL_OK, "Resumo."],
        gasto_s=90.0,
    )
    sandbox = SandboxFalso([Celula(stdout="1\n"), Celula(final=RESPOSTA_FINAL)])
    _pedido(modelo, sandbox, Orcamento(max_segundos=100.0), relogio=relogio)

    # Após a 1ª chamada sobram 10 s: a célula recebe 10 s, nunca os 30 s do padrão.
    assert [e.timeout_s for e in sandbox.execucoes] == [10.0]


# --- Erros do LLM e finalização -------------------------------------------------------------


def test_excecao_do_llm_termina_com_erro_llm_sem_propagar() -> None:
    sandbox = SandboxFalso()
    resultado = _orquestrador(ModeloQueFalha(), sandbox).executar_pedido("Pedido")

    assert resultado.motivo is MotivoTermino.ERRO_LLM
    assert resultado.completo is False
    assert sandbox.fechamentos == 1


def test_erro_inesperado_no_loop_ainda_fecha_o_sandbox_exatamente_uma_vez() -> None:
    def relogio_quebrado() -> float:
        raise RuntimeError("relógio quebrado")

    sandbox = SandboxFalso()
    modelo = LLMFalso([FINAL_OK])
    resultado = _pedido(modelo, sandbox, relogio=relogio_quebrado)

    assert resultado.motivo is MotivoTermino.ERRO_IRRECUPERAVEL
    assert sandbox.fechamentos == 1


def test_eventos_saem_na_ordem_esperada() -> None:
    modelo = LLMFalso(["```python\nprint(1)\n```", FINAL_OK])
    sandbox = SandboxFalso([Celula(stdout="1\n"), Celula(final=RESPOSTA_FINAL)])
    recebidos: list[Evento] = []
    _pedido(modelo, sandbox, eventos=recebidos.append)

    assert [e.tipo for e in recebidos] == [
        "pedido_iniciado",
        "turno_modelo",
        "verificacao_estatica",
        "execucao",
        "observacao",
        "turno_modelo",
        "verificacao_estatica",
        "execucao",
        "observacao",
        "pedido_finalizado",
    ]
    assert recebidos[-1].dados["motivo"] == MotivoTermino.FINAL.value


def test_callback_que_levanta_excecao_nao_derruba_o_loop() -> None:
    def callback(evento: Evento) -> None:
        raise RuntimeError("falha no consumidor de eventos")

    modelo = LLMFalso([FINAL_OK])
    sandbox = SandboxFalso([Celula(final=RESPOSTA_FINAL)])
    resultado = _pedido(modelo, sandbox, eventos=callback)

    assert resultado.motivo is MotivoTermino.FINAL
    assert sandbox.fechamentos == 1


def test_turno_modelo_traz_modelo_provedor_e_custo_da_resposta() -> None:
    resposta = RespostaLLM(
        texto=FINAL_OK,
        tokens_entrada=4,
        tokens_saida=2,
        modelo="deepseek/deepseek-v4.1-flash",
        provedor="Novita",
        custo=0.00021,
    )
    modelo = LLMFalso([resposta])
    sandbox = SandboxFalso([Celula(final=RESPOSTA_FINAL)])
    recebidos: list[Evento] = []
    _pedido(modelo, sandbox, eventos=recebidos.append)

    turno = next(e for e in recebidos if e.tipo == "turno_modelo")
    assert turno.dados["modelo"] == "deepseek/deepseek-v4.1-flash"
    assert turno.dados["provedor"] == "Novita"
    assert turno.dados["custo"] == 0.00021


def test_turno_modelo_sem_metadados_registra_none() -> None:
    modelo = LLMFalso([FINAL_OK])
    sandbox = SandboxFalso([Celula(final=RESPOSTA_FINAL)])
    recebidos: list[Evento] = []
    _pedido(modelo, sandbox, eventos=recebidos.append)

    turno = next(e for e in recebidos if e.tipo == "turno_modelo")
    assert (turno.dados["modelo"], turno.dados["provedor"], turno.dados["custo"]) == (
        None,
        None,
        None,
    )


# --- Limite de resultado de ferramenta e evento chamada_ferramenta -------------------------


class SandboxComFerramenta:
    """Sandbox falso que chama uma ferramenta pelo despachante, como o DockerSandbox faz."""

    def __init__(self, chamada: ToolCall) -> None:
        self._chamada = chamada
        self.resultados: list[ToolResult] = []
        self.fechamentos = 0

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        assert despachante is not None
        self.resultados.append(despachante(self._chamada))
        return ResultadoExecucao(
            done=ExecDone(
                cell_id=cell_id,
                stdout="",
                stderr="",
                exception=None,
                duration_ms=1,
                final=RESPOSTA_FINAL,
            ),
            ferramentas={self._chamada.name: 1},
        )

    def reiniciar(self) -> None:
        pass

    def carregar_contexto(self, valor: ValorContexto) -> None:
        pass

    def close(self) -> None:
        self.fechamentos += 1


def test_resultado_acima_do_limite_vira_erro_e_aparece_no_evento_chamada_ferramenta() -> None:
    registro = Registro(limite_resultado_bytes=50)
    registro.ferramenta(
        nome="teste.grande", nivel=Nivel.LEITURA, descricao="devolve muito", args=_SemArgs
    )(lambda args, perfil: "SEGREDO" + "a" * 500)
    sandbox = SandboxComFerramenta(ToolCall(call_id="c1", name="teste.grande", args={}))
    modelo = LLMFalso([FINAL_OK])
    recebidos: list[Evento] = []
    resultado = Orquestrador(
        modelo, sandbox, registro, PERFIL, eventos=recebidos.append
    ).executar_pedido("Pedido")

    # O sandbox recebe ok=False, sem o conteúdo, e o pedido segue normalmente.
    assert sandbox.resultados[0].ok is False
    assert sandbox.resultados[0].error == "resultado da ferramenta excede o limite de 50 bytes"
    assert resultado.motivo is MotivoTermino.FINAL
    assert sandbox.fechamentos == 1

    chamadas = [e for e in recebidos if e.tipo == "chamada_ferramenta"]
    assert len(chamadas) == 1
    evento = chamadas[0].dados
    assert evento["nome"] == "teste.grande"
    assert evento["ok"] is False
    assert evento["erro"] == "resultado da ferramenta excede o limite de 50 bytes"
    assert evento["tamanho_resultado"] == 0
    # O conteúdo não vaza nem pelo evento nem pela trace.
    assert "SEGREDO" not in json.dumps([e.dados for e in recebidos], default=repr)


def test_resultado_dentro_do_limite_gera_evento_com_ok_verdadeiro() -> None:
    registro = Registro(limite_resultado_bytes=50)
    registro.ferramenta(nome="teste.pequeno", nivel=Nivel.LEITURA, descricao="x", args=_SemArgs)(
        lambda args, perfil: ["ok"]
    )
    sandbox = SandboxComFerramenta(ToolCall(call_id="c1", name="teste.pequeno", args={}))
    recebidos: list[Evento] = []
    Orquestrador(
        LLMFalso([FINAL_OK]), sandbox, registro, PERFIL, eventos=recebidos.append
    ).executar_pedido("Pedido")

    evento = next(e for e in recebidos if e.tipo == "chamada_ferramenta").dados
    assert evento["ok"] is True
    assert evento["erro"] is None
    assert evento["tamanho_resultado"] == len('["ok"]')


# --- Propriedades (Hypothesis) --------------------------------------------------------------


class _ModeloCiclico:
    """LLM que repete os textos dados, para nunca faltar resposta."""

    def __init__(self, textos: Sequence[str]) -> None:
        self._textos = list(textos)
        self.chamadas = 0

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        texto = self._textos[self.chamadas % len(self._textos)]
        self.chamadas += 1
        return RespostaLLM(texto=texto, tokens_entrada=1, tokens_saida=1)


@settings(deadline=None, max_examples=60)
@given(
    textos=st.lists(st.text(max_size=300), min_size=1, max_size=5),
    maximo=st.integers(min_value=1, max_value=5),
    invalidos=st.integers(min_value=1, max_value=4),
)
def test_loop_com_textos_arbitrarios_sempre_termina_sem_levantar(
    textos: list[str], maximo: int, invalidos: int
) -> None:
    modelo = _ModeloCiclico(textos)
    sandbox = SandboxFalso()
    orcamento = Orcamento(
        max_iteracoes=maximo,
        max_invalidos_seguidos=invalidos,
        max_tokens=10**9,
    )
    resultado = _pedido(modelo, sandbox, orcamento)

    assert isinstance(resultado.motivo, MotivoTermino)
    assert modelo.chamadas <= maximo + 1
    assert sandbox.fechamentos == 1
