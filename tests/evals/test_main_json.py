"""Testes da CLI com `--variante` (T18a). Nada aqui chama o modelo: o executor é falso."""

import io
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import main
from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import Classificacao, Relatorio, RodadaCaso, agregar
from harness.llm.base import LLMFalso


def _caso() -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
        comportamento_esperado=None,
    )


def _relatorio(variante: str, *, rodadas: int = 3) -> Relatorio:
    regra = ResultadoRegra(descricao="regra", passou=True, detalhe="ok")
    veredito = Veredito(caso_id="consulta-01", aprovado=True, regras=(regra,))
    lista = [
        RodadaCaso(
            numero=n,
            classificacao=Classificacao.APROVADA,
            veredito=veredito,
            custo=0.01,
            tokens_entrada=300,
            tokens_saida=20,
            tokens_entrada_por_turno=(100, 200),
            pico_contexto=200,
            caracteres_observacao=40,
        )
        for n in range(1, rodadas + 1)
    ]
    return agregar(
        [_caso()],
        {"consulta-01": lista},
        semente=42,
        rodadas_por_caso=rodadas,
        variante=variante,  # type: ignore[arg-type]
        ferramentas_json="basicas+agregacao" if variante != "repl" else None,
        hash_ferramentas="c" * 64 if variante != "repl" else None,
    )


def _executor(relatorio: Relatorio, capturadas: list[Any]) -> Callable[..., Relatorio]:
    def falso(
        casos: Callable[..., Any],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
        **_extra: Any,
    ) -> Relatorio:
        capturadas.append(config)
        return relatorio

    return falso


def _rodar(
    tmp_path: Path,
    capturadas: list[Any],
    relatorio: Relatorio,
    *argumentos: str,
) -> tuple[int, str]:
    saida = io.StringIO()
    codigo = main(
        [
            "--rodadas",
            "3",
            "--saida",
            str(tmp_path / "saida"),
            "--trace",
            str(tmp_path / "trace"),
            *argumentos,
        ],
        saida=saida,
        executar=_executor(relatorio, capturadas),
        fabrica_modelo=lambda: LLMFalso([]),
        raiz=tmp_path,
        obter_procedencia=lambda _raiz: ("abc123", False),
    )
    return codigo, saida.getvalue()


def test_variante_json_chega_a_configuracao_da_execucao(tmp_path: Path) -> None:
    capturadas: list[Any] = []
    codigo, _saida = _rodar(tmp_path, capturadas, _relatorio("json"), "--variante", "json")
    assert codigo == 0
    assert capturadas[0].variante == "json"


def test_variante_padrao_e_repl(tmp_path: Path) -> None:
    capturadas: list[Any] = []
    codigo, _saida = _rodar(tmp_path, capturadas, _relatorio("repl"))
    assert codigo == 0
    assert capturadas[0].variante == "repl"


def test_variante_json_agregacao_e_aceita(tmp_path: Path) -> None:
    capturadas: list[Any] = []
    codigo, _saida = _rodar(
        tmp_path, capturadas, _relatorio("json-agregacao"), "--variante", "json-agregacao"
    )
    assert codigo == 0
    assert capturadas[0].variante == "json-agregacao"


def test_variante_desconhecida_sai_com_dois_sem_executar(tmp_path: Path) -> None:
    capturadas: list[Any] = []
    codigo, _saida = _rodar(tmp_path, capturadas, _relatorio("repl"), "--variante", "xpto")
    assert codigo == 2
    assert capturadas == []


def test_resumo_mostra_variante_pico_caracteres_e_custo_por_acerto(tmp_path: Path) -> None:
    capturadas: list[Any] = []
    _codigo, saida = _rodar(tmp_path, capturadas, _relatorio("json"), "--variante", "json")
    assert "Variante: json" in saida
    assert "Pico de contexto médio: 200 tokens por chamada" in saida
    assert "Caracteres de observação médios por rodada: 40" in saida
    assert "Custo por acerto: US$ 0,0100" in saida


def test_resumo_sem_metricas_nao_mostra_as_linhas_novas(tmp_path: Path) -> None:
    capturadas: list[Any] = []
    antigo = _relatorio("repl").model_copy(
        update={
            "pico_contexto_medio": None,
            "caracteres_observacao_medio": None,
            "custo_por_acerto": None,
        }
    )
    _codigo, saida = _rodar(tmp_path, capturadas, antigo)
    assert "Pico de contexto" not in saida
    assert "Custo por acerto" not in saida


def test_linha_de_base_com_variante_json_exige_provedor_antes_de_rodar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_PROVEDOR", raising=False)
    capturadas: list[Any] = []
    codigo, _saida = _rodar(
        tmp_path,
        capturadas,
        _relatorio("json"),
        "--variante",
        "json",
        "--salvar-linha-de-base",
        "teste-json",
    )
    assert codigo == 2
    assert capturadas == []


def test_linha_de_base_com_variante_json_e_provedor_fixo_grava_o_arquivo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_PROVEDOR", raising=False)
    capturadas: list[Any] = []
    codigo, saida = _rodar(
        tmp_path,
        capturadas,
        _relatorio("json"),
        "--variante",
        "json",
        "--provedor",
        "Wafer",
        "--salvar-linha-de-base",
        "teste-json",
    )
    assert codigo == 0, saida
    assert "Linha de base gravada" in saida
    gravadas = list((tmp_path / "evals" / "linhas-de-base").glob("teste-json.json"))
    assert len(gravadas) == 1
