"""Os orquestradores emitem `aprovacao_pedida` e `aprovacao_resposta` sem vazar argumentos."""

from pydantic import BaseModel

from harness.host.aprovacao import sempre_aprova
from harness.host.perfil import Perfil
from harness.host.registry import Nivel, Registro
from harness.llm.base import ChamadaFerramenta, LLMFalso, RespostaLLM
from harness.orchestrator.loop import Evento, Orquestrador
from harness.orchestrator.loop_json import OrquestradorJson
from harness.sandbox.protocol import ToolCall
from tests.orchestrator.test_politica_loop import PERFIL_COM_TESTES, _SandboxComRastreador

_CAMINHO_SECRETO = "dados/segredo_teste.txt"
_CONTEUDO_SECRETO = "conteudo_confidencial_do_arquivo"


class _ArgsEscrita(BaseModel):
    caminho: str
    conteudo: str


def _registrar_ferramenta_escrita(registro: Registro) -> None:
    @registro.ferramenta(
        nome="teste.escrita",
        nivel=Nivel.ESCRITA_LOCAL,
        descricao="Escreve conteúdo em um arquivo.",
        args=_ArgsEscrita,
    )
    def _escrita(args: _ArgsEscrita, perfil: Perfil) -> str:
        return "escrito"


def _assert_eventos_aprovacao(eventos: list[Evento]) -> None:
    pedidas = [e for e in eventos if e.tipo == "aprovacao_pedida"]
    respostas = [e for e in eventos if e.tipo == "aprovacao_resposta"]
    assert len(pedidas) == 1
    assert len(respostas) == 1
    assert pedidas[0].dados["ferramenta"] == "teste.escrita"
    assert respostas[0].dados["ferramenta"] == "teste.escrita"
    assert respostas[0].dados["resposta"] == "aprovado"

    for evento in (*pedidas, *respostas):
        serializado = repr(evento.dados)
        assert _CAMINHO_SECRETO not in serializado
        assert _CONTEUDO_SECRETO not in serializado
        assert "caminho" not in evento.dados
        assert "conteudo" not in evento.dados


def test_orquestrador_emite_eventos_de_aprovacao() -> None:
    registro = Registro(aprovador=sempre_aprova())
    _registrar_ferramenta_escrita(registro)
    chamadas = [
        [
            ToolCall(
                call_id="1",
                name="teste.escrita",
                args={"caminho": _CAMINHO_SECRETO, "conteudo": _CONTEUDO_SECRETO},
            )
        ]
    ]
    eventos: list[Evento] = []
    sandbox = _SandboxComRastreador(chamadas_por_celula=chamadas)
    Orquestrador(
        LLMFalso(["```python\nFINAL('ok', ['a'])\n```"]),
        sandbox,
        registro,
        PERFIL_COM_TESTES,
        eventos=eventos.append,
    ).executar_pedido("qualquer pedido")

    _assert_eventos_aprovacao(eventos)


def test_loop_json_emite_eventos_de_aprovacao() -> None:
    registro = Registro(aprovador=sempre_aprova())
    _registrar_ferramenta_escrita(registro)
    modelo = LLMFalso(
        [
            RespostaLLM(
                texto="",
                tokens_entrada=10,
                tokens_saida=5,
                chamadas_ferramenta=(
                    ChamadaFerramenta(
                        id="1",
                        nome="teste__escrita",
                        argumentos_brutos=(
                            f'{{"caminho":"{_CAMINHO_SECRETO}","conteudo":"{_CONTEUDO_SECRETO}"}}'
                        ),
                    ),
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
    OrquestradorJson(
        modelo,
        registro,
        PERFIL_COM_TESTES,
        eventos=eventos.append,
    ).executar_pedido("qualquer pedido")

    _assert_eventos_aprovacao(eventos)
