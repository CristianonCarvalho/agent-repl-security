"""Testes dos gráficos com as variantes JSON (T18a).

O ponto mais importante: os arquivos da variante REPL continuam idênticos, byte a byte, aos que já
estão em docs/relatorios/graficos, gerados de docs/relatorios/dados/evolucao-execucoes.json.
"""

import hashlib
from pathlib import Path

from harness.evals.graficos import (
    COR_JSON_AGREGACAO,
    NOMES_ARQUIVOS,
    PALETA,
    ROTULOS_VARIANTE,
    DadosEvolucao,
    Execucao,
    _cores_variantes,
    carregar_dados,
    gerar_todos,
)

RAIZ = Path(__file__).resolve().parents[2]
DADOS_DE_DOCS = RAIZ / "docs" / "relatorios" / "dados" / "evolucao-execucoes.json"
GRAFICOS_DE_DOCS = RAIZ / "docs" / "relatorios" / "graficos"


def _sha256(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()


def test_svgs_da_variante_repl_seguem_identicos_aos_de_docs(tmp_path: Path) -> None:
    dados = carregar_dados(DADOS_DE_DOCS)
    gerados = gerar_todos(dados, tmp_path)
    assert gerados, "nenhum arquivo foi gerado"
    for caminho in gerados:
        referencia = GRAFICOS_DE_DOCS / caminho.name
        assert referencia.exists(), caminho.name
        assert _sha256(caminho) == _sha256(referencia), caminho.name


def test_rotulos_e_cores_das_variantes_novas() -> None:
    assert ROTULOS_VARIANTE["json"] == "Tool calling em JSON"
    assert ROTULOS_VARIANTE["json-agregacao"] == "Tool calling em JSON, com agregação"
    cores = _cores_variantes(["repl", "json", "json-agregacao"])
    assert cores == {
        "repl": PALETA[0],
        "json": PALETA[1],
        "json-agregacao": COR_JSON_AGREGACAO,
    }
    assert len(set(cores.values())) == 3


def _execucao(numero: int, variante: str, taxa: float) -> Execucao:
    return Execucao(
        numero=numero,
        variante=variante,
        relatorio=f"avaliacao-{variante}-{numero}.json",
        casos=20,
        rodadas=60,
        rodadas_validas=60,
        aprovadas=round(taxa * 60 / 100),
        taxa_pct=taxa,
        erro_infra_pct=0.0,
        iteracoes_media=4.0,
        tokens_entrada_por_rodada=5000,
        tokens_saida_por_rodada=300,
        custo_por_rodada_usd=0.001,
        prompt="abc",
    )


def test_tres_variantes_no_mesmo_eixo_com_rotulos_e_cores_proprios(tmp_path: Path) -> None:
    dados = DadosEvolucao(
        versao=1,
        execucoes=(
            _execucao(1, "repl", 80.0),
            _execucao(1, "json", 60.0),
            _execucao(1, "json-agregacao", 70.0),
        ),
    )
    gerados = gerar_todos(dados, tmp_path)
    taxa = (tmp_path / "taxa-total.svg").read_text(encoding="utf-8")
    for rotulo in ROTULOS_VARIANTE.values():
        assert rotulo in taxa or rotulo in (tmp_path / "graficos.html").read_text(encoding="utf-8")
    assert COR_JSON_AGREGACAO in taxa
    # Seis SVG, a comparação (com três variantes) e a página.
    assert len(gerados) == len(NOMES_ARQUIVOS) + 2


def test_variante_com_curva_de_contexto_aparece_na_pagina(tmp_path: Path) -> None:
    from harness.evals.graficos import CurvaDeContexto

    dados = DadosEvolucao(
        versao=1,
        execucoes=(_execucao(1, "json-agregacao", 70.0),),
        curvas_de_contexto=(
            CurvaDeContexto(
                nome="json-agregacao 1",
                variante="json-agregacao",
                pontos=((1, 100), (2, 300)),
                rodadas_por_ponto_minimo=10,
            ),
        ),
    )
    gerar_todos(dados, tmp_path)
    pagina = (tmp_path / "graficos.html").read_text(encoding="utf-8")
    assert ROTULOS_VARIANTE["json-agregacao"] in pagina
