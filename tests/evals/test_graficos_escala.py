"""Testes do modo escala dos gráficos (T18c): detecção, cálculo por rodada executada,
eixo X logarítmico, os quatro SVGs de escala, o SVG combinado de quatro painéis e `gerar_todos`.
"""

import io
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
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
    CasoDetalhe,
    DadosEvolucao,
    Execucao,
    _painel_interativo_html,
    carregar_dados,
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
from harness.evals.procedencia import RAIZ_DO_REPOSITORIO
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


def test_grafico_escala_repetida_usa_iniciado_em_mesmo_em_ordem_inversa(tmp_path: Path) -> None:
    mais_novo = _relatorio(
        [_valida(1, aprovada=True)], variante="repl", linhas_pedidos=60
    ).model_copy(update={"iniciado_em": "2026-11-24T10:00:00+00:00"})
    mais_antigo = _relatorio(
        [_valida(1, aprovada=False)], variante="repl", linhas_pedidos=60
    ).model_copy(update={"iniciado_em": "2026-11-23T10:00:00+00:00"})
    outra_escala = _relatorio(
        [_valida(1, aprovada=True)], variante="repl", linhas_pedidos=600
    ).model_copy(update={"iniciado_em": "2026-11-23T12:00:00+00:00"})

    # O mais novo vem antes do mais antigo na lista de arquivos (numero=1 < numero=2).
    dados = dados_de_relatorios(
        [
            _gravar(mais_novo, tmp_path / "novo.json"),
            _gravar(mais_antigo, tmp_path / "antigo.json"),
            _gravar(outra_escala, tmp_path / "escala600.json"),
        ]
    )
    assert dados.execucoes[0].iniciado_em == "2026-11-24T10:00:00+00:00"
    titulos = _titulos(grafico_escala_taxa(dados))
    assert len(titulos) == 2
    assert all(": 100,0%" in t for t in titulos)
    assert not any(": 0,0%" in t for t in titulos)


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
    assert pagina.count("<section") == 2
    pos_painel = pagina.index("<h2>Painel interativo</h2>")
    pos_escala = pagina.index(f"<h2>{SECAO_ESCALA[0]}</h2>")
    assert pos_painel < pos_escala
    assert 'id="dados-painel"' in pagina
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
        "content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">"
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
            if el.find(f"{NS}title") is not None and motivo in (el.find(f"{NS}title").text or "")  # type: ignore[union-attr]
        ]
        assert vazados
        assert all(el.get("fill") == "#ffffff" for el in vazados)
        assert "marcador vazado: execução 2 incompleta" in _textos(svg)


class _ValidadorHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.svgs = 0
        self.scripts = 0
        self.estilos = 0
        self.csp: list[str | None] = []
        self.pilha: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapa = dict(attrs)
        if tag == "svg":
            self.svgs += 1
        elif tag == "script":
            self.scripts += 1
        elif tag == "style":
            self.estilos += 1
        elif tag == "meta" and mapa.get("http-equiv") == "Content-Security-Policy":
            self.csp.append(mapa.get("content"))
        if tag not in {"meta", "br", "hr", "img", "input", "link"}:
            self.pilha.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.pilha and self.pilha[-1] == tag:
            self.pilha.pop()


def test_modo_escala_com_relatorios_sinteticos_tres_escalas_e_tres_variantes(
    tmp_path: Path,
) -> None:
    # Escalas 1 (60 linhas), 10 (600 linhas), 100 (6000 linhas) para as três variantes.
    caminhos: list[Path] = []
    escala_por_fator = {1: 60, 10: 600, 100: 6000}
    variantes = ("repl", "json", "json-agregacao")
    for idx_v, variante in enumerate(variantes, 1):
        for fator, linhas in escala_por_fator.items():
            incompleta = variante == "json" and fator == 100
            rodadas = [
                _valida(1, aprovada=True, entrada=1000 * fator * idx_v, custo=0.001 * fator),
                (
                    _nao_executada(2)
                    if incompleta
                    else _valida(
                        2, aprovada=fator < 100, entrada=1200 * fator, custo=0.0012 * fator
                    )
                ),
            ]
            rel = agregar(
                [_caso("consulta-01")],
                {"consulta-01": rodadas},
                semente=42,
                rodadas_por_caso=len(rodadas),
                variante=variante,  # type: ignore[arg-type]
                linhas_pedidos=linhas,
                teto_motivo="teto de gasto estourado" if incompleta else None,
            )
            caminhos.append(_gravar(rel, tmp_path / f"{variante}-x{fator}.json"))

    dados = dados_de_relatorios(caminhos)
    assert tem_modo_escala(dados)

    pasta_a = tmp_path / "saida_a"
    pasta_b = tmp_path / "saida_b"
    gerados_a = gerar_todos(dados, pasta_a)
    gerados_b = gerar_todos(dados, pasta_b)
    assert [c.name for c in gerados_a] == [*NOMES_ESCALA, NOME_ESCALA_PAINEIS, NOME_PAGINA]
    # Determinismo: duas gerações produzem exatamente os mesmos bytes.
    for a, b in zip(gerados_a, gerados_b, strict=True):
        assert a.read_bytes() == b.read_bytes()

    # Eixo X logarítmico com marcas exatamente em 60, 600 e 6.000.
    raiz_tokens = _raiz((pasta_a / "escala-tokens-por-rodada.svg").read_text(encoding="utf-8"))
    x_por_rotulo = {
        el.text: float(el.get("x", "0"))
        for el in raiz_tokens.iter(f"{NS}text")
        if el.text in ("60", "600", "6.000")
    }
    assert set(x_por_rotulo) == {"60", "600", "6.000"}
    assert x_por_rotulo["600"] - x_por_rotulo["60"] == pytest.approx(
        x_por_rotulo["6.000"] - x_por_rotulo["600"], abs=0.1
    )

    # Painel combinado com 9 pontos * 4 painéis = 36 tooltips e marcador vazado na incompleta.
    paineis_svg = (pasta_a / NOME_ESCALA_PAINEIS).read_text(encoding="utf-8")
    assert len(_titulos(paineis_svg)) == 36
    vazados = [
        el
        for el in _raiz(paineis_svg).iter(f"{NS}circle")
        if el.find(f"{NS}title") is not None
        and "teto de gasto estourado" in (el.find(f"{NS}title").text or "")  # type: ignore[union-attr]
    ]
    assert len(vazados) == 4
    assert all(el.get("fill") == "#ffffff" for el in vazados)

    # HTML válido com CSP inalterada e 5 SVGs inline.
    html = (pasta_a / NOME_PAGINA).read_text(encoding="utf-8")
    validador = _ValidadorHTML()
    validador.feed(html)
    assert validador.pilha == []
    assert validador.svgs == 5
    # 1: <script type="application/json" id="dados-painel">, 2: script do painel, 3: tooltips
    assert validador.scripts == 3
    assert validador.estilos == 1
    assert validador.csp == [
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
    ]


def test_relatorio_sem_escala_tratado_como_escala_1_e_linhas_60(tmp_path: Path) -> None:
    sem_escala = _gravar(
        _relatorio([_valida(1, entrada=1000, custo=0.001)], variante="repl", linhas_pedidos=None),
        tmp_path / "sem-escala.json",
    )
    so_antigo = dados_de_relatorios([sem_escala])
    assert so_antigo.execucoes[0].linhas_pedidos == 60
    assert not tem_modo_escala(so_antigo)

    escala_10 = _gravar(
        _relatorio([_valida(1, entrada=2000, custo=0.002)], variante="repl", linhas_pedidos=600),
        tmp_path / "escala-10.json",
    )
    misto = dados_de_relatorios([sem_escala, escala_10])
    assert tem_modo_escala(misto)
    textos = _textos(grafico_escala_tokens(misto))
    assert "60" in textos and "600" in textos


def test_uma_escala_so_com_uma_execucao_por_variante_nao_gera_graficos_de_linha(
    tmp_path: Path,
) -> None:
    caminhos = [
        _gravar(
            _relatorio([_valida(1)], variante=variante, linhas_pedidos=None),
            tmp_path / f"{variante}.json",
        )
        for variante in ("repl", "json", "json-agregacao")
    ]
    dados = dados_de_relatorios(caminhos)
    assert not tem_modo_escala(dados)
    gerados = gerar_todos(dados, tmp_path / "saida")
    assert [c.name for c in gerados] == [
        "dispersao-iteracoes-tokens.svg",
        "curva-de-contexto.svg",
        NOME_COMPARACAO,
        NOME_PAGINA,
    ]


def test_evolucao_execucoes_real_continua_gerando_seis_graficos(tmp_path: Path) -> None:
    caminho = RAIZ_DO_REPOSITORIO / "docs" / "relatorios" / "dados" / "evolucao-execucoes.json"
    dados = carregar_dados(caminho)
    assert not tem_modo_escala(dados)
    gerados = gerar_todos(dados, tmp_path)
    assert [c.name for c in gerados] == [*NOMES_ARQUIVOS, NOME_PAGINA]


def test_painel_interativo_escape_determinismo_e_fora_do_modo_escala() -> None:
    caso_b = CasoDetalhe(id="caso-b", categoria="b", aprovadas=1)
    caso_a = CasoDetalhe(id="caso-a", categoria="a", aprovadas=2)
    exec_a = _exec(
        1,
        'var</script><!--x-->&"1"',
        60,
        nota="nota </script><!--comentario-->",
        casos_detalhe=(caso_b, caso_a),
    )
    exec_b = _exec(
        2,
        'var</script><!--x-->&"1"',
        600,
        incompleta=True,
        motivo_incompleta="motivo </script><!--fim-->",
    )
    dados_1 = DadosEvolucao(versao=1, execucoes=(exec_a, exec_b))
    dados_2 = DadosEvolucao(versao=1, execucoes=(exec_b, exec_a))

    painel_1 = _painel_interativo_html(dados_1)
    painel_2 = _painel_interativo_html(dados_2)
    assert painel_1 == painel_2
    assert painel_1 == _painel_interativo_html(dados_1)

    m = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        painel_1,
        flags=re.DOTALL,
    )
    assert m is not None
    bruto_json = m.group(1)
    assert "</" not in bruto_json
    assert "<!--" not in bruto_json
    carregado = json.loads(bruto_json)
    assert carregado["escalas"] == [60, 600]
    assert carregado["execucoes"][0]["nota"] == "nota </script><!--comentario-->"
    assert [c["id"] for c in carregado["execucoes"][0]["casos_detalhe"]] == ["caso-a", "caso-b"]
    assert carregado["execucoes"][1]["motivo_incompleta"] == "motivo </script><!--fim-->"

    # Fora do modo escala, o painel interativo não entra na página.
    sem_escala = DadosEvolucao(versao=1, execucoes=(_exec(1, "repl", 60),))
    pagina_sem = gerar_pagina_html(sem_escala)
    assert "Painel interativo" not in pagina_sem
    assert 'id="dados-painel"' not in pagina_sem


def test_painel_escape_com_nome_de_caso_e_variante_maliciosos() -> None:
    caso_inj = CasoDetalhe(
        id="caso-</script><script>alert(1)</script>",
        categoria="consulta",
        aprovadas=1,
        reprovadas=1,
    )
    exec_a = _exec(
        1,
        "var</script><!--inj-->",
        60,
        casos_detalhe=(caso_inj,),
    )
    exec_b = _exec(
        2,
        "var</script><!--inj-->",
        600,
        casos_detalhe=(caso_inj,),
    )
    dados = DadosEvolucao(versao=1, execucoes=(exec_a, exec_b))
    html = gerar_pagina_html(dados)

    validador = _ValidadorHTML()
    validador.feed(html)
    assert validador.pilha == []
    assert validador.scripts == 3

    m = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m is not None
    bruto_json = m.group(1)
    assert "</script>" not in bruto_json.lower()
    assert "</" not in bruto_json
    assert "<!--" not in bruto_json

    carregado = json.loads(bruto_json)
    assert carregado["variantes"][0]["id"] == "var</script><!--inj-->"
    assert carregado["execucoes"][0]["casos_detalhe"][0]["id"] == (
        "caso-</script><script>alert(1)</script>"
    )


def test_painel_determinismo_pagina_escala_completa() -> None:
    caso_1 = CasoDetalhe(id="caso-01", categoria="consulta", aprovadas=2)
    caso_2 = CasoDetalhe(id="caso-02", categoria="agregacao", reprovadas=1)
    e1 = _exec(1, "repl", 60, casos_detalhe=(caso_2, caso_1))
    e2 = _exec(2, "repl", 600, casos_detalhe=(caso_1, caso_2))
    e3 = _exec(1, "json", 60)
    e4 = _exec(2, "json", 600)

    html_a = gerar_pagina_html(DadosEvolucao(versao=1, execucoes=(e1, e2, e3, e4)))
    html_b = gerar_pagina_html(DadosEvolucao(versao=1, execucoes=(e4, e2, e3, e1)))
    assert html_a == html_b


def test_pagina_fora_do_modo_escala_inalterada_regressao() -> None:
    dados = carregar_dados(
        RAIZ_DO_REPOSITORIO / "docs" / "relatorios" / "dados" / "evolucao-execucoes.json"
    )
    assert not tem_modo_escala(dados)
    atual = (RAIZ_DO_REPOSITORIO / "docs" / "relatorios" / "graficos" / NOME_PAGINA).read_text(
        encoding="utf-8"
    )
    assert gerar_pagina_html(dados) == atual


def test_painel_presenca_dos_elementos_e_ordem_no_modo_escala() -> None:
    html = gerar_pagina_html(_dados_escala())

    pos_painel = html.index('id="painel-interativo"')
    pos_escala = html.index(f"<h2>{SECAO_ESCALA[0]}</h2>")
    assert pos_painel < pos_escala

    assert 'id="painel-raiz"' in html
    assert '<script type="application/json" id="dados-painel">' in html
    assert "painel-controles" in html
    assert "painel-grupo" in html
    assert "painel-btn" in html
    assert "painel-var" in html
    assert "painel-cor" in html
    assert "painel-resumo" in html
    assert "painel-cartao" in html
    assert "painel-tabela" in html
    assert "painel-detalhe" in html
    assert "Eixo X logar\\u00edtmico" in html or "Eixo X logarítmico" in html
    assert "Eixo Y: come\\u00e7ar em zero" in html or "Eixo Y: começar em zero" in html
    assert SECAO_ESCALA_PAINEIS[0] in html


def test_execucao_campos_novos_com_padrao_para_relatorios_antigos(tmp_path: Path) -> None:
    exec_minima = Execucao(numero=1)
    assert exec_minima.reprovadas == 0
    assert exec_minima.erro_infra == 0
    assert exec_minima.nao_executadas == 0
    assert exec_minima.duracao_total_s is None
    assert exec_minima.casos_detalhe == ()

    caso_minimo = CasoDetalhe(id="c1", categoria="consulta")
    assert caso_minimo.aprovadas == 0
    assert caso_minimo.reprovadas == 0
    assert caso_minimo.erro_infra == 0
    assert caso_minimo.iteracoes_media is None
    assert caso_minimo.tokens_entrada_medio is None

    antigo_json = {
        "versao": 1,
        "execucoes": [
            {
                "numero": 1,
                "variante": "repl",
                "casos": 2,
                "rodadas": 4,
                "rodadas_validas": 4,
                "aprovadas": 3,
                "taxa_pct": 75.0,
            }
        ],
    }
    caminho = tmp_path / "antigo.json"
    caminho.write_text(json.dumps(antigo_json), encoding="utf-8")
    dados = carregar_dados(caminho)
    (exec_carregada,) = dados.execucoes
    assert exec_carregada.reprovadas == 0
    assert exec_carregada.erro_infra == 0
    assert exec_carregada.nao_executadas == 0
    assert exec_carregada.duracao_total_s is None
    assert exec_carregada.casos_detalhe == ()


def test_contagens_reprovadas_infra_nao_executadas_e_casos_detalhe(tmp_path: Path) -> None:
    caso_a = _caso("consulta-01")
    caso_b = Caso(
        id="alteracao-01",
        categoria=Categoria.ALTERACAO,
        perfil="Vendas",
        pedido="Quantos pedidos?",
        situacao=Situacao(ativo=True),
        gabarito=[CitaFontes(fontes=[FONTE])],
    )
    rodadas_a = [
        _valida(1, aprovada=True, entrada=1000).model_copy(
            update={"iteracoes": 2, "duracao_s": 1.5}
        ),
        _valida(2, aprovada=False, entrada=2000).model_copy(
            update={"iteracoes": 4, "duracao_s": 2.5}
        ),
        _erro(3, entrada=500).model_copy(update={"duracao_s": 0.5}),
    ]
    rodadas_b = [
        _valida(1, aprovada=False, entrada=3000).model_copy(
            update={"iteracoes": 6, "duracao_s": 3.0}
        ),
        _nao_executada(2),
        _nao_executada(3),
    ]
    rel = agregar(
        [caso_a, caso_b],
        {"consulta-01": rodadas_a, "alteracao-01": rodadas_b},
        semente=42,
        rodadas_por_caso=3,
        variante="repl",
        linhas_pedidos=60,
    )
    caminho = _gravar(rel, tmp_path / "sintetico.json")
    (execucao,) = dados_de_relatorios([caminho]).execucoes

    assert execucao.casos == 2
    assert execucao.rodadas == 6
    assert execucao.rodadas_validas == 3
    assert execucao.aprovadas == 1
    assert execucao.reprovadas == 2
    assert execucao.erro_infra == 1
    assert execucao.nao_executadas == 2
    assert execucao.duracao_total_s == pytest.approx(7.5)

    assert len(execucao.casos_detalhe) == 2
    det_a, det_b = execucao.casos_detalhe
    assert det_a.id == "consulta-01"
    assert det_a.categoria == "consulta"
    assert det_a.aprovadas == 1
    assert det_a.reprovadas == 1
    assert det_a.erro_infra == 1
    assert det_a.iteracoes_media == pytest.approx(3.0)
    assert det_a.tokens_entrada_medio == pytest.approx(1500.0)

    assert det_b.id == "alteracao-01"
    assert det_b.categoria == "alteracao"
    assert det_b.aprovadas == 0
    assert det_b.reprovadas == 1
    assert det_b.erro_infra == 0
    assert det_b.iteracoes_media == pytest.approx(6.0)
    assert det_b.tokens_entrada_medio == pytest.approx(3000.0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node não disponível no PATH")
def test_js_painel_e_tooltips_validos_com_node_check(tmp_path: Path) -> None:
    html = gerar_pagina_html(_dados_escala())
    scripts = re.findall(
        r"<script(?![^>]*application/json)[^>]*>(.*?)</script>",
        html,
        flags=re.DOTALL,
    )
    assert len(scripts) == 2
    for idx, codigo in enumerate(scripts):
        js_path = tmp_path / f"script_{idx}.js"
        js_path.write_text(codigo, encoding="utf-8")
        proc = subprocess.run(
            ["node", "--check", str(js_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Erro de sintaxe JS em script_{idx}: {proc.stderr}"


def test_painel_inclui_metricas_iteracoes_e_custo_por_acerto() -> None:
    e1 = _exec(1, "repl", 60, iteracoes_media=4.25, custo_por_acerto_usd=0.0125)
    e2 = _exec(2, "repl", 600, iteracoes_media=None, custo_por_acerto_usd=None)
    html = gerar_pagina_html(DadosEvolucao(versao=1, execucoes=(e1, e2)))

    m = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m is not None
    dados = json.loads(m.group(1))
    titulos = [met["titulo"] for met in dados["metricas"]]
    assert "Iterações por pedido" in titulos
    assert "Custo por acerto (milésimos de US$)" in titulos

    por_escala = {ex["escala"]: ex for ex in dados["execucoes"]}
    assert por_escala[60]["iteracoes_media"] == pytest.approx(4.25)
    assert por_escala[60]["custo_por_acerto_usd"] == pytest.approx(0.0125)
    assert por_escala[60]["valores"]["iteracoes"] == pytest.approx(4.25)
    assert por_escala[60]["valores"]["custo_acerto"] == pytest.approx(12.5)

    assert por_escala[600]["iteracoes_media"] is None
    assert por_escala[600]["custo_por_acerto_usd"] is None
    assert por_escala[600]["valores"]["iteracoes"] is None
    assert por_escala[600]["valores"]["custo_acerto"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node não disponível no PATH")
def test_painel_abas_acessiveis_e_navegacao_teclado(tmp_path: Path) -> None:
    html = gerar_pagina_html(_dados_escala())
    m_json = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m_json is not None
    scripts = re.findall(
        r"<script(?![^>]*application/json)[^>]*>(.*?)</script>",
        html,
        flags=re.DOTALL,
    )
    assert len(scripts) == 2

    harness_js = f"""
const jsonText = {json.dumps(m_json.group(1))};
function makeNode(tag, ns) {{
  const attrs = {{}};
  const listeners = {{}};
  let _text = '';
  const node = {{
    tagName: tag.toUpperCase(),
    namespaceURI: ns || null,
    className: '',
    get textContent() {{ return _text; }},
    set textContent(v) {{
      _text = String(v);
      if (_text === '') this.children = [];
    }},
    style: {{}},
    children: [],
    focused: false,
    setAttribute(k, v) {{ attrs[k] = String(v); }},
    getAttribute(k) {{ return k in attrs ? attrs[k] : null; }},
    appendChild(c) {{ this.children.push(c); return c; }},
    addEventListener(ev, fn) {{ (listeners[ev] = listeners[ev] || []).push(fn); }},
    dispatchEvent(ev) {{
      (listeners[ev.type] || []).forEach((fn) => fn(ev));
    }},
    focus() {{ node.focused = true; }},
  }};
  return node;
}}
const raiz = makeNode('div');
const elDados = {{ textContent: jsonText }};
global.document = {{
  getElementById(id) {{
    if (id === 'dados-painel') return elDados;
    if (id === 'painel-raiz') return raiz;
    return null;
  }},
  createElement(tag) {{ return makeNode(tag); }},
  createElementNS(ns, tag) {{ return makeNode(tag, ns); }},
  createTextNode(t) {{ return {{ nodeType: 3, textContent: String(t) }}; }},
}};
{scripts[0]}
const caixa = raiz.children[0];
const controles = caixa.children[0];
const tablist = controles.children[0];
if (tablist.getAttribute('role') !== 'tablist') throw new Error('missing role=tablist');
const tabs = tablist.children;
if (tabs.length !== 6) throw new Error('expected 6 tabs, got ' + tabs.length);
tabs.forEach((t, i) => {{
  if (t.getAttribute('role') !== 'tab') throw new Error('missing role=tab at ' + i);
  if (t.getAttribute('aria-selected') !== (i === 0 ? 'true' : 'false')) {{
    throw new Error('bad initial aria-selected at ' + i);
  }}
  if (t.getAttribute('tabindex') !== (i === 0 ? '0' : '-1')) {{
    throw new Error('bad initial tabindex at ' + i);
  }}
}});
function key(tab, k) {{
  let prevented = false;
  tab.dispatchEvent({{ type: 'keydown', key: k, preventDefault() {{ prevented = true; }} }});
  if (!prevented) throw new Error('expected preventDefault for ' + k);
}}
key(tabs[0], 'ArrowRight');
if (tabs[1].getAttribute('aria-selected') !== 'true' || !tabs[1].focused) {{
  throw new Error('ArrowRight failed');
}}
key(tabs[1], 'End');
if (tabs[5].getAttribute('aria-selected') !== 'true' || !tabs[5].focused) {{
  throw new Error('End failed');
}}
key(tabs[5], 'ArrowRight');
if (tabs[0].getAttribute('aria-selected') !== 'true') throw new Error('wrap ArrowRight failed');
key(tabs[0], 'ArrowLeft');
if (tabs[5].getAttribute('aria-selected') !== 'true') throw new Error('wrap ArrowLeft failed');
key(tabs[5], 'Home');
if (tabs[0].getAttribute('aria-selected') !== 'true') throw new Error('Home failed');
"""
    js_path = tmp_path / "test_tabs.js"
    js_path.write_text(harness_js, encoding="utf-8")
    proc = subprocess.run(["node", str(js_path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Falha no teste de abas JS: {proc.stderr}\n{proc.stdout}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node não disponível no PATH")
def test_painel_controles_eixo_x_log_linear_e_eixo_y_zero_propria(tmp_path: Path) -> None:
    html = gerar_pagina_html(_dados_escala())
    m_json = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m_json is not None
    scripts = re.findall(
        r"<script(?![^>]*application/json)[^>]*>(.*?)</script>",
        html,
        flags=re.DOTALL,
    )
    assert len(scripts) == 2

    harness_js = f"""
const jsonText = {json.dumps(m_json.group(1))};
function makeNode(tag, ns) {{
  const attrs = {{}};
  const listeners = {{}};
  let _text = '';
  const node = {{
    tagName: tag.toUpperCase(),
    namespaceURI: ns || null,
    className: '',
    get textContent() {{ return _text; }},
    set textContent(v) {{
      _text = String(v);
      if (_text === '') this.children = [];
    }},
    style: {{}},
    children: [],
    setAttribute(k, v) {{ attrs[k] = String(v); }},
    getAttribute(k) {{ return k in attrs ? attrs[k] : null; }},
    appendChild(c) {{ this.children.push(c); return c; }},
    addEventListener(ev, fn) {{ (listeners[ev] = listeners[ev] || []).push(fn); }},
    dispatchEvent(ev) {{
      (listeners[ev.type] || []).forEach((fn) => fn(ev));
    }},
  }};
  return node;
}}
const raiz = makeNode('div');
const elDados = {{ textContent: jsonText }};
global.document = {{
  getElementById(id) {{
    if (id === 'dados-painel') return elDados;
    if (id === 'painel-raiz') return raiz;
    return null;
  }},
  createElement(tag) {{ return makeNode(tag); }},
  createElementNS(ns, tag) {{ return makeNode(tag, ns); }},
  createTextNode(t) {{ return {{ nodeType: 3, textContent: String(t) }}; }},
}};
{scripts[0]}
const caixa = raiz.children[0];
const controles = caixa.children[0];
const grupoEixos = controles.children[1];
const btnX = grupoEixos.children[0];
const btnY = grupoEixos.children[1];

if (btnX.getAttribute('aria-pressed') !== 'true') throw new Error('btnX initial aria-pressed');
if (btnX.textContent !== 'Eixo X logarítmico') {{
  throw new Error('btnX initial text: ' + btnX.textContent);
}}
if (btnY.getAttribute('aria-pressed') !== 'true') throw new Error('btnY initial aria-pressed');
if (btnY.textContent !== 'Eixo Y: começar em zero') {{
  throw new Error('btnY initial text: ' + btnY.textContent);
}}

const areaSvg = caixa.children[2];
function yTicks() {{
  const svg = areaSvg.children[0];
  return svg.children
    .filter((c) => c.tagName === 'TEXT' && c.getAttribute('text-anchor') === 'end')
    .map((c) => c.textContent);
}}
function xTicks() {{
  const svg = areaSvg.children[0];
  return svg.children
    .filter((c) => c.tagName === 'TEXT' && c.getAttribute('text-anchor') === 'middle')
    .map((c) => Number(c.getAttribute('x')));
}}

const xsLog = xTicks();
const passoLog1 = xsLog[1] - xsLog[0];
const passoLog2 = xsLog[2] - xsLog[1];
if (Math.abs(passoLog1 - passoLog2) > 0.5) throw new Error('expected equal log spacing');

btnX.dispatchEvent({{ type: 'click' }});
if (btnX.getAttribute('aria-pressed') !== 'false') throw new Error('btnX toggled aria-pressed');
if (btnX.textContent !== 'Eixo X linear') throw new Error('btnX linear text');
const xsLin = xTicks();
if (!(xsLin[2] - xsLin[1] > (xsLin[1] - xsLin[0]) * 5)) throw new Error('expected linear spacing');

controles.children[0].children[3].dispatchEvent({{ type: 'click' }});
const ticksZero = yTicks();
if (ticksZero[0] !== '0,0') {{
  throw new Error('expected 0,0 first tick when yZero=true, got ' + ticksZero[0]);
}}
if (ticksZero[4] !== '100,0') {{
  throw new Error('expected 100,0 top tick when yZero=true, got ' + ticksZero[4]);
}}

btnY.dispatchEvent({{ type: 'click' }});
if (btnY.getAttribute('aria-pressed') !== 'false') throw new Error('btnY toggled aria-pressed');
if (btnY.textContent !== 'Eixo Y: escala própria') {{
  throw new Error('btnY propria text: ' + btnY.textContent);
}}
const ticksPropria = yTicks();
if (ticksPropria[0] === '0,0') throw new Error('expected non-zero first tick when yZero=false');
if (ticksPropria[4] === '100,0') {{
  throw new Error('expected zoomed top tick when yZero=false, got ' + ticksPropria[4]);
}}

btnY.dispatchEvent({{ type: 'click' }});
if (btnY.getAttribute('aria-pressed') !== 'true') throw new Error('btnY restored aria-pressed');
if (btnY.textContent !== 'Eixo Y: começar em zero') throw new Error('btnY restored text');
if (yTicks()[0] !== '0,0') throw new Error('expected 0,0 restored');
"""
    js_path = tmp_path / "test_eixos.js"
    js_path.write_text(harness_js, encoding="utf-8")
    proc = subprocess.run(["node", str(js_path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Falha no teste de controles X/Y JS: {proc.stderr}\n{proc.stdout}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node não disponível no PATH")
def test_painel_guia_vertical_balao_multivariante_e_setas_escalas(tmp_path: Path) -> None:
    dados = DadosEvolucao(
        versao=1,
        execucoes=(
            _exec(
                1,
                "repl",
                60,
                rodadas=10,
                rodadas_validas=9,
                aprovadas=8,
                reprovadas=1,
                erro_infra=1,
                tokens_entrada_por_rodada_executada=8000.0,
            ),
            _exec(
                2,
                "repl",
                600,
                rodadas=10,
                rodadas_validas=10,
                aprovadas=9,
                reprovadas=1,
                erro_infra=0,
                tokens_entrada_por_rodada_executada=12000.0,
            ),
            _exec(
                3,
                "repl",
                6000,
                rodadas=10,
                rodadas_validas=8,
                aprovadas=6,
                reprovadas=2,
                erro_infra=2,
                tokens_entrada_por_rodada_executada=18000.0,
            ),
            _exec(
                1,
                "json",
                60,
                rodadas=10,
                rodadas_validas=10,
                aprovadas=7,
                reprovadas=3,
                erro_infra=0,
                tokens_entrada_por_rodada_executada=10000.0,
            ),
            _exec(
                2,
                "json",
                600,
                rodadas=10,
                rodadas_validas=9,
                aprovadas=5,
                reprovadas=4,
                erro_infra=1,
                tokens_entrada_por_rodada_executada=15000.0,
            ),
            _exec(
                3,
                "json",
                6000,
                rodadas=10,
                rodadas_validas=7,
                aprovadas=4,
                reprovadas=3,
                erro_infra=3,
                tokens_entrada_por_rodada_executada=22000.0,
            ),
        ),
    )
    html = gerar_pagina_html(dados)
    m_json = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m_json is not None
    scripts = re.findall(
        r"<script(?![^>]*application/json)[^>]*>(.*?)</script>",
        html,
        flags=re.DOTALL,
    )
    assert len(scripts) == 2

    harness_js = f"""
const jsonText = {json.dumps(m_json.group(1))};
function makeNode(tag, ns) {{
  const attrs = {{}};
  const listeners = {{}};
  let _text = '';
  const node = {{
    tagName: tag.toUpperCase(),
    namespaceURI: ns || null,
    className: '',
    hidden: false,
    get textContent() {{ return _text; }},
    set textContent(v) {{
      _text = String(v);
      if (_text === '') this.children = [];
    }},
    style: {{}},
    children: [],
    setAttribute(k, v) {{ attrs[k] = String(v); }},
    getAttribute(k) {{ return k in attrs ? attrs[k] : null; }},
    appendChild(c) {{ this.children.push(c); return c; }},
    contains(alvo) {{
      if (alvo === this) return true;
      return this.children.some((c) => c.contains && c.contains(alvo));
    }},
    getBoundingClientRect() {{ return {{ left: 0, top: 0, width: 720, height: 300 }}; }},
    addEventListener(ev, fn) {{ (listeners[ev] = listeners[ev] || []).push(fn); }},
    dispatchEvent(ev) {{
      if (!ev.target) ev.target = this;
      (listeners[ev.type] || []).forEach((fn) => fn(ev));
    }},
  }};
  return node;
}}
function collectText(n) {{
  if (n.nodeType === 3) return n.textContent || '';
  if (n.children && n.children.length) return n.children.map(collectText).join(' ');
  return n.textContent || '';
}}
const raiz = makeNode('div');
const elDados = {{ textContent: jsonText }};
global.document = {{
  getElementById(id) {{
    if (id === 'dados-painel') return elDados;
    if (id === 'painel-raiz') return raiz;
    return null;
  }},
  createElement(tag) {{ return makeNode(tag); }},
  createElementNS(ns, tag) {{ return makeNode(tag, ns); }},
  createTextNode(t) {{ return {{ nodeType: 3, textContent: String(t) }}; }},
}};
{scripts[0]}
const caixa = raiz.children[0];
const controles = caixa.children[0];
const areaSvg = caixa.children[2];
const svg = areaSvg.children[0];
const balao = areaSvg.children[1];
const guia = svg.children.find((c) => c.getAttribute('data-guia') === '1');
if (!guia) throw new Error('missing vertical guideline');
if (guia.getAttribute('visibility') !== 'hidden') throw new Error('guide should start hidden');
if (!balao.hidden) throw new Error('balao should start hidden');

const ticksX = svg.children
  .filter((c) => c.tagName === 'TEXT' && c.getAttribute('text-anchor') === 'middle')
  .map((c) => Number(c.getAttribute('x')));
if (ticksX.length !== 3) throw new Error('expected 3 x ticks');

// Hover near second scale (600)
svg.dispatchEvent({{ type: 'pointermove', pointerType: 'mouse', clientX: ticksX[1] + 5 }});
if (guia.getAttribute('visibility') !== 'visible') throw new Error('guide not visible on hover');
if (Math.abs(Number(guia.getAttribute('x1')) - ticksX[1]) > 0.1) {{
  throw new Error('guide did not snap to scale 600');
}}
if (balao.hidden) throw new Error('balao not visible on hover');
const txt600 = collectText(balao);
if (!txt600.includes('600 linhas')) throw new Error('missing scale header: ' + txt600);
if (!txt600.includes('REPL') || !txt600.includes('JSON')) {{
  throw new Error('missing all visible variants: ' + txt600);
}}
if (!txt600.includes('12,0') || !txt600.includes('15,0')) {{
  throw new Error('missing metric values: ' + txt600);
}}
if (!txt600.includes('10/10') || !txt600.includes('9/10')) {{
  throw new Error('missing valid/total rounds: ' + txt600);
}}
if (
  !txt600.includes('Aprovadas: 9') ||
  !txt600.includes('Reprovadas: 4') ||
  !txt600.includes('Infra: 1')
) {{
  throw new Error('missing aprovadas/reprovadas/infra: ' + txt600);
}}

// Pointer leave hides guide and balao
svg.dispatchEvent({{ type: 'pointerleave' }});
if (guia.getAttribute('visibility') !== 'hidden' || !balao.hidden) {{
  throw new Error('expected hidden after pointerleave');
}}

// Touch near third scale (6000)
svg.dispatchEvent({{ type: 'pointerdown', pointerType: 'touch', clientX: ticksX[2] - 4 }});
if (guia.getAttribute('visibility') !== 'visible') throw new Error('guide not visible on touch');
if (Math.abs(Number(guia.getAttribute('x1')) - ticksX[2]) > 0.1) {{
  throw new Error('guide did not snap to scale 6000 on touch');
}}
if (!collectText(balao).includes('6.000 linhas')) {{
  throw new Error('expected 6.000 linhas on touch');
}}

// Focus + keyboard ArrowLeft / ArrowRight / Home / End / Escape
svg.dispatchEvent({{ type: 'focusin', target: svg }});
if (guia.getAttribute('visibility') !== 'visible') throw new Error('guide not visible on focusin');
function keySvg(k) {{
  let prevented = false;
  svg.dispatchEvent({{ type: 'keydown', key: k, preventDefault() {{ prevented = true; }} }});
  if (k !== 'Escape' && !prevented) throw new Error('expected preventDefault for ' + k);
}}
function assertGuiaX(expected, msg) {{
  if (Math.abs(Number(guia.getAttribute('x1')) - expected) > 0.1) throw new Error(msg);
}}
keySvg('Home');
assertGuiaX(ticksX[0], 'Home failed');
keySvg('ArrowRight');
assertGuiaX(ticksX[1], 'ArrowRight failed');
keySvg('ArrowRight');
assertGuiaX(ticksX[2], 'ArrowRight 2 failed');
keySvg('ArrowLeft');
assertGuiaX(ticksX[1], 'ArrowLeft failed');
keySvg('End');
assertGuiaX(ticksX[2], 'End failed');
keySvg('Escape');
if (guia.getAttribute('visibility') !== 'hidden' || !balao.hidden) {{
  throw new Error('Escape did not hide guide');
}}

// Uncheck first variant checkbox -> balao should only list remaining visible variant
const grupoVars = controles.children[2];
const cb0 = grupoVars.children[0].children[0];
cb0.checked = false;
cb0.dispatchEvent({{ type: 'change' }});
const svg2 = areaSvg.children[0];
const balao2 = areaSvg.children[1];
svg2.dispatchEvent({{ type: 'pointermove', pointerType: 'mouse', clientX: ticksX[0] }});
const txtFiltrado = collectText(balao2);
if (txtFiltrado.includes('REPL')) {{
  throw new Error('hidden variant should not appear: ' + txtFiltrado);
}}
if (!txtFiltrado.includes('JSON')) {{
  throw new Error('visible variant should appear: ' + txtFiltrado);
}}
"""
    js_path = tmp_path / "test_guia_balao.js"
    js_path.write_text(harness_js, encoding="utf-8")
    proc = subprocess.run(["node", str(js_path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"Falha no teste de guia/balão JS: {proc.stderr}\n{proc.stdout}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node não disponível no PATH")
def test_painel_cartoes_resumo_maior_escala_e_razao_custo(tmp_path: Path) -> None:
    dados = DadosEvolucao(
        versao=1,
        execucoes=(
            _exec(1, "repl", 60, custo_por_rodada_executada_usd=0.001, taxa_pct=90.0),
            _exec(2, "repl", 600, custo_por_rodada_executada_usd=0.001, taxa_pct=85.0),
            _exec(3, "repl", 6000, custo_por_rodada_executada_usd=0.001, taxa_pct=80.0),
            _exec(1, "json", 60, custo_por_rodada_executada_usd=0.002, taxa_pct=85.0),
            _exec(2, "json", 600, custo_por_rodada_executada_usd=0.010, taxa_pct=60.0),
            _exec(3, "json", 6000, custo_por_rodada_executada_usd=0.025, taxa_pct=30.0),
            _exec(1, "json-agregacao", 60, custo_por_rodada_executada_usd=0.0012, taxa_pct=88.0),
            _exec(2, "json-agregacao", 600, custo_por_rodada_executada_usd=0.0025, taxa_pct=82.0),
        ),
    )
    html = gerar_pagina_html(dados)
    m_json = re.search(
        r'<script type="application/json" id="dados-painel">(.*?)</script>',
        html,
        flags=re.DOTALL,
    )
    assert m_json is not None
    scripts = re.findall(
        r"<script(?![^>]*application/json)[^>]*>(.*?)</script>",
        html,
        flags=re.DOTALL,
    )
    assert len(scripts) == 2

    harness_js = f"""
const jsonText = {json.dumps(m_json.group(1))};
function makeNode(tag, ns) {{
  const attrs = {{}};
  const listeners = {{}};
  let _text = '';
  const node = {{
    tagName: tag.toUpperCase(),
    namespaceURI: ns || null,
    className: '',
    hidden: false,
    get textContent() {{ return _text; }},
    set textContent(v) {{
      _text = String(v);
      if (_text === '') this.children = [];
    }},
    style: {{}},
    children: [],
    setAttribute(k, v) {{ attrs[k] = String(v); }},
    getAttribute(k) {{ return k in attrs ? attrs[k] : null; }},
    appendChild(c) {{ this.children.push(c); return c; }},
    addEventListener(ev, fn) {{ (listeners[ev] = listeners[ev] || []).push(fn); }},
    dispatchEvent(ev) {{
      if (!ev.target) ev.target = this;
      (listeners[ev.type] || []).forEach((fn) => fn(ev));
    }},
  }};
  return node;
}}
function collectText(n) {{
  if (n.nodeType === 3) return n.textContent || '';
  if (n.children && n.children.length) return n.children.map(collectText).join(' ');
  return n.textContent || '';
}}
const raiz = makeNode('div');
const elDados = {{ textContent: jsonText }};
global.document = {{
  getElementById(id) {{
    if (id === 'dados-painel') return elDados;
    if (id === 'painel-raiz') return raiz;
    return null;
  }},
  createElement(tag) {{ return makeNode(tag); }},
  createElementNS(ns, tag) {{ return makeNode(tag, ns); }},
  createTextNode(t) {{ return {{ nodeType: 3, textContent: String(t) }}; }},
}};
{scripts[0]}
const caixa = raiz.children[0];
const controles = caixa.children[0];
const areaResumo = caixa.children[1];
const areaSvg = caixa.children[2];
if (areaResumo.className !== 'painel-resumo') {{
  throw new Error('expected painel-resumo above chart, got ' + areaResumo.className);
}}
if (areaSvg.className !== 'painel-grafico') {{
  throw new Error('expected painel-grafico after resumo, got ' + areaSvg.className);
}}
const cartoes = areaResumo.children;
// 3 variantes ativas + 1 cartao de razao de custo = 4 cartoes
if (cartoes.length !== 4) {{
  throw new Error('expected 4 cards, got ' + cartoes.length);
}}
const txtRepl = collectText(cartoes[0]);
if (
  !txtRepl.includes('REPL') ||
  !txtRepl.includes('80,0%') ||
  !txtRepl.includes('1,00 milésimos de US$') ||
  !txtRepl.includes('6.000 linhas')
) {{
  throw new Error('unexpected REPL card: ' + txtRepl);
}}
const txtJson = collectText(cartoes[1]);
if (
  !txtJson.includes('JSON') ||
  !txtJson.includes('30,0%') ||
  !txtJson.includes('25,00 milésimos de US$') ||
  !txtJson.includes('6.000 linhas')
) {{
  throw new Error('unexpected JSON card: ' + txtJson);
}}
const txtAgreg = collectText(cartoes[2]);
if (!txtAgreg.includes('sem dado')) {{
  throw new Error('expected sem dado for variant missing 6000 scale: ' + txtAgreg);
}}
const txtRazao = collectText(cartoes[3]);
if (!txtRazao.includes('25x') || !txtRazao.includes('JSON vs REPL')) {{
  throw new Error('unexpected ratio card: ' + txtRazao);
}}

// Desmarcar JSON -> maior escala continua 6000 (REPL), apenas 1 variante com custo em 6000
const grupoVars = controles.children[2];
const cbJson = grupoVars.children[1].children[0];
cbJson.checked = false;
cbJson.dispatchEvent({{ type: 'change' }});
if (areaResumo.children.length !== 2) {{
  throw new Error(
    'expected 2 cards without ratio when <2 variants have cost at max scale, got ' +
      areaResumo.children.length
  );
}}

// Desmarcar REPL tambem -> apenas json-agregacao ativo, maior escala passa a ser 600
const cbRepl = grupoVars.children[0].children[0];
cbRepl.checked = false;
cbRepl.dispatchEvent({{ type: 'change' }});
if (areaResumo.children.length !== 1) {{
  throw new Error('expected 1 card for json-agregacao alone, got ' + areaResumo.children.length);
}}
const txtSoAgreg = collectText(areaResumo.children[0]);
if (
  !txtSoAgreg.includes('600 linhas') ||
  !txtSoAgreg.includes('82,0%') ||
  !txtSoAgreg.includes('2,50 milésimos de US$')
) {{
  throw new Error('unexpected filtered single-variant card at 600: ' + txtSoAgreg);
}}
"""
    js_path = tmp_path / "test_resumo.js"
    js_path.write_text(harness_js, encoding="utf-8")
    proc = subprocess.run(["node", str(js_path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, (
        f"Falha no teste de cartões-resumo JS: {proc.stderr}\n{proc.stdout}"
    )
