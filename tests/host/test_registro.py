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
from harness.host.registry import LIMITE_RESULTADO_PADRAO, Nivel, Registro
from harness.sandbox.protocol import ToolCall, ToolResult

# Perfil de Vendas com as ferramentas `teste.*` concedidas: os testes do registro usam esse grupo.
CONCESSAO_DE_TESTE: frozenset[str] = frozenset({"teste.*"})


def _perfil_de_teste() -> Perfil:
    return perfil_por_nome("Vendas").com_concessoes(CONCESSAO_DE_TESTE)


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
    return _perfil_de_teste()


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


# --- Limite de tamanho do resultado ------------------------------------------


def _registro_com_dado(limite: int, dado: Any) -> Registro:
    reg = Registro(limite_resultado_bytes=limite)
    reg.ferramenta(nome="teste.dado", nivel=Nivel.LEITURA, descricao="x", args=Entrada)(
        lambda args, perfil: dado
    )
    return reg


def _despachar_dado(reg: Registro, perfil: Perfil) -> ToolResult:
    return reg.despachar(_chamada("teste.dado", {"texto": "a"}), perfil)


def test_limite_padrao_e_um_mebibyte() -> None:
    assert LIMITE_RESULTADO_PADRAO == 1024 * 1024
    # Sem argumento, o Registro usa o padrão: a string de 1 MiB - 2 letras fica exatamente no
    # limite (as aspas somam 2 bytes) e a de uma letra a mais é recusada.
    perfil = _perfil_de_teste()
    no_limite = Registro()
    no_limite.ferramenta(nome="teste.dado", nivel=Nivel.LEITURA, descricao="x", args=Entrada)(
        lambda args, perfil: "a" * (LIMITE_RESULTADO_PADRAO - 2)
    )
    assert _despachar_dado(no_limite, perfil).ok is True
    acima = Registro()
    acima.ferramenta(nome="teste.dado", nivel=Nivel.LEITURA, descricao="x", args=Entrada)(
        lambda args, perfil: "a" * (LIMITE_RESULTADO_PADRAO - 1)
    )
    assert _despachar_dado(acima, perfil).ok is False


def test_resultado_abaixo_do_limite_passa(perfil: Perfil) -> None:
    resultado = _despachar_dado(_registro_com_dado(100, {"a": "b"}), perfil)
    assert resultado.ok is True
    assert resultado.data == {"a": "b"}


def test_resultado_exatamente_no_limite_passa(perfil: Perfil) -> None:
    # A string "a"*98 serializa para 100 bytes: '"' + 98 letras + '"'.
    resultado = _despachar_dado(_registro_com_dado(100, "a" * 98), perfil)
    assert resultado.ok is True
    assert resultado.data == "a" * 98


def test_resultado_acima_do_limite_falha_sem_vazar_o_conteudo(perfil: Perfil) -> None:
    resultado = _despachar_dado(_registro_com_dado(100, "SEGREDO" + "a" * 92), perfil)
    assert resultado.ok is False
    assert resultado.data is None
    assert resultado.error == "resultado da ferramenta excede o limite de 100 bytes"
    assert "SEGREDO" not in resultado.error
    assert "aaaa" not in resultado.error


def test_limite_conta_bytes_utf8_e_nao_caracteres(perfil: Perfil) -> None:
    # Cada "é" ocupa 2 bytes em utf-8 sem escape: "é"*49 serializa para 100 bytes.
    assert _despachar_dado(_registro_com_dado(100, "é" * 49), perfil).ok is True
    # Com 50 "é" são 102 bytes: passa de 100 mesmo com só 52 caracteres.
    resultado = _despachar_dado(_registro_com_dado(100, "é" * 50), perfil)
    assert resultado.ok is False


@pytest.mark.parametrize("limite", [0, -1, True, 1.5, "100"])
def test_limite_invalido_e_recusado_na_construcao(limite: Any) -> None:
    with pytest.raises(ValueError):
        Registro(limite_resultado_bytes=limite)


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


# --- Concessões (D14): negar por padrão, sem vazar o que existe ----------------------------


def _sem_concessao() -> Perfil:
    """Vendas com a base de concessões dos perfis embutidos, sem nenhuma `teste.*`."""
    return perfil_por_nome("Vendas")


def _inexistente(nome: str, call_id: str = "c1") -> ToolResult:
    """Resultado que um nome qualquer recebe quando não existe nenhuma ferramenta."""
    return Registro().despachar(_chamada(nome, {"texto": "a"}, call_id=call_id), _sem_concessao())


def test_ferramenta_nao_concedida_responde_igual_a_inexistente(
    registro: Registro, chamadas: list[Any]
) -> None:
    negada = registro.despachar(_chamada("teste.eco", {"texto": "a"}), _sem_concessao())
    assert negada == _inexistente("teste.eco")
    assert negada.ok is False
    assert negada.error == "a ferramenta 'teste.eco' não existe"


def test_ferramenta_nao_concedida_nao_executa_a_funcao(
    registro: Registro, chamadas: list[Any]
) -> None:
    registro.despachar(_chamada("teste.eco", {"texto": "a"}), _sem_concessao())
    registro.despachar(_chamada("teste.escreve", {"texto": "a"}), _sem_concessao())
    assert chamadas == []


def test_ferramenta_nao_concedida_nao_chega_a_validar_argumentos(registro: Registro) -> None:
    # Argumentos inválidos e nível de escrita não aparecem para quem não tem a concessão.
    casos: list[tuple[str, dict[str, Any]]] = [
        ("teste.eco", {"zz": 1}),
        ("teste.escreve", {"texto": "a"}),
    ]
    for nome, args in casos:
        resultado = registro.despachar(_chamada(nome, args), _sem_concessao())
        assert resultado == _inexistente(nome), nome


def test_nome_longo_nao_concedido_e_truncado_como_inexistente(registro: Registro) -> None:
    nome = "teste." + "x" * 300
    resultado = registro.despachar(_chamada(nome, {}), _sem_concessao())
    assert resultado == _inexistente(nome)
    assert resultado.error is not None
    assert "x" * 81 not in resultado.error


def test_ferramenta_concedida_passa_pela_concessao(registro: Registro, chamadas: list[Any]) -> None:
    resultado = registro.despachar(_chamada("teste.eco", {"texto": "a"}), _perfil_de_teste())
    assert resultado.ok is True
    assert chamadas == ["Vendas"]


def test_concessao_exata_libera_so_aquele_nome(registro: Registro, chamadas: list[Any]) -> None:
    perfil = _sem_concessao().com_concessoes(["teste.eco"])
    assert registro.despachar(_chamada("teste.eco", {"texto": "a"}), perfil).ok is True
    negada = registro.despachar(_chamada("teste.explode", {"texto": "a"}), perfil)
    assert negada == _inexistente("teste.explode")


def test_ordem_e_concessao_depois_validacao_depois_nivel(
    registro: Registro, chamadas: list[Any]
) -> None:
    sem = _sem_concessao()
    # Sem concessão, o nível de escrita não chega a ser avaliado.
    negada = registro.despachar(_chamada("teste.escreve", {}), sem)
    assert negada.error == "a ferramenta 'teste.escreve' não existe"
    # Com concessão, a validação vem antes da checagem de nível.
    invalido = registro.despachar(_chamada("teste.escreve", {"zz": 1}), _perfil_de_teste())
    assert invalido.error is not None and invalido.error.startswith("argumentos inválidos")
    # Com concessão e argumentos válidos, o nível de escrita recusa a chamada sem executar.
    recusada = registro.despachar(_chamada("teste.escreve", {"texto": "a"}), _perfil_de_teste())
    assert recusada.error is not None and "exige aprovação humana" in recusada.error
    assert chamadas == []


def test_visiveis_traz_so_as_concedidas_em_ordem_de_nome(chamadas: list[Any]) -> None:
    reg = Registro()
    for nome in ["teste.zeta", "grupo.x", "teste.eco", "outro.y"]:
        reg.ferramenta(nome=nome, nivel=Nivel.LEITURA, descricao="x", args=Entrada)(
            lambda args, perfil: None
        )
    perfil = _sem_concessao().com_concessoes(["teste.*", "grupo.x"])
    assert [f.nome for f in reg.visiveis(perfil)] == ["grupo.x", "teste.eco", "teste.zeta"]
    assert [f.nome for f in reg.visiveis(_sem_concessao())] == []


# --- Propriedade de concessões (Hypothesis) -------------------------------------------------

_NOMES_DE_TESTE = st.one_of(
    st.text(max_size=40),
    st.sampled_from(["teste.eco", "teste.escreve", "teste.*", "teste.", ".eco", "eco.*"]),
    st.from_regex(r"teste\.[a-z]{0,4}", fullmatch=True),
)
_ITENS_DE_CONCESSAO = st.one_of(
    st.sampled_from(["teste.eco", "teste.escreve", "teste.explode", "teste.*", "outro.*"]),
    st.from_regex(r"[a-z]{1,4}\.[a-z]{1,4}", fullmatch=True),
    st.from_regex(r"[a-z]{1,4}\.\*", fullmatch=True),
)


@settings(max_examples=400, deadline=None)
@given(nome=_NOMES_DE_TESTE, concessoes=st.frozensets(_ITENS_DE_CONCESSAO, max_size=5))
def test_funcao_so_executa_quando_o_perfil_permite_o_nome(
    nome: str, concessoes: frozenset[str]
) -> None:
    """Para qualquer nome e qualquer conjunto de concessões válido, o despacho é fiel à regra."""
    chamadas: list[str] = []

    def eco(args: Entrada, perfil: Perfil) -> Any:
        chamadas.append(args.texto)
        return args.texto

    reg = Registro()
    reg.ferramenta(nome="teste.eco", nivel=Nivel.LEITURA, descricao="x", args=Entrada)(eco)
    perfil = Perfil(nome="P", departamentos=frozenset(), concessoes=concessoes)

    resultado = reg.despachar(_chamada(nome, {"texto": "a"}), perfil)

    permitido = perfil.permite(nome)
    if not permitido:
        assert chamadas == []
        assert resultado == Registro().despachar(_chamada(nome, {"texto": "a"}), perfil)
    if chamadas:
        assert nome == "teste.eco"
        assert permitido is True
    if permitido and nome == "teste.eco":
        assert chamadas == ["a"]
        assert resultado.ok is True
