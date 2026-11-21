"""Trace de um pedido completo pelo Orquestrador (Tarefa 8), sem Docker.

Usa LLMFalso, SandboxFalso, Celula, RelogioFalso e os demais dublês de test_loop. O
SandboxFalso não chama o despachante, então este módulo estende o dublê com uma subclasse
que chama uma ferramenta na primeira célula, como o contêiner faria.
"""

import json
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from harness.host.perfil import Perfil
from harness.host.registry import Nivel, Registro
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import Evento, Orcamento, Orquestrador, ResultadoPedido
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ToolCall, ToolResult
from harness.telemetry.trace import TraceJsonl, ler_trace
from tests.orchestrator.test_loop import (
    FINAL_OK,
    PERFIL,
    RESPOSTA_FINAL,
    Celula,
    RelogioFalso,
    SandboxFalso,
)

CODIGO_BUSCA = "```python\nresultado = tools.catalogo.buscar(termo='ação')\nprint(resultado)\n```"
DADOS_BUSCA = ["vendas/pedidos.csv", "vendas/ação.csv"]
# Quanto tempo a ferramenta "gasta" no relógio falso: vira latencia_ms=250.
TEMPO_DA_FERRAMENTA_S = 0.25
SEQUENCIA_ESPERADA = [
    "pedido_iniciado",
    "turno_modelo",
    "verificacao_estatica",
    "chamada_ferramenta",
    "execucao",
    "observacao",
    "turno_modelo",
    "verificacao_estatica",
    "execucao",
    "observacao",
    "pedido_finalizado",
]


class _Busca(BaseModel):
    termo: str


class SandboxQueChamaFerramenta(SandboxFalso):
    """SandboxFalso que chama o despachante uma vez por célula, com a chamada indicada."""

    def __init__(self, roteiro: list[Celula], chamadas: dict[int, ToolCall]) -> None:
        super().__init__(roteiro)
        self._chamadas = chamadas

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        if despachante is not None and cell_id in self._chamadas:
            despachante(self._chamadas[cell_id])
        return super().executar_detalhado(cell_id, code, timeout_s, despachante)


def _registro(relogio: RelogioFalso) -> Registro:
    registro = Registro()

    @registro.ferramenta(
        nome="catalogo.buscar",
        nivel=Nivel.LEITURA,
        descricao="Busca arquivos pelo termo.",
        args=_Busca,
    )
    def buscar(args: _Busca, perfil: Perfil) -> list[str]:
        relogio.agora += TEMPO_DA_FERRAMENTA_S
        return list(DADOS_BUSCA)

    return registro


def _orquestrador(
    relogio: RelogioFalso,
    eventos: Callable[[Evento], None] | None,
) -> tuple[Orquestrador, SandboxQueChamaFerramenta]:
    modelo = LLMFalso([CODIGO_BUSCA, FINAL_OK])
    sandbox = SandboxQueChamaFerramenta(
        roteiro=[
            Celula(stdout="[...]\n", ferramentas={"catalogo.buscar": 1}),
            Celula(final=RESPOSTA_FINAL),
        ],
        chamadas={1: ToolCall(call_id="c1", name="catalogo.buscar", args={"termo": "ação"})},
    )
    orquestrador = Orquestrador(
        modelo,
        sandbox,
        _registro(relogio),
        PERFIL.com_concessoes({"catalogo.buscar"}),
        relogio=relogio,
        eventos=eventos,
    )
    return orquestrador, sandbox


def test_pedido_completo_gera_trace_na_ordem_esperada(tmp_path: Path) -> None:
    relogio = RelogioFalso()
    trace = TraceJsonl(tmp_path, session_id="pedido-1")
    orquestrador, _ = _orquestrador(relogio, trace)

    resultado = orquestrador.executar_pedido("Quanto vendi?")

    registros = ler_trace(trace.caminho)
    assert [r["tipo"] for r in registros] == SEQUENCIA_ESPERADA
    assert [r["seq"] for r in registros] == list(range(1, len(SEQUENCIA_ESPERADA) + 1))
    assert {r["session_id"] for r in registros} == {"pedido-1"}
    assert resultado.motivo.value == registros[-1]["dados"]["motivo"]


def test_pedido_finalizado_bate_com_o_resultado_do_pedido(tmp_path: Path) -> None:
    relogio = RelogioFalso()
    trace = TraceJsonl(tmp_path, session_id="s")
    orquestrador, _ = _orquestrador(relogio, trace)

    resultado: ResultadoPedido = orquestrador.executar_pedido("Quanto vendi?")

    final = ler_trace(trace.caminho)[-1]["dados"]
    assert final["motivo"] == resultado.motivo.value
    assert final["completo"] is resultado.completo
    assert final["iteracoes"] == resultado.iteracoes
    assert final["tokens_entrada"] == resultado.tokens_entrada
    assert final["tokens_saida"] == resultado.tokens_saida
    assert final["resposta"] == resultado.resposta
    assert final["fontes"] == list(resultado.fontes)


def test_pedido_iniciado_traz_pedido_e_orcamento(tmp_path: Path) -> None:
    relogio = RelogioFalso()
    trace = TraceJsonl(tmp_path, session_id="s")
    orquestrador, _ = _orquestrador(relogio, trace)

    orquestrador.executar_pedido("Quanto vendi?")

    inicio = ler_trace(trace.caminho)[0]["dados"]
    assert inicio["pedido"] == "Quanto vendi?"
    assert inicio["orcamento"] == Orcamento().model_dump()


def test_verificacao_estatica_traz_iteracao_e_codigo(tmp_path: Path) -> None:
    relogio = RelogioFalso()
    trace = TraceJsonl(tmp_path, session_id="s")
    orquestrador, _ = _orquestrador(relogio, trace)

    orquestrador.executar_pedido("Quanto vendi?")

    verificacoes = [
        r["dados"] for r in ler_trace(trace.caminho) if r["tipo"] == "verificacao_estatica"
    ]
    assert [v["iteracao"] for v in verificacoes] == [1, 2]
    assert (
        verificacoes[0]["codigo"]
        == "resultado = tools.catalogo.buscar(termo='ação')\nprint(resultado)"
    )
    assert verificacoes[0]["problemas"] == []


def test_chamada_ferramenta_traz_nome_args_latencia_e_tamanho(tmp_path: Path) -> None:
    relogio = RelogioFalso()
    trace = TraceJsonl(tmp_path, session_id="s")
    orquestrador, _ = _orquestrador(relogio, trace)

    orquestrador.executar_pedido("Quanto vendi?")

    chamada = next(
        r["dados"] for r in ler_trace(trace.caminho) if r["tipo"] == "chamada_ferramenta"
    )
    assert chamada["nome"] == "catalogo.buscar"
    assert chamada["args"] == {"termo": "ação"}
    assert chamada["ok"] is True
    assert chamada["erro"] is None
    assert chamada["latencia_ms"] == 250
    assert chamada["tamanho_resultado"] == len(json.dumps(DADOS_BUSCA, default=repr))


def test_trace_quebrado_nao_altera_o_resultado_do_pedido(tmp_path: Path) -> None:
    referencia, _ = _orquestrador(RelogioFalso(), None)
    esperado = referencia.executar_pedido("Quanto vendi?")

    # O diretório do trace é um arquivo: toda escrita falha de verdade, sem monkeypatch.
    # (Trocar Path.open globalmente quebraria também a leitura do prompt do sistema.)
    bloqueio = tmp_path / "bloqueio"
    bloqueio.write_text("sou um arquivo", encoding="utf-8")
    trace = TraceJsonl(bloqueio / "traces", session_id="s")
    orquestrador, _ = _orquestrador(RelogioFalso(), trace)

    resultado = orquestrador.executar_pedido("Quanto vendi?")

    assert resultado == esperado
    assert trace.falhas_de_escrita == len(SEQUENCIA_ESPERADA)
