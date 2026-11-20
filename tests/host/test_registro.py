"""Testes do registro de ferramentas no host (spec v0, "Ferramentas do host" e regra 4).

O despacho nunca levanta exceção: todo problema vira ToolResult com ok=False e mensagem
legível em português.
"""

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.sandbox.protocol import ToolCall, ToolResult


class Entrada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto: str
    numero: int = 0


@pytest.fixture
def chamadas() -> list[Any]:
    return []


@pytest.fixture
def registro(chamadas: list[Any]) -> Registro:
    reg = Registro()

    @reg.ferramenta(nome="teste.eco", nivel=Nivel.LEITURA, descricao="devolve", args=Entrada)
    def eco(args: Entrada, perfil: Perfil) -> Any:
        chamadas.append(perfil.nome)
        return {"texto": args.texto, "n": args.numero}

    @reg.ferramenta(nome="teste.explode", nivel=Nivel.LEITURA, descricao="falha", args=Entrada)
    def explode(args: Entrada, perfil: Perfil) -> Any:
        raise RuntimeError("segredo em /Users/alguem/arquivo")

    @reg.ferramenta(
        nome="teste.escreve", nivel=Nivel.ESCRITA_LOCAL, descricao="escreve", args=Entrada
    )
    def escreve(args: Entrada, perfil: Perfil) -> Any:
        chamadas.append("escreveu")
        return None

    @reg.ferramenta(nome="teste.objeto", nivel=Nivel.LEITURA, descricao="não é JSON", args=Entrada)
    def objeto(args: Entrada, perfil: Perfil) -> Any:
        return object()

    @reg.ferramenta(nome="teste.nan", nivel=Nivel.LEITURA, descricao="NaN não é JSON", args=Entrada)
    def nan(args: Entrada, perfil: Perfil) -> Any:
        return float("nan")

    return reg


@pytest.fixture
def perfil() -> Perfil:
    return perfil_por_nome("Vendas")


def _chamada(nome: str, args: dict[str, Any], call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id=call_id, name=nome, args=args)


# --- Caminho feliz ------------------------------------------------------------


def test_despachar_ok_devolve_data_e_mantem_call_id(
    registro: Registro, perfil: Perfil, chamadas: list[Any]
) -> None:
    resultado = registro.despachar(
        _chamada("teste.eco", {"texto": "oi", "numero": 2}, call_id="abc"), perfil
    )
    assert resultado == ToolResult(call_id="abc", ok=True, data={"texto": "oi", "n": 2})
    assert chamadas == ["Vendas"]


def test_despachante_entrega_funcao_que_despacha_para_o_perfil(
    registro: Registro, perfil: Perfil
) -> None:
    despachante = registro.despachante(perfil)
    chamada = _chamada("teste.eco", {"texto": "x"})
    assert despachante(chamada) == registro.despachar(chamada, perfil)


def test_nome_precisa_ser_grupo_e_nome() -> None:
    reg = Registro()
    for nome in ["semgrupo", "A.b", "grupo.nome.extra", "grupo.Nome", "grupo.1x"]:
        with pytest.raises(ValueError):
            reg.ferramenta(nome=nome, nivel=Nivel.LEITURA, descricao="x", args=Entrada)(
                lambda args, perfil: None
            )


def test_nome_repetido_e_recusado() -> None:
    reg = Registro()
    decorar = reg.ferramenta(nome="grupo.nome", nivel=Nivel.LEITURA, descricao="x", args=Entrada)
    decorar(lambda args, perfil: None)
    with pytest.raises(ValueError):
        decorar(lambda args, perfil: None)


def test_propriedades_da_trifecta_sao_declaradas_com_padrao_falso() -> None:
    reg = Registro()
    reg.ferramenta(
        nome="grupo.privada",
        nivel=Nivel.LEITURA,
        descricao="x",
        args=Entrada,
        dado_privado=True,
    )(lambda args, perfil: None)
    ferramenta = reg.obter("grupo.privada")
    assert ferramenta is not None
    assert ferramenta.dado_privado is True
    assert ferramenta.conteudo_nao_confiavel is False
    assert ferramenta.canal_externo is False


# --- Erros viram ToolResult com ok=False -------------------------------------


def test_ferramenta_inexistente_vira_erro_legivel(registro: Registro, perfil: Perfil) -> None:
    resultado = registro.despachar(_chamada("nao.existe", {}), perfil)
    assert resultado.ok is False
    assert resultado.error is not None
    assert "não existe" in resultado.error


@pytest.mark.parametrize(
    "args",
    [
        {"texto": 5},
        {},
        {"texto": "ok", "zz": 1},
        {"texto": "ok", "numero": "dez"},
    ],
)
def test_argumentos_invalidos_ou_extras_viram_erro(
    registro: Registro, perfil: Perfil, args: dict[str, Any]
) -> None:
    resultado = registro.despachar(_chamada("teste.eco", args), perfil)
    assert resultado.ok is False
    assert resultado.error is not None


def test_argumento_extra_e_nomeado_na_mensagem(registro: Registro, perfil: Perfil) -> None:
    resultado = registro.despachar(_chamada("teste.eco", {"texto": "a", "zz": 1}), perfil)
    assert resultado.error is not None
    assert "zz" in resultado.error


def test_excecao_na_ferramenta_nao_vaza_detalhes_do_host(
    registro: Registro, perfil: Perfil
) -> None:
    resultado = registro.despachar(_chamada("teste.explode", {"texto": "a"}), perfil)
    assert resultado.ok is False
    assert resultado.error is not None
    assert "segredo" not in resultado.error
    assert "/Users" not in resultado.error


def test_nivel_diferente_de_leitura_e_recusado_sem_executar(
    registro: Registro, perfil: Perfil, chamadas: list[Any]
) -> None:
    resultado = registro.despachar(_chamada("teste.escreve", {"texto": "a"}), perfil)
    assert resultado.ok is False
    assert resultado.error is not None
    assert "exige aprovação humana" in resultado.error
    assert chamadas == []


@pytest.mark.parametrize("nome", ["teste.objeto", "teste.nan"])
def test_resultado_que_nao_serializa_em_json_vira_erro(
    registro: Registro, perfil: Perfil, nome: str
) -> None:
    resultado = registro.despachar(_chamada(nome, {"texto": "a"}), perfil)
    assert resultado.ok is False
    assert resultado.error is not None
    assert "JSON" in resultado.error


# --- Propriedade: despachar nunca levanta exceção ----------------------------

_json = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20),
    lambda filhos: (
        st.lists(filhos, max_size=4) | st.dictionaries(st.text(max_size=10), filhos, max_size=4)
    ),
    max_leaves=20,
)


@settings(max_examples=300, deadline=None)
@given(
    nome=st.text(max_size=40),
    args=st.dictionaries(st.text(max_size=12), _json, max_size=5),
    call_id=st.text(min_size=1, max_size=20),
)
def test_despachar_nunca_levanta_para_nome_e_args_arbitrarios(
    nome: str, args: dict[str, Any], call_id: str
) -> None:
    reg = Registro()
    reg.ferramenta(nome="teste.eco", nivel=Nivel.LEITURA, descricao="x", args=Entrada)(
        lambda a, p: {"ecoa": a.texto}
    )
    # Sem catálogo registrado de propósito: basta o nome/args arbitrários não derrubarem nada.
    resultado = reg.despachar(
        ToolCall(call_id=call_id, name=nome, args=args), perfil_por_nome("Direção")
    )
    assert isinstance(resultado, ToolResult)
    assert resultado.call_id == call_id
    # O resultado precisa ser serializável em JSON, seja sucesso ou erro.
    json.dumps(resultado.model_dump(mode="json"), allow_nan=False)
