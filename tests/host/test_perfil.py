"""Testes dos perfis de acesso e da montagem de dados por perfil (spec v0, "Perfis de acesso")."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.host.perfil import PERFIS, montagens_do_perfil, perfil_por_nome


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
