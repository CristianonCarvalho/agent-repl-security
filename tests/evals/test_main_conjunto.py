"""Testes do `--conjunto {padrao,calculo}` da CLI e dos campos de conjunto no relatório."""

import io
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, _parser, main
from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.comparacao import comparar
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.executor import VARIANTES
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    RodadaCaso,
    agregar,
    relatorio_de_json,
    relatorio_para_json,
)
from harness.llm.base import LLMFalso


def _caso(caso_id: str = "calculo-01", categoria: Categoria = Categoria.CALCULO) -> Caso:
    return Caso(
        id=caso_id,
        categoria=categoria,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )


def _relatorio(
    *,
    caso_id: str = "calculo-01",
    categoria: Categoria = Categoria.CALCULO,
    conjunto: str = "padrao",
    versao_conjunto: str | None = "v1",
) -> Relatorio:
    veredito = Veredito(
        caso_id=caso_id,
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    rodadas = [RodadaCaso(numero=1, classificacao=Classificacao.APROVADA, veredito=veredito)]
    rel = agregar([_caso(caso_id, categoria)], {caso_id: rodadas}, semente=42, rodadas_por_caso=1)
    return rel.model_copy(update={"conjunto": conjunto, "versao_conjunto": versao_conjunto})


def _executor_falso(
    chamadas: list[dict[str, Any]],
    fatos: FatosVerdadeiros | None = None,
) -> Callable[..., Relatorio]:
    """Executor que anota os casos selecionados e a configuração recebida."""

    def falso(
        casos: Callable[[FatosVerdadeiros], Sequence[Caso]],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
    ) -> Relatorio:
        selecionados = list(casos(fatos)) if fatos is not None else []
        chamadas.append({"config": config, "casos": selecionados})
        return _relatorio()

    return falso


def _fabrica() -> LLMFalso:
    return LLMFalso([])


def _argumentos(saida: Path, *extra: str) -> list[str]:
    return ["--rodadas", "1", "--saida", str(saida), "--trace", str(saida / "trace"), *extra]


def test_conjunto_padrao_e_padrao_e_aceita_calculo() -> None:
    assert _parser().parse_args([]).conjunto == "padrao"
    assert _parser().parse_args(["--conjunto", "calculo"]).conjunto == "calculo"
    with pytest.raises(SystemExit):
        _parser().parse_args(["--conjunto", "desconhecido"])


def test_conjunto_calculo_roda_apenas_casos_de_calculo(tmp_path: Path) -> None:
    fatos = gerar_dados(tmp_path / "dados", semente=42)
    chamadas: list[dict[str, Any]] = []
    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida", "--conjunto", "calculo"),
        saida=fala,
        executar=_executor_falso(chamadas, fatos),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_OK
    ids = [c.id for c in chamadas[0]["casos"]]
    assert ids == ["calculo-01", "calculo-02", "calculo-03", "calculo-04"]

    arquivo = next((tmp_path / "saida").glob("avaliacao-*.json"))
    gravado = relatorio_de_json(arquivo.read_text(encoding="utf-8"))
    assert gravado.conjunto == "calculo"
    assert gravado.versao_conjunto == "calculo-nao-congelado"
    assert "Conjunto: calculo" in fala.getvalue()


def test_conjunto_padrao_mantem_conjunto_completo_e_versao_v1(tmp_path: Path) -> None:
    fatos = gerar_dados(tmp_path / "dados", semente=42)
    chamadas: list[dict[str, Any]] = []
    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(chamadas, fatos),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_OK
    ids = [c.id for c in chamadas[0]["casos"]]
    assert len(ids) == 35
    assert all(not cid.startswith("calculo-") for cid in ids)

    arquivo = next((tmp_path / "saida").glob("avaliacao-*.json"))
    gravado = relatorio_de_json(arquivo.read_text(encoding="utf-8"))
    assert gravado.conjunto == "padrao"
    assert gravado.versao_conjunto == "v1"
    assert "Conjunto:" not in fala.getvalue()


def test_conjunto_calculo_respeita_grupo_e_categoria(tmp_path: Path) -> None:
    fatos = gerar_dados(tmp_path / "dados", semente=42)
    chamadas_dev: list[dict[str, Any]] = []
    assert (
        main(
            _argumentos(
                tmp_path / "s1", "--conjunto", "calculo", "--grupo", "desenvolvimento"
            ),
            saida=io.StringIO(),
            executar=_executor_falso(chamadas_dev, fatos),
            fabrica_modelo=_fabrica,
        )
        == CODIGO_OK
    )
    assert [c.id for c in chamadas_dev[0]["casos"]] == ["calculo-01", "calculo-02", "calculo-03"]

    chamadas_res: list[dict[str, Any]] = []
    assert (
        main(
            _argumentos(tmp_path / "s2", "--conjunto", "calculo", "--grupo", "reservado"),
            saida=io.StringIO(),
            executar=_executor_falso(chamadas_res, fatos),
            fabrica_modelo=_fabrica,
        )
        == CODIGO_OK
    )
    assert [c.id for c in chamadas_res[0]["casos"]] == ["calculo-04"]

    chamadas_cat: list[dict[str, Any]] = []
    assert (
        main(
            _argumentos(tmp_path / "s3", "--conjunto", "calculo", "--categoria", "calculo"),
            saida=io.StringIO(),
            executar=_executor_falso(chamadas_cat, fatos),
            fabrica_modelo=_fabrica,
        )
        == CODIGO_OK
    )
    assert [c.id for c in chamadas_cat[0]["casos"]] == [
        "calculo-01",
        "calculo-02",
        "calculo-03",
        "calculo-04",
    ]

    fala_vazia = io.StringIO()
    assert (
        main(
            _argumentos(tmp_path / "s4", "--conjunto", "calculo", "--categoria", "consulta"),
            saida=fala_vazia,
            fabrica_modelo=_fabrica,
        )
        == CODIGO_OK
    )
    assert "Nenhum modelo foi chamado" in fala_vazia.getvalue()
    assert not (tmp_path / "s4").exists()


def test_salvar_linha_de_base_e_recusada_com_conjunto_calculo(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []
    fala = io.StringIO()
    raiz = tmp_path / "repo"
    raiz.mkdir()
    codigo = main(
        _argumentos(
            tmp_path / "saida", "--conjunto", "calculo", "--salvar-linha-de-base", "base-calc"
        ),
        saida=fala,
        executar=_executor_falso(chamadas),
        fabrica_modelo=_fabrica,
        raiz=raiz,
        obter_procedencia=lambda _raiz: ("a" * 40, False),
    )

    assert codigo == CODIGO_USO
    assert "linha de base exige o conjunto padrão" in fala.getvalue()
    assert chamadas == []
    assert not (raiz / "evals").exists()


def test_conjunto_calculo_com_escala_e_tres_variantes(tmp_path: Path) -> None:
    fatos = gerar_dados(tmp_path / "dados", semente=42, escala=5)
    for variante in VARIANTES:
        chamadas: list[dict[str, Any]] = []
        saida = tmp_path / f"saida-{variante}"
        codigo = main(
            _argumentos(
                saida, "--conjunto", "calculo", "--escala", "5", "--variante", variante
            ),
            saida=io.StringIO(),
            executar=_executor_falso(chamadas, fatos),
            fabrica_modelo=_fabrica,
        )

        assert codigo == CODIGO_OK
        assert chamadas[0]["config"].escala == 5
        assert chamadas[0]["config"].variante == variante
        assert len(chamadas[0]["casos"]) == 4

        arquivo = next(saida.glob("avaliacao-*.json"))
        gravado = relatorio_de_json(arquivo.read_text(encoding="utf-8"))
        assert gravado.conjunto == "calculo"
        assert gravado.escala == 5
        assert gravado.versao_conjunto == "calculo-nao-congelado-escala-5"


def test_comparar_recusa_conjuntos_diferentes_e_aceita_iguais(tmp_path: Path) -> None:
    base_padrao = tmp_path / "base_padrao.json"
    novo_calculo = tmp_path / "novo_calculo.json"
    base_padrao.write_text(
        relatorio_para_json(_relatorio(conjunto="padrao", versao_conjunto="v1")),
        encoding="utf-8",
    )
    novo_calculo.write_text(
        relatorio_para_json(
            _relatorio(conjunto="calculo", versao_conjunto="calculo-nao-congelado")
        ),
        encoding="utf-8",
    )

    fala = io.StringIO()
    codigo = main(["comparar", str(base_padrao), str(novo_calculo)], saida=fala)
    assert codigo == CODIGO_USO
    assert "conjuntos diferentes (padrao e calculo)" in fala.getvalue()

    comp_diff = comparar(
        _relatorio(conjunto="padrao", versao_conjunto="v1"),
        _relatorio(conjunto="calculo", versao_conjunto="calculo-nao-congelado"),
    )
    campos = {c.campo: c for c in comp_diff.contexto}
    assert campos["conjunto"].diferente is True
    assert campos["conjunto"].base == "padrao"
    assert campos["conjunto"].novo == "calculo"

    base_calculo = tmp_path / "base_calculo.json"
    base_calculo.write_text(
        relatorio_para_json(
            _relatorio(conjunto="calculo", versao_conjunto="calculo-nao-congelado")
        ),
        encoding="utf-8",
    )
    codigo = main(["comparar", str(base_calculo), str(novo_calculo)], saida=io.StringIO())
    assert codigo == CODIGO_OK


def test_relatorio_antigo_sem_conjunto_carrega_como_padrao() -> None:
    dados = json.loads(relatorio_para_json(_relatorio()))
    dados.pop("conjunto")

    antigo = relatorio_de_json(json.dumps(dados))
    assert antigo.conjunto == "padrao"
