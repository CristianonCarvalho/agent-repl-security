"""T19-e: filtro `--caso` da CLI e modelo/provedor gravados em cada rodada."""

import io
from pathlib import Path

import pytest

from harness.evals.__main__ import CODIGO_USO, _parser, _selecionar, main
from harness.evals.dados import gerar_dados
from harness.evals.relatorio import Classificacao, RodadaCaso


def test_parser_aceita_caso_repetido() -> None:
    args = _parser().parse_args(["--caso", "injecao-08", "--caso", "injecao-09"])
    assert args.caso == ["injecao-08", "injecao-09"]


def test_selecionar_filtra_por_id(tmp_path: Path) -> None:
    fatos = gerar_dados(tmp_path / "d", 42, com_ataques_disfarcados=True)
    casos = _selecionar(frozenset(), "todos", "injecao-disfarcada", frozenset({"injecao-08"}))(
        fatos
    )
    assert [c.id for c in casos] == ["injecao-08"]


def test_linha_de_base_recusa_caso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    saida = io.StringIO()
    codigo = main(
        ["--caso", "injecao-08", "--salvar-linha-de-base", "x", "--saida", str(tmp_path)],
        saida=saida,
    )
    assert codigo == CODIGO_USO
    assert "--caso" in saida.getvalue()


def test_rodada_guarda_modelo_e_provedor() -> None:
    rodada = RodadaCaso(
        numero=1, classificacao=Classificacao.ERRO_INFRA, detalhe="x", modelo="a/b", provedor="P"
    )
    assert (rodada.modelo, rodada.provedor) == ("a/b", "P")
    assert RodadaCaso(numero=1, classificacao=Classificacao.ERRO_INFRA, detalhe="x").modelo is None
