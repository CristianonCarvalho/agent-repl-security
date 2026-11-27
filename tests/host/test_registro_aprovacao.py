from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.host.aprovacao import (
    PedidoDeAprovacao,
    RespostaDeAprovacao,
    sempre_aprova,
    sempre_recusa,
)
from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import ErroFerramenta, Nivel, Registro
from harness.sandbox.protocol import ToolCall


class Vazio(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _perfil() -> Perfil:
    return perfil_por_nome("Vendas").com_concessoes(frozenset({"teste.*"}))


def _registro(**opcoes: Any) -> tuple[Registro, list[str]]:
    chamadas: list[str] = []
    registro = Registro(**opcoes)

    @registro.ferramenta(
        nome="teste.escreve",
        nivel=Nivel.ESCRITA_EXTERNA,
        descricao="x",
        args=Vazio,
        canal_externo=True,
        montar_pedido=lambda args, perfil: (("Arquivo", "a\nb.csv"),),
    )
    def escreve(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("escreveu")
        return "ok"

    @registro.ferramenta(
        nome="teste.invalida",
        nivel=Nivel.ESCRITA_EXTERNA,
        descricao="x",
        args=Vazio,
        montar_pedido=lambda args, perfil: (_ for _ in ()).throw(
            ErroFerramenta("arquivo inválido")
        ),
    )
    def invalida(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("invalida")
        return "ok"

    return registro, chamadas


def _chamar(registro: Registro, nome: str = "teste.escreve") -> Any:
    return registro.despachar(ToolCall(call_id="c1", name=nome, args={}), _perfil())


def test_sem_aprovador_mantem_a_mensagem_de_hoje() -> None:
    registro, chamadas = _registro()
    resultado = _chamar(registro)
    assert resultado.ok is False and "exige aprovação humana" in (resultado.error or "")
    assert chamadas == []


def test_aprovado_executa_e_traz_espera() -> None:
    registro, chamadas = _registro(aprovador=sempre_aprova())
    resultado = _chamar(registro)
    assert resultado.ok is True and chamadas == ["escreveu"]
    assert resultado.espera_s is not None and resultado.espera_s >= 0.0


def test_recusado_nao_executa() -> None:
    registro, chamadas = _registro(aprovador=sempre_recusa())
    resultado = _chamar(registro)
    assert resultado.ok is False and "pessoa responsável" in (resultado.error or "")
    assert chamadas == [] and resultado.espera_s is not None


def test_expirado_nao_executa() -> None:
    registro, chamadas = _registro(aprovador=lambda pedido, prazo: RespostaDeAprovacao.EXPIRADO)
    resultado = _chamar(registro)
    assert "a tempo" in (resultado.error or "") and chamadas == []


def test_aprovador_que_levanta_conta_como_recusa() -> None:
    def quebra(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        raise RuntimeError("segredo em /Users/alguem")

    registro, chamadas = _registro(aprovador=quebra)
    resultado = _chamar(registro)
    assert resultado.ok is False and chamadas == []
    assert "/Users" not in (resultado.error or "")


def test_montador_que_recusa_nao_pede_aprovacao() -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro, chamadas = _registro(aprovador=espiao)
    resultado = _chamar(registro, "teste.invalida")
    assert resultado.error == "arquivo inválido" and pedidos == [] and chamadas == []


def test_pedido_leva_as_linhas_do_montador_e_o_motivo() -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro, _ = _registro(aprovador=espiao)
    _chamar(registro)
    assert pedidos[0].motivo == "nivel" and pedidos[0].linhas == (("Arquivo", "a\nb.csv"),)


def test_motivo_nivel_e_trifecta_pergunta_uma_vez() -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro, _ = _registro(politica=True, aprovador=espiao)
    registro.exposicao.viu_dado_privado = True
    registro.exposicao.viu_nao_confiavel = True
    _chamar(registro)
    assert len(pedidos) == 1 and pedidos[0].motivo == "nivel+trifecta"


def test_observador_recebe_pedida_e_resposta() -> None:
    registro, _ = _registro(aprovador=sempre_aprova())
    eventos: list[tuple[str, dict[str, Any]]] = []
    registro.observar_aprovacao(lambda tipo, dados: eventos.append((tipo, dados)))
    _chamar(registro)
    assert [tipo for tipo, _ in eventos] == ["aprovacao_pedida", "aprovacao_resposta"]
    assert eventos[1][1]["resposta"] == "aprovado"
