"""Testes do modo escala dos gráficos (T18c): detecção, cálculo por rodada executada,
eixo X logarítmico, os quatro SVGs de escala, o SVG combinado de quatro painéis e `gerar_todos`.
"""

import io
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import CODIGO_OK, main
from harness.evals.casos import Caso, Categoria, CitaFontes, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.graficos import (
    COR_FUNDO,
    COR_JSON_AGREGACAO,
    NOME_COMPARACAO,
    NOME_ESCALA_PAINEIS,
    NOME_PAGINA,
    NOMES_ARQUIVOS,
    NOMES_ESCALA,
    PALETA,
    SECAO_COMPARACAO,
    SECAO_ESCALA,
    SECAO_ESCALA_PAINEIS,
    SECOES,
    DadosEvolucao,
    Execucao,
    dados_de_relatorios,
    gerar_pagina_html,
    gerar_todos,
    grafico_escala_custo,
    grafico_escala_paineis,
    grafico_escala_pico_contexto,
    grafico_escala_taxa,
    grafico_escala_tokens,
    tem_modo_escala,
)
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    RodadaCaso,
    agregar,
    relatorio_para_json,
)

NS: str = "{http://www.w3.org/2000/svg}"
XMLNS: str = "http://www.w3.org/2000/svg"
FONTE = "vendas/pedidos_2026-10.xlsx"


def _caso(identificador: str) -> Caso:
    return Caso(
        id=identificador,
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o total?",
        situacao=Situacao(ativo=True),
        gabarito=[CitaFontes(fontes=[FONTE])],
    )


def _veredito(identificador: str, aprovado: bool) -> Veredito:
    regra = ResultadoRegra(
        descricao="cita a fonte",
        passou=aprovado,
        detalhe="ok" if aprovado else "não citou",
    )
    return Veredito(caso_id=identificador, aprovado=aprovado, regras=(regra,))


def _valida(
    numero: int,
    *,
    aprovada: bool = True,
    entrada: int = 1000,
    custo: float | None = 0.001,
) -> RodadaCaso:
    return RodadaCaso(
        numero=numero,
        classificacao=Classificacao.APROVADA if aprovada else Classificacao.REPROVADA,
        veredito=_veredito("consulta-01", aprovada),
        iteracoes=2,
        tokens_entrada=entrada,
        tokens_saida=100,
        custo=custo,
        tokens_entrada_por_turno=(entrada,),
        pico_contexto=entrada,
        caracteres_observacao=50,
    )


def _erro(numero: int, *, entrada: int = 500, custo: float | None = 0.0005) -> RodadaCaso:
    return RodadaCaso(
        numero=numero,
        classificacao=Classificacao.ERRO_INFRA,
        tokens_entrada=entrada,
        custo=custo,
        detalhe="RuntimeError",
    )


def _nao_executada(numero: int) -> RodadaCaso:
    return RodadaCaso(
        numero=numero,
        classificacao=Classificacao.NAO_EXECUTADA,
        detalhe="teto",
    )


def _relatorio(
    rodadas: list[RodadaCaso],
    *,
    variante: str = "repl",
    linhas_pedidos: int | None = None,
) -> Relatorio:
    return agregar(
        [_caso("consulta-01")],
        {"consulta-01": rodadas},
        semente=42,
        rodadas_por_caso=len(rodadas),
        variante=variante,  # type: ignore[arg-type]
        linhas_pedidos=linhas_pedidos,
    )


def _gravar(relatorio: Relatorio, caminho: Path) -> Path:
    caminho.write_text(relatorio_para_json(relatorio), encoding="utf-8")
    return caminho


def _exec(numero: int, variante: str, linhas_pedidos: int | None, **campos: Any) -> Execucao:
    base: dict[str, Any] = {
        "numero": numero,
        "variante": variante,
        "relatorio": f"{variante}-{numero}.json",
        "casos": 1,
        "rodadas": 5,
        "rodadas_validas": 5,
        "aprovadas": 4,
        "taxa_pct": 80.0,
        "erro_infra_pct": 0.0,
        "iteracoes_media": 3.0,
        "tokens_entrada_por_rodada": 10000,
        "tokens_entrada_por_rodada_executada": 10000.0,
        "custo_por_rodada_usd": 0.002,
        "custo_por_rodada_executada_usd": 0.002,
        "pico_contexto_medio": 4000.0,
        "linhas_pedidos": linhas_pedidos,
    }
    base.update(campos)
    return Execucao(**base)


def _dados_escala() -> DadosEvolucao:
    return DadosEvolucao(
        versao=1,
        execucoes=(
            _exec(
                1,
                "repl",
                60,
                tokens_entrada_por_rodada_executada=8000.0,
                custo_por_rodada_executada_usd=0.001,
                pico_contexto_medio=3000.0,
                taxa_pct=90.0,
            ),
            _exec(
                2,
                "repl",
                600,
                tokens_entrada_por_rodada_executada=12000.0,
                custo_por_rodada_executada_usd=0.002,
                pico_contexto_medio=4500.0,
                taxa_pct=85.0,
            ),
            _exec(
                3,
                "repl",
                6000,
                tokens_entrada_por_rodada_executada=18000.0,
                custo_por_rodada_executada_usd=0.003,
                pico_contexto_medio=6000.0,
                taxa_pct=80.0,
            ),
            _exec(
                1,
                "json",
                60,
                tokens_entrada_por_rodada_executada=10000.0,
                custo_por_rodada_executada_usd=0.0015,
                pico_contexto_medio=5000.0,
                taxa_pct=85.0,
            ),
            _exec(
                2,
                "json",
                600,
                tokens_entrada_por_rodada_executada=40000.0,
                custo_por_rodada_executada_usd=0.006,
                pico_contexto_medio=25000.0,
                taxa_pct=60.0,
            ),
            _exec(
                3,
                "json",
                6000,
                tokens_entrada_por_rodada_executada=90000.0,
                custo_por_rodada_executada_usd=0.015,
                pico_contexto_medio=65000.0,
                taxa_pct=30.0,
            ),
            _exec(
                1,
                "json-agregacao",
                60,
                tokens_entrada_por_rodada_executada=9000.0,
                custo_por_rodada_executada_usd=0.0012,
                pico_contexto_medio=3500.0,
                taxa_pct=88.0,
            ),
            _exec(
                2,
                "json-agregacao",
                600,
                tokens_entrada_por_rodada_executada=14000.0,
                custo_por_rodada_executada_usd=0.0022,
                pico_contexto_medio=5500.0,
                taxa_pct=82.0,
            ),
        ),
    )


def _raiz(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _titulos(svg: str) -> list[str]:
    return [el.text or "" for el in _raiz(svg).iter(f"{NS}title") if el.get("id") is None]


def _textos(svg: str) -> list[str]:
    return ["".join(el.itertext()) for el in _raiz(svg).iter(f"{NS}text")]


# --- detecção e cálculo por rodada executada -------------------------------------------------


def test_tem_modo_escala_exige_ao_menos_uma_variante_com_duas_escalas() -> None:
    assert not tem_modo_escala(DadosEvolucao(versao=1, execucoes=(_exec(1, "repl", None),)))
    assert not tem_modo_escala(
        DadosEvolucao(versao=1, execucoes=(_exec(1, "repl", 60), _exec(1, "json", 600)))
    )
    assert tem_modo_escala(
        DadosEvolucao(versao=1, execucoes=(_exec(1, "repl", 60), _exec(2, "repl", 600)))
    )


def test_tokens_e_custo_por_rodada_executada_ignoram_nao_executadas(tmp_path: Path) -> None:
    # 2 aprovadas + 1 reprovada + 1 erro_infra = 4 executadas; 2 nao_executadas ficam de fora.
    rodadas = [
        _valida(1, entrada=1000, custo=0.001),
        _valida(2, aprovada=False, entrada=2000, custo=0.003),
        _valida(3, entrada=3000, custo=0.002),
        _erro(4, entrada=2000, custo=0.002),
        _nao_executada(5),
        _nao_executada(6),
    ]
    rel = _relatorio(rodadas, variante="repl", linhas_pedidos=600)
    caminho = _gravar(rel, tmp_path / "r.json")
    (execucao,) = dados_de_relatorios([caminho]).execucoes
    assert execucao.linhas_pedidos == 600
    # Total tokens = 8000 / 4 executadas = 2000.0 (não 8000/6)
    assert execucao.tokens_entrada_por_rodada_executada == pytest.approx(2000.0)
    # Total custo = 0.008 / 4 executadas = 0.002 (não 0.008/6)
    assert execucao.custo_por_rodada_executada_usd == pytest.approx(0.002)


# --- eixo X logarítmico e quatro gráficos ---------------------------------------------------


def test_eixo_x_logaritmico_com_marcas_nos_valores_presentes() -> None:
    svg = grafico_escala_tokens(_dados_escala())
    raiz = _raiz(svg)
    textos = _textos(svg)
    assert "60" in textos
    assert "600" in textos
    assert "6.000" in textos
    assert "Linhas de pedidos (escala logarítmica)" in textos

    x_por_rotulo = {
        el.text: float(el.get("x", "0"))
        for el in raiz.iter(f"{NS}text")
        if el.text in ("60", "600", "6.000")
    }
    passo_1 = x_por_rotulo["600"] - x_por_rotulo["60"]
    passo_2 = x_por_rotulo["6.000"] - x_por_rotulo["600"]
    assert passo_1 > 0
    assert passo_1 == pytest.approx(passo_2, abs=0.1)


@pytest.mark.parametrize(
    ("fn", "trecho_titulo", "trecho_dica"),
    [
        (
            grafico_escala_tokens,
            "Tokens de entrada por rodada por escala",
            "mil tokens de entrada por rodada",
        ),
        (
            grafico_escala_custo,
            "Custo por rodada por escala",
            "milésimos de US$ por rodada",
        ),
        (
            grafico_escala_pico_contexto,
            "Pico de contexto médio por escala",
            "tokens de pico de contexto",
        ),
        (grafico_escala_taxa, "Taxa de acerto por escala", "de acerto"),
    ],
)
def test_quatro_graficos_de_escala_estilo_cores_tooltips_e_sem_externos(
    fn: Any, trecho_titulo: str, trecho_dica: str
) -> None:
    dados = _dados_escala()
    svg = fn(dados)
    raiz = _raiz(svg)
    assert raiz.get("role") == "img"
    assert f'fill="{COR_FUNDO}"' in svg
    assert trecho_titulo in _textos(svg)
    assert "<script" not in svg
    assert "javascript:" not in svg
    assert "http://" not in svg.replace(XMLNS, "")
    assert "https://" not in svg
    assert fn(dados) == svg

    titulos = _titulos(svg)
    # 3 escalas repl + 3 escalas json + 2 escalas json-agregacao = 8 pontos
    assert len(titulos) == 8
    assert all(trecho_dica in t for t in titulos)

    # Cores das variantes presentes nas linhas
    caminhos = [el.get("stroke") for el in raiz.iter(f"{NS}path")]
    assert PALETA[0] in caminhos
    assert PALETA[1] in caminhos
    assert COR_JSON_AGREGACAO in caminhos


def test_grafico_escala_usa_execucao_mais_recente_quando_repetida_na_mesma_escala() -> None:
    dados = DadosEvolucao(
        versao=1,
        execucoes=(
            _exec(1, "repl", 60, taxa_pct=10.0),
            _exec(2, "repl", 60, taxa_pct=90.0),
            _exec(3, "repl", 600, taxa_pct=80.0),
        ),
    )
    titulos = _titulos(grafico_escala_taxa(dados))
    assert len(titulos) == 2
    assert any("90,0%" in t for t in titulos)
    assert not any("10,0%" in t for t in titulos)


def test_grafico_escala_sem_duas_escalas_devolve_sem_dados() -> None:
    dados = DadosEvolucao(versao=1, execucoes=(_exec(1, "repl", 60),))
    svg = grafico_escala_tokens(dados)
    _raiz(svg)
    assert "sem dados" in _textos(svg)


# --- SVG combinado de 4 painéis -------------------------------------------------------------


def test_escala_paineis_tem_quatro_paineis_e_eixo_log_em_cada() -> None:
    dados = _dados_escala()
    svg = grafico_escala_paineis(dados)
    raiz = _raiz(svg)
    assert raiz.get("role") == "img"
    assert f'fill="{COR_FUNDO}"' in svg
    titulos_paineis = [
        el.text
        for el in raiz.iter(f"{NS}text")
        if el.get("font-size") == "13" and el.get("font-weight") == "500"
    ]
    assert titulos_paineis == [
        "Tokens de entrada por rodada (mil)",
        "Custo por rodada (milésimos de US$)",
        "Pico de contexto médio (tokens)",
        "Taxa de acerto (%)",
    ]
    # 8 pontos por painel * 4 painéis = 32 tooltips
    assert len(_titulos(svg)) == 32
    assert "<script" not in svg
    assert "https://" not in svg
    assert grafico_escala_paineis(dados) == svg


# --- gerar_todos, página HTML e CLI ---------------------------------------------------------


def test_gerar_todos_e_pagina_incluem_escala_apenas_quando_ativa(tmp_path: Path) -> None:
    sem_escala = DadosEvolucao(versao=1, execucoes=(_exec(1, "repl", 60), _exec(1, "json", 60)))
    pasta_a = tmp_path / "sem"
    caminhos_a = gerar_todos(sem_escala, pasta_a)
    assert [c.name for c in caminhos_a] == [
        "dispersao-iteracoes-tokens.svg",
        "curva-de-contexto.svg",
        NOME_COMPARACAO,
        NOME_PAGINA,
    ]
    assert not (pasta_a / NOME_ESCALA_PAINEIS).exists()

    com_escala = _dados_escala()
    pasta_b = tmp_path / "com"
    caminhos_b = gerar_todos(com_escala, pasta_b)
    assert [c.name for c in caminhos_b] == [
        *NOMES_ESCALA,
        NOME_ESCALA_PAINEIS,
        NOME_PAGINA,
    ]
    for nome in (*NOMES_ESCALA, NOME_ESCALA_PAINEIS):
        _raiz((pasta_b / nome).read_text(encoding="utf-8"))
    for nome in (*NOMES_ARQUIVOS, NOME_COMPARACAO):
        assert not (pasta_b / nome).exists()

    pagina = gerar_pagina_html(com_escala)
    assert pagina.count("<section>") == 1
    assert f"<h2>{SECAO_ESCALA[0]}</h2>" in pagina
    assert "<h2>Escala dos dados</h2>" in pagina
    assert f"<h3>{SECAO_ESCALA_PAINEIS[0]}</h3>" in pagina
    assert pagina.count("<svg ") == 5
    assert f"<h2>{SECAO_COMPARACAO[0]}</h2>" not in pagina
    for _, titulo, _ in SECOES:
        assert f"<h2>{titulo}</h2>" not in pagina
    ids = re.findall(r'\sid="([^"]+)"', pagina)
    assert len(ids) == len(set(ids))
    assert "tep" in ids and "dep" in ids
    for i in range(1, 5):
        assert f"te{i}" in ids and f"de{i}" in ids
    assert "tc" not in ids and "t1" not in ids


def test_pagina_html_escala_csp_interatividade_e_escape() -> None:
    dados = DadosEvolucao(
        versao=1,
        descricao='Teste <escala> & "aspas"',
        execucoes=(
            _exec(1, 'var<A>&"1"', 60),
            _exec(
                2,
                'var<A>&"1"',
                600,
                incompleta=True,
                motivo_incompleta='motivo <especial> & "teste"',
            ),
        ),
    )
    pagina = gerar_pagina_html(dados)
    assert (
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
    ) in pagina
    assert "pointerover" in pagina and "focusin" in pagina
    assert 'tabindex="0"' in pagina
    assert "Teste &lt;escala&gt; &amp; &quot;aspas&quot;" in pagina
    assert "var&lt;A&gt;&amp;&quot;1&quot;" in pagina
    assert "motivo &lt;especial&gt; &amp; &quot;teste&quot;" in pagina
    assert 'var<A>&"1"' not in pagina
    assert 'motivo <especial> & "teste"' not in pagina


def test_cli_de_relatorios_gera_graficos_de_escala(tmp_path: Path) -> None:
    a = _gravar(
        _relatorio([_valida(1, entrada=1000, custo=0.001)], variante="repl", linhas_pedidos=60),
        tmp_path / "a.json",
    )
    b = _gravar(
        _relatorio([_valida(1, entrada=2000, custo=0.002)], variante="repl", linhas_pedidos=600),
        tmp_path / "b.json",
    )
    c = _gravar(
        _relatorio([_valida(1, entrada=4000, custo=0.004)], variante="json", linhas_pedidos=60),
        tmp_path / "c.json",
    )
    d = _gravar(
        _relatorio([_valida(1, entrada=8000, custo=0.008)], variante="json", linhas_pedidos=600),
        tmp_path / "d.json",
    )
    buf = io.StringIO()
    codigo = main(
        [
            "graficos",
            "--de-relatorios",
            str(a),
            str(b),
            str(c),
            str(d),
            "--saida",
            str(tmp_path / "saida"),
        ],
        saida=buf,
        raiz=tmp_path,
    )
    assert codigo == CODIGO_OK
    for nome in (*NOMES_ESCALA, NOME_ESCALA_PAINEIS):
        assert (tmp_path / "saida" / nome).exists()
    for nome in (*NOMES_ARQUIVOS, NOME_COMPARACAO):
        assert not (tmp_path / "saida" / nome).exists()
    # 4 escala + 1 painéis escala + 1 html = 6 arquivos
    linhas = [x for x in buf.getvalue().splitlines() if x.startswith("Gráfico gerado:")]
    assert len(linhas) == 6


def test_escala_e_paineis_sinalizam_execucao_incompleta() -> None:
    motivo = "execução incompleta: teto de gasto estourado; 4 rodadas não executadas"
    dados = DadosEvolucao(
        versao=1,
        execucoes=(
            _exec(1, "repl", 60),
            _exec(2, "repl", 600, incompleta=True, motivo_incompleta=motivo),
        ),
    )
    for fn in (grafico_escala_tokens, grafico_escala_paineis):
        svg = fn(dados)
        raiz = _raiz(svg)
        vazados = [
            el
            for el in raiz.iter(f"{NS}circle")
            if el.find(f"{NS}title") is not None
            and motivo in (el.find(f"{NS}title").text or "")  # type: ignore[union-attr]
        ]
        assert vazados
        assert all(el.get("fill") == "#ffffff" for el in vazados)
        assert "marcador vazado: execução 2 incompleta" in _textos(svg)
