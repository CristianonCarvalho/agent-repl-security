"""Testes da ferramenta `catalogo.listar` no host (spec v0, "Ferramentas do host"; D10).

Os testes usam um diretório temporário com a mesma forma de /dados: um subdiretório por
departamento e, opcionalmente, um manifesto.json na raiz com a flag de confiança.
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.host.tools import registrar_todas
from harness.sandbox.protocol import ToolCall, ToolResult


@pytest.fixture
def dados(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido,valor\n1,10\n", encoding="utf-8")
    (raiz / "comercial" / "propostas").mkdir(parents=True)
    (raiz / "comercial" / "propostas" / "p1.txt").write_bytes(b"proposta um")
    (raiz / "comercial" / "contratos.xlsx").write_bytes(b"\x00\x01contrato")
    (raiz / "marketing").mkdir()
    (raiz / "marketing" / "campanhas.csv").write_text("campanha\nx\n", encoding="utf-8")
    (raiz / "financeiro").mkdir()
    (raiz / "financeiro" / "dre.csv").write_text("receita\n5\n", encoding="utf-8")
    (raiz / "direcao").mkdir()
    (raiz / "direcao" / "metas.txt").write_text("meta\n", encoding="utf-8")
    return raiz


def _registro(raiz: Path) -> Registro:
    registro = Registro()
    registrar_todas(registro, raiz)
    return registro


def _listar(registro: Registro, perfil: Perfil, **args: Any) -> ToolResult:
    return registro.despachar(ToolCall(call_id="c1", name="catalogo.listar", args=args), perfil)


def _arquivos(resultado: ToolResult) -> set[str]:
    assert resultado.ok, resultado.error
    assert isinstance(resultado.data, list)
    return {item["arquivo"] for item in resultado.data}


# --- Quais arquivos aparecem -----------------------------------------------------


def test_perfil_vendas_lista_so_vendas_e_comercial(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert _arquivos(resultado) == {
        "vendas/pedidos.csv",
        "comercial/propostas/p1.txt",
        "comercial/contratos.xlsx",
    }


def test_perfil_marketing_nao_ve_vendas(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Marketing"))
    assert _arquivos(resultado) == {"marketing/campanhas.csv"}


def test_departamento_dentro_do_perfil_filtra(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"), departamento="comercial")
    assert _arquivos(resultado) == {"comercial/propostas/p1.txt", "comercial/contratos.xlsx"}


def test_departamento_fora_do_perfil_e_negado_sem_vazar_caminho(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Marketing"), departamento="vendas")
    assert resultado.ok is False
    assert resultado.error is not None
    assert "acesso negado" in resultado.error
    assert "vendas" in resultado.error
    assert str(dados) not in resultado.error


def test_permission_error_direto_da_funcao_e_levantado(dados: Path) -> None:
    from harness.host.tools.catalogo import ListarArgs, listar

    with pytest.raises(PermissionError, match="marketing"):
        listar(ListarArgs(departamento="marketing"), perfil_por_nome("Vendas"), dados)


@pytest.mark.parametrize("departamento", ["../x", "VENDAS", "vendas/../x", "vendas\n", "", "a b"])
def test_departamento_fora_do_padrao_e_rejeitado(dados: Path, departamento: str) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Direção"), departamento=departamento)
    assert resultado.ok is False


def test_argumento_extra_e_recusado(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"), outra="x")
    assert resultado.ok is False


def test_departamento_sem_pasta_no_disco_nao_quebra(tmp_path: Path) -> None:
    resultado = _listar(_registro(tmp_path), perfil_por_nome("Financeiro"))
    assert resultado.ok is True
    assert resultado.data == []


def test_arquivo_de_manifesto_nao_aparece_na_listagem(dados: Path) -> None:
    (dados / "manifesto.json").write_text("{}", encoding="utf-8")
    resultado = _listar(_registro(dados), perfil_por_nome("Direção"))
    assert all(not item.startswith("manifesto") for item in _arquivos(resultado))


def test_link_simbolico_para_fora_nao_e_listado(dados: Path, tmp_path: Path) -> None:
    segredo = tmp_path / "fora" / "segredo.txt"
    segredo.parent.mkdir()
    segredo.write_text("nao listar", encoding="utf-8")
    os.symlink(segredo, dados / "vendas" / "atalho.txt")
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert "vendas/atalho.txt" not in _arquivos(resultado)


# --- Campos de cada item -----------------------------------------------------------


def test_hash_tamanho_e_data_de_cada_arquivo(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert isinstance(resultado.data, list)
    itens = {item["arquivo"]: item for item in resultado.data}
    conteudo = (dados / "vendas" / "pedidos.csv").read_bytes()
    item = itens["vendas/pedidos.csv"]
    assert item["sha256"] == hashlib.sha256(conteudo).hexdigest()
    assert item["bytes"] == len(conteudo)
    data = datetime.fromisoformat(item["modificado"])
    assert data.tzinfo is not None


def test_sem_manifesto_todo_arquivo_e_confiavel(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert isinstance(resultado.data, list)
    assert {item["confianca"] for item in resultado.data} == {"confiavel"}


def test_manifesto_marca_arquivo_como_externo(dados: Path) -> None:
    (dados / "manifesto.json").write_text(
        json.dumps({"comercial/propostas/p1.txt": "externo"}), encoding="utf-8"
    )
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert isinstance(resultado.data, list)
    confianca = {item["arquivo"]: item["confianca"] for item in resultado.data}
    assert confianca["comercial/propostas/p1.txt"] == "externo"
    assert confianca["vendas/pedidos.csv"] == "confiavel"


def test_valor_desconhecido_no_manifesto_vira_externo(dados: Path) -> None:
    (dados / "manifesto.json").write_text(
        json.dumps({"vendas/pedidos.csv": "talvez"}), encoding="utf-8"
    )
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert isinstance(resultado.data, list)
    confianca = {item["arquivo"]: item["confianca"] for item in resultado.data}
    assert confianca["vendas/pedidos.csv"] == "externo"


def test_manifesto_malformado_falha_fechado(dados: Path) -> None:
    (dados / "manifesto.json").write_text("isto não é json", encoding="utf-8")
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert resultado.ok is False
    assert resultado.error is not None
    assert "manifesto" in resultado.error


def test_lista_vem_ordenada_por_nome_de_arquivo(dados: Path) -> None:
    resultado = _listar(_registro(dados), perfil_por_nome("Vendas"))
    assert isinstance(resultado.data, list)
    nomes = [item["arquivo"] for item in resultado.data]
    assert nomes == sorted(nomes)


# --- Registro -----------------------------------------------------------------------


def test_catalogo_listar_declara_leitura_e_so_dado_privado(dados: Path) -> None:
    ferramenta = _registro(dados).obter("catalogo.listar")
    assert ferramenta is not None
    assert ferramenta.nivel is Nivel.LEITURA
    assert ferramenta.dado_privado is True
    assert ferramenta.conteudo_nao_confiavel is False
    assert ferramenta.canal_externo is False
