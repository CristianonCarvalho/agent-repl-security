"""Testes do `--escala` da linha de comando e dos campos de escala no relatório (T18c-a).

Como em `test_main.py`, o executor real não roda: `main` recebe um `executar` falso, e a seleção
sem modelo (categoria só com pendentes) roda de verdade, sem chamar o modelo nem ler o `.env`.
"""

import io
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, _parser, main
from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    RodadaCaso,
    agregar,
    relatorio_de_json,
    relatorio_para_json,
)
from harness.llm.base import LLMFalso


def _caso() -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )


def _relatorio() -> Relatorio:
    veredito = Veredito(
        caso_id="consulta-01",
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    rodadas = [RodadaCaso(numero=1, classificacao=Classificacao.APROVADA, veredito=veredito)]
    return agregar([_caso()], {"consulta-01": rodadas}, semente=42, rodadas_por_caso=1)


def _executor_falso(chamadas: list[dict[str, Any]]) -> Callable[..., Relatorio]:
    """Executor que anota a configuração recebida e devolve um relatório pronto."""

    def falso(
        casos: Callable[..., Sequence[Caso]],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
    ) -> Relatorio:
        chamadas.append({"config": config})
        return _relatorio()

    return falso


def _fabrica() -> LLMFalso:
    return LLMFalso([])


def _argumentos(saida: Path, *extra: str) -> list[str]:
    return ["--rodadas", "1", "--saida", str(saida), "--trace", str(saida / "trace"), *extra]


def test_escala_padrao_e_1() -> None:
    assert _parser().parse_args([]).escala == 1


@pytest.mark.parametrize("valor", ["0", "1001", "abc", "-3"])
def test_escala_fora_da_faixa_sai_com_dois_sem_executar(tmp_path: Path, valor: str) -> None:
    chamadas: list[dict[str, Any]] = []
    codigo = main(
        _argumentos(tmp_path / "saida", "--escala", valor),
        saida=io.StringIO(),
        executar=_executor_falso(chamadas),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_USO
    assert chamadas == []


def test_escala_chega_ao_executor_e_ao_relatorio_gravado(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []
    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida", "--escala", "10"),
        saida=fala,
        executar=_executor_falso(chamadas),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_OK
    assert chamadas[0]["config"].escala == 10
    arquivo = next((tmp_path / "saida").glob("avaliacao-*.json"))
    gravado = relatorio_de_json(arquivo.read_text(encoding="utf-8"))
    assert gravado.escala == 10
    # Sem o conjunto congelado: o rótulo não é "v1", e `comparar` não o confunde com a base.
    assert gravado.versao_conjunto == "nao-congelado-escala-10"
    assert "Escala dos dados: 10" in fala.getvalue()


def test_escala_1_mantem_o_conjunto_congelado(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []
    fala = io.StringIO()
    main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(chamadas),
        fabrica_modelo=_fabrica,
    )

    arquivo = next((tmp_path / "saida").glob("avaliacao-*.json"))
    gravado = relatorio_de_json(arquivo.read_text(encoding="utf-8"))
    assert gravado.escala == 1
    assert gravado.versao_conjunto == "v1"
    assert "Escala dos dados" not in fala.getvalue()


def test_salvar_linha_de_base_e_recusada_com_escala_diferente_de_1(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []
    fala = io.StringIO()
    raiz = tmp_path / "repo"
    raiz.mkdir()
    codigo = main(
        _argumentos(tmp_path / "saida", "--escala", "10", "--salvar-linha-de-base", "base-x"),
        saida=fala,
        executar=_executor_falso(chamadas),
        fabrica_modelo=_fabrica,
        raiz=raiz,
        obter_procedencia=lambda _raiz: ("a" * 40, False),
    )

    assert codigo == CODIGO_USO
    assert "linha de base exige a escala 1" in fala.getvalue()
    assert chamadas == []
    assert not (raiz / "evals").exists()


def test_comparar_recusa_escalas_diferentes(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    novo = tmp_path / "novo.json"
    base.write_text(relatorio_para_json(_relatorio()), encoding="utf-8")
    novo_relatorio = _relatorio().model_copy(update={"escala": 10, "linhas_pedidos": 600})
    novo.write_text(relatorio_para_json(novo_relatorio), encoding="utf-8")

    fala = io.StringIO()
    codigo = main(["comparar", str(base), str(novo)], saida=fala)

    assert codigo == CODIGO_USO
    assert "escalas diferentes (1 e 10)" in fala.getvalue()


def test_relatorio_registra_escala_e_linhas_de_pedidos() -> None:
    rodadas = [RodadaCaso(numero=1, classificacao=Classificacao.ERRO_INFRA, detalhe="x")]
    relatorio = agregar(
        [_caso()],
        {"consulta-01": rodadas},
        semente=42,
        rodadas_por_caso=1,
        escala=100,
        linhas_pedidos=6000,
    )

    ida_e_volta = relatorio_de_json(relatorio_para_json(relatorio))
    assert ida_e_volta.escala == 100
    assert ida_e_volta.linhas_pedidos == 6000


def test_relatorio_antigo_sem_escala_carrega_como_escala_1() -> None:
    dados = json.loads(relatorio_para_json(_relatorio()))
    dados.pop("escala")
    dados.pop("linhas_pedidos")

    antigo = relatorio_de_json(json.dumps(dados))
    assert antigo.escala == 1
    assert antigo.linhas_pedidos is None


def test_selecao_sem_modelo_funciona_com_escala_grande(tmp_path: Path) -> None:
    fala = io.StringIO()
    codigo = main(
        ["--categoria", "geracao", "--escala", "100", "--saida", str(tmp_path / "saida")],
        saida=fala,
        fabrica_modelo=_fabrica,
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_OK
    assert "Nenhum modelo foi chamado" in texto
    assert "Pendentes selecionados: 5" in texto
    assert not (tmp_path / "saida").exists()


def test_parser_aceita_escala_1000_e_recusa_1001() -> None:
    assert _parser().parse_args(["--escala", "1000"]).escala == 1000
    with pytest.raises(SystemExit):
        _parser().parse_args(["--escala", "1001"])
