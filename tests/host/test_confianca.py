"""Confiança dos arquivos (D10), compartilhada pelo catálogo e pelo rastreio."""

from pathlib import Path

import pytest

from harness.host.confianca import (
    CONFIAVEL,
    EXTERNO,
    ErroManifesto,
    confianca_de,
    ler_manifesto,
)


def test_sem_manifesto_o_arquivo_e_confiavel(tmp_path: Path) -> None:
    assert ler_manifesto(tmp_path) == {}
    assert confianca_de({}, "vendas/a.csv") == CONFIAVEL


def test_manifesto_marca_externo(tmp_path: Path) -> None:
    (tmp_path / "manifesto.json").write_text('{"vendas/a.csv": "externo"}', encoding="utf-8")
    manifesto = ler_manifesto(tmp_path)
    assert confianca_de(manifesto, "vendas/a.csv") == EXTERNO
    assert confianca_de(manifesto, "vendas/b.csv") == CONFIAVEL


def test_valor_desconhecido_vira_externo() -> None:
    assert confianca_de({"x/a": "talvez"}, "x/a") == EXTERNO


@pytest.mark.parametrize("conteudo", ["não é json", "[1, 2]", "42"])
def test_manifesto_invalido_levanta(tmp_path: Path, conteudo: str) -> None:
    (tmp_path / "manifesto.json").write_text(conteudo, encoding="utf-8")
    with pytest.raises(ErroManifesto):
        ler_manifesto(tmp_path)
