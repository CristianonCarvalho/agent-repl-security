"""Testes da CLI com `--provedor` e `--pausa-s`: provedor fixado, linha de base e relatório.

Nada aqui fala com a rede nem com o modelo: a execução e a fábrica são falsas, e os testes que
não devem chegar à execução usam `_explode`. A pasta de trabalho é temporária, então o `.env` do
projeto nunca é lido.
"""

import io
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, main
from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import Classificacao, Relatorio, RodadaCaso, agregar, relatorio_de_json
from harness.llm.base import ModeloLLM

COMMIT = "0" * 40


def _explode(*_: Any, **__: Any) -> Any:
    raise AssertionError("não deveria ser chamado: a validação devia ter parado antes")


def _fabrica_que_explode() -> ModeloLLM:
    raise AssertionError("o modelo não deveria ser criado: a validação devia ter parado antes")


@pytest.fixture(autouse=True)
def _ambiente_limpo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for nome in list(os.environ):
        if nome.startswith(("LANGFUSE", "OTEL", "OPENROUTER")):
            monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("OPENROUTER_MODELO", "modelo-de-teste")


def _caso() -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )


def _relatorio(rodadas: int = 1) -> Relatorio:
    veredito = Veredito(
        caso_id="consulta-01",
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    lista = [
        RodadaCaso(numero=numero, classificacao=Classificacao.APROVADA, veredito=veredito)
        for numero in range(1, rodadas + 1)
    ]
    return agregar([_caso()], {"consulta-01": lista}, semente=42, rodadas_por_caso=rodadas)


def _executor_falso(
    relatorio: Relatorio, chamadas: list[dict[str, Any]] | None = None
) -> Callable[..., Relatorio]:
    def falso(casos: Any, fabrica: Any, config: Any, **_: Any) -> Relatorio:
        if chamadas is not None:
            chamadas.append({"config": config})
        return relatorio

    return falso


def _argumentos(saida: Path, *extra: str) -> list[str]:
    return ["--rodadas", "1", "--saida", str(saida), "--trace", str(saida / "trace"), *extra]


def _relatorio_gravado(saida: Path) -> Relatorio:
    arquivos = list(saida.glob("avaliacao-*.json"))
    assert len(arquivos) == 1
    return relatorio_de_json(arquivos[0].read_text(encoding="utf-8"))


# --- --provedor e OPENROUTER_PROVEDOR --------------------------------------------------------


def test_provedor_da_opcao_sobrescreve_a_variavel_e_vai_para_o_relatorio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVEDOR", "Novita")
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida", "--provedor", "Wafer"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica_que_explode,
    )

    assert codigo == CODIGO_OK
    assert "Provedor solicitado: Wafer" in fala.getvalue()
    assert _relatorio_gravado(tmp_path / "saida").provedor_solicitado == "Wafer"


def test_provedor_so_da_variavel_vai_para_o_relatorio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVEDOR", "Wafer")

    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica_que_explode,
    )

    assert codigo == CODIGO_OK
    assert _relatorio_gravado(tmp_path / "saida").provedor_solicitado == "Wafer"


def test_sem_provedor_o_resumo_diz_que_nao_foi_fixado(tmp_path: Path) -> None:
    fala = io.StringIO()

    main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica_que_explode,
    )

    assert "Provedor solicitado: não fixado" in fala.getvalue()
    assert _relatorio_gravado(tmp_path / "saida").provedor_solicitado is None


def test_nome_invalido_na_opcao_sai_com_dois_antes_de_rodar(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida", "--provedor", "a/b"),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_fabrica_que_explode,
    )

    assert codigo == CODIGO_USO
    assert "--provedor inválido" in fala.getvalue()
    assert "a/b" not in fala.getvalue()


def test_nome_invalido_na_variavel_sai_com_dois_sem_mostrar_o_valor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVEDOR", "a;b")
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_fabrica_que_explode,
    )

    assert codigo == CODIGO_USO
    assert "OPENROUTER_PROVEDOR inválido" in fala.getvalue()
    assert "a;b" not in fala.getvalue()


# --- --salvar-linha-de-base exige provedor --------------------------------------------------


def _argumentos_linha(*extra: str) -> list[str]:
    return ["--rodadas", "3", "--salvar-linha-de-base", "base-teste", *extra]


def _obter(
    commit: str | None, sujo: bool | None
) -> Callable[[Path], tuple[str | None, bool | None]]:
    return lambda _raiz: (commit, sujo)


def test_linha_de_base_sem_provedor_sai_com_dois_antes_de_rodar(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_fabrica_que_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_USO
    assert "--provedor" in texto
    assert "OPENROUTER_PROVEDOR" in texto
    assert not (tmp_path / "evals" / "linhas-de-base").exists()


def test_linha_de_base_com_provedor_da_variavel_segue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_PROVEDOR", "Wafer")
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha("--saida", str(tmp_path / "saida")),
        saida=fala,
        executar=_executor_falso(_relatorio(rodadas=3)),
        fabrica_modelo=_fabrica_que_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_OK
    base = json.loads((tmp_path / "evals" / "linhas-de-base" / "base-teste.json").read_text())
    assert base["provedor_solicitado"] == "Wafer"


def test_linha_de_base_com_provedor_da_opcao_segue(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha("--provedor", "Wafer", "--saida", str(tmp_path / "saida")),
        saida=fala,
        executar=_executor_falso(_relatorio(rodadas=3)),
        fabrica_modelo=_fabrica_que_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_OK
    assert (tmp_path / "evals" / "linhas-de-base" / "base-teste.json").exists()


def test_linha_de_base_sem_provedor_sai_com_dois_mesmo_com_arvore_limpa(tmp_path: Path) -> None:
    codigo = main(
        _argumentos_linha(),
        saida=io.StringIO(),
        executar=_explode,
        fabrica_modelo=_fabrica_que_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_USO


# --- --pausa-s -------------------------------------------------------------------------------


def test_pausa_s_chega_a_configuracao_da_execucao(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []

    codigo = main(
        _argumentos(tmp_path / "saida", "--pausa-s", "0.5"),
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_fabrica_que_explode,
    )

    assert codigo == CODIGO_OK
    assert chamadas[0]["config"].pausa_s == 0.5


def test_pausa_s_padrao_e_zero(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []

    main(
        _argumentos(tmp_path / "saida"),
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_fabrica_que_explode,
    )

    assert chamadas[0]["config"].pausa_s == 0.0


@pytest.mark.parametrize("valor", ["-1", "abc", "nan", "inf"])
def test_pausa_s_invalida_sai_com_dois_antes_de_rodar(tmp_path: Path, valor: str) -> None:
    codigo = main(
        _argumentos(tmp_path / "saida", "--pausa-s", valor),
        saida=io.StringIO(),
        executar=_explode,
        fabrica_modelo=_fabrica_que_explode,
    )

    assert codigo == CODIGO_USO
