"""Testes do desconto de espera por aprovação no orçamento de tempo."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.llm.base import ChamadaFerramenta, LLMFalso, Mensagem, RespostaLLM
from harness.llm.ferramentas import EsquemaFerramenta
from harness.orchestrator.loop import MotivoTermino, Orcamento, Orquestrador, _Estado
from harness.orchestrator.loop_json import OrquestradorJson
from harness.orchestrator.loop_json import _Estado as _EstadoJson
from harness.sandbox.contexto import ValorContexto
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

FINAL_OK = "```python\nFINAL('Resposta pronta', ['vendas/pedidos.csv'])\n```"
RESPOSTA_FINAL = RespostaFinal(answer="Resposta pronta", sources=["vendas/pedidos.csv"])


def _orquestrador(agora: list[float]) -> Orquestrador:
    return Orquestrador(
        LLMFalso([]),
        object(),  # type: ignore[arg-type]
        Registro(),
        perfil_por_nome("Vendas"),
        orcamento=Orcamento(max_segundos=10.0),
        relogio=lambda: agora[0],
    )


def _orquestrador_json(agora: list[float]) -> OrquestradorJson:
    return OrquestradorJson(
        LLMFalso([]),
        Registro(),
        perfil_por_nome("Vendas"),
        orcamento=Orcamento(max_segundos=10.0),
        relogio=lambda: agora[0],
    )


def test_restante_desconta_a_espera_por_aprovacao() -> None:
    agora = [100.0]
    orquestrador = _orquestrador(agora)
    estado = _Estado()
    estado.inicio = 0.0
    assert orquestrador._restante(estado) == pytest.approx(-90.0)
    estado.espera_s = 95.0
    assert orquestrador._restante(estado) == pytest.approx(5.0)


def test_restante_json_desconta_a_espera_por_aprovacao() -> None:
    agora = [100.0]
    orquestrador = _orquestrador_json(agora)
    estado = _EstadoJson()
    estado.inicio = 0.0
    assert orquestrador._restante(estado) == pytest.approx(-90.0)
    estado.espera_s = 95.0
    assert orquestrador._restante(estado) == pytest.approx(5.0)


@dataclass(frozen=True)
class _CelulaComEspera:
    espera_aprovacao_s: float = 0.0
    final: RespostaFinal | None = None


class _SandboxComEspera:
    """Sandbox falso que avança o relógio durante a execução e devolve espera_aprovacao_s."""

    def __init__(
        self, agora: list[float], roteiro: Sequence[tuple[_CelulaComEspera, float]]
    ) -> None:
        self._agora = agora
        self._roteiro = list(roteiro)

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        celula, duracao_relogio = self._roteiro.pop(0)
        self._agora[0] += duracao_relogio
        return ResultadoExecucao(
            done=ExecDone(
                cell_id=cell_id,
                stdout="",
                stderr="",
                exception=None,
                duration_ms=1,
                final=celula.final,
            ),
            ferramentas={},
            espera_aprovacao_s=celula.espera_aprovacao_s,
        )

    def reiniciar(self) -> None:
        pass

    def carregar_contexto(self, valor: ValorContexto) -> None:
        pass

    def close(self) -> None:
        pass


def test_loop_acumula_espera_aprovacao_e_nao_estoura_orcamento() -> None:
    agora = [0.0]
    sandbox = _SandboxComEspera(
        agora,
        [
            (_CelulaComEspera(espera_aprovacao_s=95.0), 100.0),
            (_CelulaComEspera(espera_aprovacao_s=0.0, final=RESPOSTA_FINAL), 1.0),
        ],
    )
    modelo = LLMFalso(["```python\nx = 1\n```", FINAL_OK])
    orquestrador = Orquestrador(
        modelo,
        sandbox,
        Registro(),
        perfil_por_nome("Vendas"),
        orcamento=Orcamento(max_segundos=10.0),
        relogio=lambda: agora[0],
    )
    resultado = orquestrador.executar_pedido("teste")
    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.completo is True


class _SemArgs(BaseModel):
    pass


class _ModeloJsonComRelogio:
    def __init__(self, respostas: Sequence[RespostaLLM]) -> None:
        self._respostas = list(respostas)

    def responder_com_ferramentas(
        self,
        mensagens: Sequence[Mensagem],
        esquemas: Sequence[EsquemaFerramenta],
        escolha: str = "auto",
    ) -> RespostaLLM:
        return self._respostas.pop(0)


def test_loop_json_acumula_espera_s_e_nao_estoura_orcamento() -> None:
    agora = [0.0]

    class _RegistroComEspera(Registro):
        def despachar(self, chamada: ToolCall, perfil: Perfil) -> ToolResult:
            agora[0] += 100.0
            return ToolResult(call_id=chamada.call_id, ok=True, data="ok", espera_s=95.0)

    reg = _RegistroComEspera()

    @reg.ferramenta(
        nome="catalogo.listar",
        nivel=Nivel.LEITURA,
        descricao="Lista itens.",
        args=_SemArgs,
    )
    def _listar(_args: _SemArgs, _perfil: Perfil) -> str:
        return "ok"

    modelo = _ModeloJsonComRelogio(
        [
            RespostaLLM(
                texto="",
                tokens_entrada=10,
                tokens_saida=5,
                chamadas_ferramenta=(
                    ChamadaFerramenta(id="c1", nome="catalogo__listar", argumentos_brutos="{}"),
                ),
            ),
            RespostaLLM(
                texto="",
                tokens_entrada=10,
                tokens_saida=5,
                chamadas_ferramenta=(
                    ChamadaFerramenta(
                        id="f",
                        nome="finalizar",
                        argumentos_brutos='{"resposta": "ok", "fontes": ["a.csv"]}',
                    ),
                ),
            ),
        ]
    )
    orquestrador = OrquestradorJson(
        modelo,
        reg,
        perfil_por_nome("Vendas"),
        orcamento=Orcamento(max_segundos=10.0),
        relogio=lambda: agora[0],
    )
    resultado = orquestrador.executar_pedido("teste")
    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.completo is True
