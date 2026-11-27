"""`--aprovador` nas avaliações: configuração, relatório e recusa na linha de base."""

import io
import json
from pathlib import Path

import pytest

from harness.evals.__main__ import CODIGO_USO, _parser, main
from harness.evals.executor import ConfigExecucao
from harness.evals.ferramentas_json import registro_json
from harness.evals.relatorio import agregar
from harness.host.aprovacao import sempre_aprova


def test_configuracao_padrao_nao_liga_aprovador() -> None:
    assert ConfigExecucao().aprovador is None


def test_parser_aceita_as_duas_opcoes_e_recusa_outra() -> None:
    assert _parser().parse_args([]).aprovador is None
    assert _parser().parse_args(["--aprovador", "recusar"]).aprovador == "recusar"
    assert _parser().parse_args(["--aprovador", "aprovar"]).aprovador == "aprovar"
    with pytest.raises(SystemExit):
        _parser().parse_args(["--aprovador", "outro"])


def test_registro_json_repassa_o_aprovador(tmp_path: Path) -> None:
    assert registro_json(tmp_path, "json")._aprovador is None
    aprovador = sempre_aprova()
    assert registro_json(tmp_path, "json", aprovador=aprovador)._aprovador == aprovador


def test_relatorio_sem_aprovador_nao_tem_o_campo_no_json() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1)
    assert "aprovador" not in json.loads(relatorio.model_dump_json())


def test_relatorio_com_aprovador_marca_o_campo() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1, aprovador="aprovar")
    assert json.loads(relatorio.model_dump_json())["aprovador"] == "aprovar"


def test_linha_de_base_recusa_o_aprovador(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    saida = io.StringIO()
    codigo = main(
        ["--aprovador", "recusar", "--salvar-linha-de-base", "x", "--saida", str(tmp_path)],
        saida=saida,
    )
    assert codigo == CODIGO_USO
    assert "--aprovador" in saida.getvalue()
