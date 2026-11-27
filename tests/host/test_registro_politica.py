"""Registro com a política da trifecta ligada ou desligada (Fase 4, peça 1)."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import MENSAGEM_POLITICA, Nivel, Registro
from harness.sandbox.protocol import ToolCall


class Vazio(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _perfil() -> Perfil:
    return perfil_por_nome("Vendas").com_concessoes(frozenset({"teste.*"}))


def _registro(**opcoes: Any) -> tuple[Registro, list[str]]:
    chamadas: list[str] = []
    registro = Registro(**opcoes)

    @registro.ferramenta(
        nome="teste.privado", nivel=Nivel.LEITURA, descricao="x", args=Vazio, dado_privado=True
    )
    def privado(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("privado")
        return "ok"

    @registro.ferramenta(
        nome="teste.externa", nivel=Nivel.LEITURA, descricao="x", args=Vazio, canal_externo=True
    )
    def externa(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("externa")
        return "ok"

    @registro.ferramenta(
        nome="teste.suja",
        nivel=Nivel.LEITURA,
        descricao="x",
        args=Vazio,
        conteudo_nao_confiavel=True,
    )
    def suja(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("suja")
        return "ok"

    @registro.ferramenta(
        nome="teste.suja_que_falha",
        nivel=Nivel.LEITURA,
        descricao="x",
        args=Vazio,
        conteudo_nao_confiavel=True,
    )
    def suja_que_falha(args: Vazio, perfil: Perfil) -> Any:
        raise RuntimeError("segredo em /Users/alguem")

    return registro, chamadas


def _chamar(registro: Registro, nome: str) -> Any:
    return registro.despachar(ToolCall(call_id="c1", name=nome, args={}), _perfil())


def test_politica_desligada_nunca_recusa_e_nao_rastreia() -> None:
    registro, chamadas = _registro()
    assert registro.politica_ligada is False
    assert _chamar(registro, "teste.privado").ok
    assert _chamar(registro, "teste.suja").ok
    assert _chamar(registro, "teste.externa").ok
    assert chamadas == ["privado", "suja", "externa"]
    assert registro.exposicao.viu_dado_privado is False
    assert registro.exposicao.viu_nao_confiavel is False


def test_politica_ligada_recusa_externa_depois_de_privado_e_nao_confiavel() -> None:
    registro, chamadas = _registro(politica=True)
    assert _chamar(registro, "teste.privado").ok
    assert _chamar(registro, "teste.suja").ok
    resultado = _chamar(registro, "teste.externa")
    assert resultado.ok is False
    assert resultado.error == MENSAGEM_POLITICA
    assert "externa" not in chamadas


def test_politica_ligada_permite_externa_sem_exposicao() -> None:
    registro, chamadas = _registro(politica=True)
    assert _chamar(registro, "teste.externa").ok
    assert chamadas == ["externa"]


def test_mensagem_de_recusa_nao_vaza_detalhe() -> None:
    assert "/" not in MENSAGEM_POLITICA
    assert "dados" not in MENSAGEM_POLITICA.lower().replace("enviados", "")


def test_aprovador_pode_liberar_a_chamada() -> None:
    registro, chamadas = _registro(politica=True, aprovador=lambda chamada, ferramenta: True)
    _chamar(registro, "teste.privado")
    _chamar(registro, "teste.suja")
    assert _chamar(registro, "teste.externa").ok
    assert "externa" in chamadas


def test_erro_em_ferramenta_nao_confiavel_ainda_marca_a_exposicao() -> None:
    registro, _ = _registro(politica=True)
    resultado = _chamar(registro, "teste.suja_que_falha")
    assert resultado.ok is False
    assert registro.exposicao.viu_nao_confiavel is True


def test_observador_recebe_cada_decisao() -> None:
    registro, _ = _registro(politica=True)
    eventos: list[dict[str, Any]] = []
    registro.observar_politica(eventos.append)
    _chamar(registro, "teste.privado")
    _chamar(registro, "teste.suja")
    _chamar(registro, "teste.externa")
    assert [e["decisao"] for e in eventos] == ["permitir", "permitir", "pedir_aprovacao"]
    assert eventos[-1] == {
        "ferramenta": "teste.externa",
        "decisao": "pedir_aprovacao",
        "viu_dado_privado": True,
        "viu_nao_confiavel": True,
    }


def test_observador_que_falha_nao_derruba_o_despacho() -> None:
    registro, _ = _registro(politica=True)

    def quebra(_: dict[str, Any]) -> None:
        raise RuntimeError("falha do consumidor")

    registro.observar_politica(quebra)
    assert _chamar(registro, "teste.externa").ok
