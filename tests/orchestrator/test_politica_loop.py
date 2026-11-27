"""O orquestrador zera a exposição, emite `politica_decisao` e repassa o rastreador."""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.llm.base import ChamadaFerramenta, LLMFalso, RespostaLLM
from harness.orchestrator.loop import Evento, Orquestrador
from harness.orchestrator.loop_json import OrquestradorJson
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ToolCall, ToolResult
from tests.orchestrator.test_loop import Celula, SandboxFalso

PERFIL_COM_TESTES: Perfil = perfil_por_nome("Vendas").com_concessoes({"teste.*"})


class _SemArgs(BaseModel):
    pass


class _SandboxComRastreador(SandboxFalso):
    """Amplia `SandboxFalso` para registrar `ao_abrir` e simular chamadas ao despachante."""

    def __init__(
        self,
        roteiro: Sequence[Celula | BaseException] = (),
        chamadas_por_celula: Sequence[Sequence[ToolCall]] = (),
    ) -> None:
        super().__init__(roteiro)
        self._chamadas_por_celula = list(chamadas_por_celula)
        self.chamadas_executar: list[dict[str, Any]] = []

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
        ao_abrir: Callable[[str], str | None] | None = None,
    ) -> ResultadoExecucao:
        self.chamadas_executar.append(
            {
                "cell_id": cell_id,
                "code": code,
                "timeout_s": timeout_s,
                "despachante": despachante,
                "ao_abrir": ao_abrir,
            }
        )
        if despachante is not None and self._chamadas_por_celula:
            for chamada in self._chamadas_por_celula.pop(0):
                despachante(chamada)
        return super().executar_detalhado(cell_id, code, timeout_s, despachante=despachante)


def _registrar_ferramentas_trifecta(registro: Registro) -> None:
    @registro.ferramenta(
        nome="teste.privado",
        nivel=Nivel.LEITURA,
        descricao="Lê dado privado.",
        args=_SemArgs,
        dado_privado=True,
    )
    def _privado(args: _SemArgs, perfil: Perfil) -> str:
        return "segredo"

    @registro.ferramenta(
        nome="teste.nao_confiavel",
        nivel=Nivel.LEITURA,
        descricao="Lê conteúdo não confiável.",
        args=_SemArgs,
        conteudo_nao_confiavel=True,
    )
    def _nao_confiavel(args: _SemArgs, perfil: Perfil) -> str:
        return "externo"

    @registro.ferramenta(
        nome="teste.externa",
        nivel=Nivel.LEITURA,
        descricao="Envia para canal externo.",
        args=_SemArgs,
        canal_externo=True,
    )
    def _externa(args: _SemArgs, perfil: Perfil) -> str:
        return "enviado"


def _rodar(
    registro: Registro,
    *,
    perfil: Perfil | None = None,
    chamadas_por_celula: Sequence[Sequence[ToolCall]] = (),
    **opcoes: Any,
) -> tuple[list[Evento], _SandboxComRastreador]:
    eventos: list[Evento] = []
    sandbox = _SandboxComRastreador(chamadas_por_celula=chamadas_por_celula)
    orquestrador = Orquestrador(
        LLMFalso(["```python\nFINAL('ok', ['a'])\n```"]),
        sandbox,
        registro,
        perfil or perfil_por_nome("Vendas"),
        eventos=eventos.append,
        **opcoes,
    )
    orquestrador.executar_pedido("qualquer pedido")
    return eventos, sandbox


def test_exposicao_e_zerada_no_inicio_do_pedido() -> None:
    registro = Registro(politica=True)
    registro.exposicao.viu_dado_privado = True
    registro.exposicao.viu_nao_confiavel = True
    _rodar(registro)
    assert (registro.exposicao.viu_dado_privado, registro.exposicao.viu_nao_confiavel) == (
        False,
        False,
    )


def test_decisao_da_politica_vira_evento_de_trace() -> None:
    registro = Registro(politica=True)
    _registrar_ferramentas_trifecta(registro)
    chamadas = [
        [
            ToolCall(call_id="1", name="teste.privado", args={}),
            ToolCall(call_id="2", name="teste.nao_confiavel", args={}),
            ToolCall(call_id="3", name="teste.externa", args={}),
        ]
    ]
    eventos, _ = _rodar(
        registro,
        perfil=PERFIL_COM_TESTES,
        chamadas_por_celula=chamadas,
    )
    decisoes = [e for e in eventos if e.tipo == "politica_decisao"]
    assert len(decisoes) == 3
    externa = next(e for e in decisoes if e.dados["ferramenta"] == "teste.externa")
    assert externa.dados["decisao"] == "pedir_aprovacao"
    assert externa.dados["viu_dado_privado"] is True
    assert externa.dados["viu_nao_confiavel"] is True


def test_rastreador_so_chega_ao_sandbox_quando_existe() -> None:
    _, sem = _rodar(Registro())
    assert all(chamada.get("ao_abrir") is None for chamada in sem.chamadas_executar)
    rastreador = lambda caminho: None  # noqa: E731
    _, com = _rodar(Registro(politica=True), rastreador=rastreador)
    assert any(chamada.get("ao_abrir") is rastreador for chamada in com.chamadas_executar)


def test_loop_json_zera_exposicao_e_emite_politica_decisao() -> None:
    registro = Registro(politica=True)
    _registrar_ferramentas_trifecta(registro)
    registro.exposicao.viu_dado_privado = True
    registro.exposicao.viu_nao_confiavel = True

    modelo = LLMFalso(
        [
            RespostaLLM(
                texto="",
                tokens_entrada=10,
                tokens_saida=5,
                chamadas_ferramenta=(
                    ChamadaFerramenta(id="1", nome="teste__externa", argumentos_brutos="{}"),
                    ChamadaFerramenta(
                        id="f",
                        nome="finalizar",
                        argumentos_brutos='{"resposta":"ok","fontes":["a"]}',
                    ),
                ),
            )
        ]
    )
    eventos: list[Evento] = []
    OrquestradorJson(modelo, registro, PERFIL_COM_TESTES, eventos=eventos.append).executar_pedido(
        "qualquer pedido"
    )

    # Como a exposição foi zerada antes da chamada, `teste.externa` sozinha é permitida.
    decisoes = [e for e in eventos if e.tipo == "politica_decisao"]
    assert len(decisoes) == 1
    assert decisoes[0].dados["ferramenta"] == "teste.externa"
    assert decisoes[0].dados["decisao"] == "permitir"
    assert decisoes[0].dados["viu_dado_privado"] is False
    assert decisoes[0].dados["viu_nao_confiavel"] is False
