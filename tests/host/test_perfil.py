"""Testes dos perfis de acesso, das concessões de ferramentas e da montagem de dados por perfil.

Cobrem a spec v0 ("Perfis de acesso") e as concessões da D14 (versão mínima).
"""

import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from harness.host.perfil import (
    CONCESSOES_BASE,
    PERFIS,
    Perfil,
    montagens_do_perfil,
    perfil_por_nome,
)


def test_quatro_perfis_da_spec() -> None:
    assert {perfil.nome for perfil in PERFIS} == {"Vendas", "Marketing", "Financeiro", "Direção"}


@pytest.mark.parametrize(
    ("nome", "departamentos"),
    [
        ("Vendas", {"vendas", "comercial"}),
        ("Marketing", {"marketing"}),
        ("Financeiro", {"financeiro", "vendas", "comercial"}),
        ("Direção", {"vendas", "comercial", "marketing", "financeiro", "direcao"}),
    ],
)
def test_departamentos_de_cada_perfil_seguem_a_spec(nome: str, departamentos: set[str]) -> None:
    assert perfil_por_nome(nome).departamentos == frozenset(departamentos)


def test_busca_de_perfil_ignora_acento_e_caixa() -> None:
    assert perfil_por_nome("direção") == perfil_por_nome("Direção")
    assert perfil_por_nome("FINANCEIRO").nome == "Financeiro"


def test_perfil_desconhecido_levanta_valueerror() -> None:
    with pytest.raises(ValueError, match="desconhecido"):
        perfil_por_nome("Estagiário")


def test_perfil_e_imutavel() -> None:
    perfil = perfil_por_nome("Vendas")
    with pytest.raises(ValidationError):
        perfil.nome = "Outro"


def test_montagens_so_dos_departamentos_do_perfil(tmp_path: Path) -> None:
    montagens = montagens_do_perfil(perfil_por_nome("Vendas"), tmp_path)
    assert montagens == {"vendas": tmp_path / "vendas", "comercial": tmp_path / "comercial"}


@pytest.mark.parametrize("nome", ["Vendas", "Marketing", "Financeiro", "Direção"])
def test_montagens_de_cada_perfil_correspondem_aos_departamentos(tmp_path: Path, nome: str) -> None:
    perfil = perfil_por_nome(nome)
    montagens = montagens_do_perfil(perfil, tmp_path)
    assert set(montagens) == set(perfil.departamentos)
    for departamento, caminho in montagens.items():
        assert caminho == tmp_path / departamento


# --- Concessões: negar por padrão e base dos perfis embutidos -------------------------------


def test_sem_concessoes_o_perfil_nega_tudo() -> None:
    perfil = Perfil(nome="Nu", departamentos=frozenset({"vendas"}))
    assert perfil.concessoes == frozenset()
    assert perfil.permite("catalogo.listar") is False
    assert perfil.permite("descoberta.buscar") is False


@pytest.mark.parametrize("perfil", PERFIS, ids=lambda perfil: perfil.nome)
def test_perfis_embutidos_recebem_a_base_de_concessoes(perfil: Perfil) -> None:
    assert perfil.concessoes == CONCESSOES_BASE
    assert CONCESSOES_BASE == frozenset({"catalogo.listar", "descoberta.*"})
    assert perfil.permite("catalogo.listar") is True
    assert perfil.permite("descoberta.descrever") is True
    assert perfil.permite("arquivos.historico") is False


# --- Validação das concessões -----------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        "",
        "semponto",
        "eco.",
        ".x",
        "eco.x.y",
        "Eco.x",
        "eco.X",
        "eco.1x",
        "eco .x",
        " eco.x",
        "eco.x ",
        "eco.*x",
        "eco.**",
        "eco.*.x",
        "*.x",
        "*",
        "eco*",
        "eco-x.y",
        "eco.x\n",
    ],
)
def test_concessao_com_formato_invalido_levanta_valueerror(item: str) -> None:
    with pytest.raises(ValueError):
        Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({item}))


@pytest.mark.parametrize("item", ["catalogo.listar", "descoberta.*", "a_b.c_d", "_.*", "_._"])
def test_concessao_com_formato_valido_e_aceita(item: str) -> None:
    perfil = Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({item}))
    assert item in perfil.concessoes


def test_concessao_que_nao_e_texto_e_recusada() -> None:
    with pytest.raises(ValueError):
        Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({5}))  # type: ignore[arg-type]


# --- Curingas e nomes exatos -------------------------------------------------------------------


def test_curinga_casa_so_o_grupo_inteiro() -> None:
    perfil = Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({"descoberta.*"}))
    assert perfil.permite("descoberta.buscar") is True
    assert perfil.permite("descoberta.descrever") is True
    assert perfil.permite("descoberta") is False


def test_curinga_de_grupo_curto_nao_casa_grupo_com_o_mesmo_comeco() -> None:
    perfil = Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({"desc.*"}))
    assert perfil.permite("desc.x") is True
    assert perfil.permite("descoberta.buscar") is False


def test_curinga_eco_nao_casa_eco2() -> None:
    perfil = Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({"eco.*"}))
    assert perfil.permite("eco.x") is True
    assert perfil.permite("eco2.x") is False


def test_nome_exato_casa_so_o_proprio_nome() -> None:
    perfil = Perfil(nome="X", departamentos=frozenset(), concessoes=frozenset({"catalogo.listar"}))
    assert perfil.permite("catalogo.listar") is True
    assert perfil.permite("catalogo.listarr") is False
    assert perfil.permite("catalogo.buscar") is False
    assert perfil.permite("catalogo") is False


@pytest.mark.parametrize(
    "nome",
    [
        "",
        " ",
        "eco",
        "eco.",
        ".x",
        "eco.x\n",
        "eco.x\x00",
        "eco.x.y",
        "eco.*",
        "é.x",
        "eco é.x",
        "ECO.x",
        "x" * 10_000,
    ],
)
def test_nomes_estranhos_nunca_sao_permitidos(nome: str) -> None:
    perfil = Perfil(
        nome="X", departamentos=frozenset(), concessoes=frozenset({"eco.*", "descoberta.*"})
    )
    assert perfil.permite(nome) is False


# --- com_concessoes -------------------------------------------------------------------------


def test_com_concessoes_devolve_copia_com_a_uniao_e_nao_muda_o_original() -> None:
    base = perfil_por_nome("Vendas")
    ampliado = base.com_concessoes(["teste.eco", "teste.*"])
    assert ampliado.concessoes == CONCESSOES_BASE | {"teste.eco", "teste.*"}
    assert ampliado.nome == base.nome
    assert ampliado.departamentos == base.departamentos
    assert base.concessoes == CONCESSOES_BASE
    assert ampliado.permite("teste.eco") is True
    assert base.permite("teste.eco") is False


def test_com_concessoes_vazio_mantem_as_concessoes() -> None:
    assert perfil_por_nome("Vendas").com_concessoes([]) == perfil_por_nome("Vendas")


def test_com_concessoes_valida_os_itens_somados() -> None:
    with pytest.raises(ValueError):
        perfil_por_nome("Vendas").com_concessoes(["formato errado"])


# --- Consistência entre as duas camadas (D14, transição) ---------------------------------------


@pytest.mark.parametrize("perfil", PERFIS, ids=lambda perfil: perfil.nome)
def test_concessao_de_catalogo_exige_departamento_montado(perfil: Perfil, tmp_path: Path) -> None:
    """Quem pode listar o catálogo tem departamento, e cada departamento tem montagem própria.

    O catálogo lê `perfil.departamentos`, e a montagem do contêiner usa o mesmo conjunto. Se
    as duas camadas divergirem (concessão sem departamento, ou departamento sem diretório
    montado), este teste falha.
    """
    montagens = montagens_do_perfil(perfil, tmp_path)
    if perfil.permite("catalogo.listar"):
        assert perfil.departamentos, perfil.nome
    assert set(montagens) == set(perfil.departamentos)
    for departamento, caminho in montagens.items():
        assert caminho == tmp_path / departamento


# --- Propriedades (Hypothesis) ---------------------------------------------------------------

_GRUPO = st.from_regex(r"[a-z_]{1,6}", fullmatch=True)
_NOME_DA_FERRAMENTA = st.from_regex(r"[a-z_]{1,6}\.[a-z_]{1,6}", fullmatch=True)
_ITEM = st.one_of(_NOME_DA_FERRAMENTA, st.builds(lambda g: f"{g}.*", _GRUPO))
_CONCESSOES = st.frozensets(_ITEM, max_size=6)
_NOME_QUALQUER = st.one_of(
    st.text(max_size=40),
    st.from_regex(r"[a-z_.*]{0,12}", fullmatch=True),
    _NOME_DA_FERRAMENTA,
)


@settings(max_examples=500, deadline=None)
@given(nome=_NOME_QUALQUER, concessoes=_CONCESSOES)
def test_permite_so_quando_ha_item_exato_ou_curinga_do_grupo(
    nome: str, concessoes: frozenset[str]
) -> None:
    """Para nomes bem formados, permite é verdadeiro sse há o item exato ou o curinga do grupo."""
    perfil = Perfil(nome="P", departamentos=frozenset(), concessoes=concessoes)
    bem_formado = re.fullmatch(r"[a-z_]+\.[a-z_]+", nome) is not None
    grupo = nome.split(".", 1)[0]
    esperado = bem_formado and (nome in concessoes or f"{grupo}.*" in concessoes)
    assert perfil.permite(nome) == esperado


@settings(max_examples=200, deadline=None)
@given(concessoes=_CONCESSOES)
def test_concessoes_geradas_sao_validas_e_permitem_o_que_nomeiam(
    concessoes: frozenset[str],
) -> None:
    """Cada item válido permite o próprio nome exato, ou um nome qualquer do grupo do curinga."""
    perfil = Perfil(nome="P", departamentos=frozenset(), concessoes=concessoes)
    for item in concessoes:
        assert re.fullmatch(r"[a-z_]+\.(?:[a-z_]+|\*)", item)
        if item.endswith(".*"):
            assert perfil.permite(f"{item[:-2]}.qualquer") is True
        else:
            assert perfil.permite(item) is True
