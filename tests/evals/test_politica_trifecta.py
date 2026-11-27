"""`--politica-trifecta` nas avaliações: configuração, relatório e recusa na linha de base."""

import io
import json
from pathlib import Path

import pytest

from harness.evals.__main__ import CODIGO_USO, _parser, main
from harness.evals.executor import ConfigExecucao
from harness.evals.ferramentas_json import registro_json
from harness.evals.relatorio import agregar


def test_configuracao_padrao_nao_liga_a_politica() -> None:
    assert ConfigExecucao().politica_trifecta is False


def test_parser_aceita_a_flag() -> None:
    assert _parser().parse_args(["--politica-trifecta"]).politica_trifecta is True
    assert _parser().parse_args([]).politica_trifecta is False


def test_registro_json_repassa_a_politica(tmp_path: Path) -> None:
    assert registro_json(tmp_path, "json").politica_ligada is False
    assert registro_json(tmp_path, "json", politica=True).politica_ligada is True


def test_relatorio_sem_politica_nao_tem_o_campo_no_json() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1)
    assert "politica_trifecta" not in json.loads(relatorio.model_dump_json())


def test_relatorio_com_politica_marca_o_campo() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1, politica_trifecta=True)
    assert json.loads(relatorio.model_dump_json())["politica_trifecta"] is True


def test_linha_de_base_recusa_a_politica(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    saida = io.StringIO()
    codigo = main(
        ["--politica-trifecta", "--salvar-linha-de-base", "x", "--saida", str(tmp_path)],
        saida=saida,
    )
    assert codigo == CODIGO_USO
    assert "--politica-trifecta" in saida.getvalue()
