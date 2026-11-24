"""Testes dos gráficos SVG e da página HTML (T17d).

Nada aqui fala com a rede nem com o modelo. Os dados são sintéticos, montados em cada teste, ou
o arquivo real de métricas, só para leitura. Toda escrita vai para `tmp_path`: nada vai para docs/.
"""

import io
import json
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, main
from harness.evals.graficos import (
    NOME_PAGINA,
    NOMES_ARQUIVOS,
    DadosEvolucao,
    ErroDeGraficos,
    carregar_dados,
    gerar_pagina_html,
    gerar_todos,
    grafico_curva_de_contexto,
    grafico_custo_por_rodada,
    grafico_dispersao_iteracoes_tokens,
    grafico_iteracoes,
    grafico_taxa_por_categoria,
    grafico_taxa_total,
)
from harness.evals.procedencia import RAIZ_DO_REPOSITORIO

NS: str = "{http://www.w3.org/2000/svg}"
XMLNS: str = "http://www.w3.org/2000/svg"
ARQUIVO_REAL: Path = (
    RAIZ_DO_REPOSITORIO / "docs" / "relatorios" / "dados" / "evolucao-execucoes.json"
)
CATEGORIAS: Mapping[str, float] = {
    "conformidade": 60.0,
    "consulta": 40.0,
    "cruzamento": 80.0,
    "permissao": 100.0,
}
FUNCOES: tuple[Callable[[DadosEvolucao], str], ...] = (
    grafico_taxa_total,
    grafico_taxa_por_categoria,
    grafico_iteracoes,
    grafico_dispersao_iteracoes_tokens,
    grafico_curva_de_contexto,
    grafico_custo_por_rodada,
)


def _ex(numero: int, **campos: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "numero": numero,
        "variante": "repl",
        "rodadas": 10,
        "rodadas_validas": 10,
        "aprovadas": 5,
        "taxa_pct": 50.0,
        "iteracoes_media": 5.0,
        "tokens_entrada_por_rodada": 10000,
        "custo_por_rodada_usd": 0.0005,
        "categorias_pct": dict(CATEGORIAS),
        "prompt": "antigo",
    }
    base.update(campos)
    return base


def _dados(
    execucoes: list[dict[str, Any]], curvas: list[dict[str, Any]] | None = None
) -> DadosEvolucao:
    return DadosEvolucao.model_validate(
        {"versao": 1, "execucoes": execucoes, "curvas_de_contexto": curvas or []}
    )


def _raiz(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _titulos(svg: str) -> list[str]:
    """Textos dos `<title>` de marcadores (o título do próprio SVG tem id e fica de fora)."""
    return [el.text or "" for el in _raiz(svg).iter(f"{NS}title") if el.get("id") is None]


def _textos(svg: str) -> list[str]:
    return [el.text or "" for el in _raiz(svg).iter(f"{NS}text")]


# --- SVG: estrutura, acessibilidade e determinismo ------------------------------------------


@pytest.mark.parametrize("funcao", FUNCOES)
def test_svg_bem_formado_com_titulo_desc_e_determinismo(
    funcao: Callable[[DadosEvolucao], str],
) -> None:
    dados = _dados([_ex(1), _ex(2, taxa_pct=90.0), _ex(3, variante="json")])
    svg = funcao(dados)
    raiz = _raiz(svg)
    assert raiz.tag == f"{NS}svg"
    assert raiz.get("viewBox") == "0 0 720 400"
    assert raiz.get("role") == "img"
    assert raiz.find(f"{NS}title") is not None
    assert raiz.find(f"{NS}desc") is not None
    assert "<script" not in svg
    assert "javascript:" not in svg
    assert "http://" not in svg.replace(XMLNS, "")
    assert "https://" not in svg
    assert svg == funcao(dados)


def test_coordenada_do_maior_valor_fica_acima() -> None:
    svg = grafico_taxa_total(_dados([_ex(1, taxa_pct=50.0), _ex(2, taxa_pct=90.0)]))
    # Círculos da legenda não têm <title>; só os marcadores com tooltip entram aqui.
    cy: dict[str, float] = {}
    for el in _raiz(svg).iter(f"{NS}circle"):
        titulo = el.find(f"{NS}title")
        if titulo is not None:
            cy[titulo.text or ""] = float(el.get("cy", "0"))
    assert (
        cy["Execução 2: 90,0% de acerto (5 de 10 rodadas)"]
        < cy["Execução 1: 50,0% de acerto (5 de 10 rodadas)"]
    )


def test_ponto_fraco_vira_circulo_vazado_com_nota_no_titulo() -> None:
    svg = grafico_taxa_total(_dados([_ex(1), _ex(2, ponto_fraco=True, nota="Provedor instável")]))
    circulos = [el for el in _raiz(svg).iter(f"{NS}circle") if el.find(f"{NS}title") is not None]
    vazado = [
        el
        for el in circulos
        if "Provedor instável" in (el.find(f"{NS}title").text or "")  # type: ignore[union-attr]
    ]
    assert len(vazado) == 1
    assert vazado[0].get("fill") == "#ffffff"
    assert vazado[0].get("stroke") == "#2a78d6"
    assert vazado[0].get("tabindex") == "0"


def test_none_interrompe_a_linha_em_dois_segmentos() -> None:
    execucoes = [_ex(n) for n in (1, 2, 4, 5)]
    execucoes.insert(2, _ex(3, taxa_pct=None))
    svg = grafico_taxa_total(_dados(execucoes))
    caminhos = list(_raiz(svg).iter(f"{NS}path"))
    assert len(caminhos) == 2


def test_duas_variantes_geram_duas_series_com_cores_e_legenda() -> None:
    svg = grafico_taxa_total(
        _dados([_ex(1), _ex(2), _ex(1, variante="json"), _ex(2, variante="json")])
    )
    assert len(list(_raiz(svg).iter(f"{NS}path"))) == 2
    cores = {el.get("stroke") for el in _raiz(svg).iter(f"{NS}path")}
    assert cores == {"#2a78d6", "#eb6834"}
    textos = _textos(svg)
    assert "REPL (Code Mode)" in textos
    assert "Tool calling em JSON" in textos


def test_taxa_por_categoria_so_desenha_a_variante_pedida() -> None:
    dados = _dados(
        [
            _ex(1),
            _ex(2),
            _ex(1, variante="json", categorias_pct={"conformidade": 1.0}),
        ]
    )
    svg = grafico_taxa_por_categoria(dados, "repl")
    assert len(list(_raiz(svg).iter(f"{NS}path"))) == 4
    assert "Tool calling em JSON" not in _textos(svg)
    legenda = _textos(svg)
    for nome in ("Conformidade", "Consulta", "Cruzamento", "Permissao"):
        assert nome in legenda


def test_marcador_com_valor_numerico_tem_titulo_em_portugues() -> None:
    svg = grafico_iteracoes(_dados([_ex(7, iteracoes_media=5.57)]))
    assert "Execução 7: 5,6 iterações por pedido" in _titulos(svg)


def test_dispersao_tem_titulo_e_rotulo_por_ponto() -> None:
    svg = grafico_dispersao_iteracoes_tokens(
        _dados([_ex(7, iteracoes_media=5.57, tokens_entrada_por_rodada=17800)])
    )
    assert "Execução 7: 5,6 iterações, 17,8 mil tokens de entrada" in _titulos(svg)
    assert "7" in _textos(svg)


def test_curva_tem_titulo_de_turno_com_ponto_no_milhar() -> None:
    curva = {"nome": "REPL (Code Mode), linha de base", "pontos": [[1, 1200], [2, 2836]]}
    svg = grafico_curva_de_contexto(_dados([], [curva]))
    assert "Turno 2: 2.836 tokens de entrada" in _titulos(svg)


def test_custo_tem_titulo_em_milesimos_por_mil_rodadas() -> None:
    svg = grafico_custo_por_rodada(_dados([_ex(10, custo_por_rodada_usd=0.000691)]))
    assert "Execução 10: US$ 0,69 por mil rodadas" in _titulos(svg)


def test_escape_de_nota_e_de_categoria_continua_xml_valido() -> None:
    dados = _dados(
        [
            _ex(
                1,
                ponto_fraco=True,
                nota='A & B <x> "q"',
                categorias_pct={"a&b": 50.0},
            )
        ]
    )
    svg = grafico_taxa_total(dados)
    assert any('A & B <x> "q"' in titulo for titulo in _titulos(svg))
    assert "A&b" in _textos(grafico_taxa_por_categoria(dados, "repl"))


# --- degenerados ----------------------------------------------------------------------------


@pytest.mark.parametrize("funcao", FUNCOES)
def test_sem_execucoes_mostra_sem_dados(funcao: Callable[[DadosEvolucao], str]) -> None:
    svg = funcao(_dados([]))
    assert "sem dados" in _textos(svg)


def test_uma_execucao_gera_ponto_sem_linha() -> None:
    svg = grafico_taxa_total(_dados([_ex(1)]))
    assert len(list(_raiz(svg).iter(f"{NS}path"))) == 0
    assert "Execução 1: 50,0% de acerto (5 de 10 rodadas)" in _titulos(svg)


@pytest.mark.parametrize("funcao", FUNCOES)
def test_valores_iguais_nao_levantam(funcao: Callable[[DadosEvolucao], str]) -> None:
    dados = _dados([_ex(1), _ex(2), _ex(3)])
    _raiz(funcao(dados))


# --- validação ------------------------------------------------------------------------------


def _escrever(caminho: Path, conteudo: str) -> Path:
    caminho.write_text(conteudo, encoding="utf-8")
    return caminho


def test_json_invalido_vira_erro_sem_despejar_conteudo(tmp_path: Path) -> None:
    caminho = _escrever(tmp_path / "x.json", '{"segredo_xyz": ')
    with pytest.raises(ErroDeGraficos) as info:
        carregar_dados(caminho)
    assert "não é JSON válido" in str(info.value)
    assert "segredo_xyz" not in str(info.value)


def test_esquema_invalido_vira_erro_sem_despejar_conteudo(tmp_path: Path) -> None:
    caminho = _escrever(tmp_path / "x.json", json.dumps({"versao": 2, "segredo_xyz": "abc"}))
    with pytest.raises(ErroDeGraficos) as info:
        carregar_dados(caminho)
    assert "esquema inválido" in str(info.value)
    assert "versao" in str(info.value)
    assert "segredo_xyz" not in str(info.value)


def test_numero_repetido_na_mesma_variante_vira_erro(tmp_path: Path) -> None:
    conteudo = {"versao": 1, "execucoes": [_ex(1), _ex(1)]}
    caminho = _escrever(tmp_path / "x.json", json.dumps(conteudo))
    with pytest.raises(ErroDeGraficos, match="repetido"):
        carregar_dados(caminho)


def test_numero_repetido_em_variantes_diferentes_e_aceito() -> None:
    dados = _dados([_ex(1), _ex(1, variante="json")])
    assert len(dados.execucoes) == 2


def test_arquivo_ausente_vira_erro(tmp_path: Path) -> None:
    with pytest.raises(ErroDeGraficos, match="não encontrado"):
        carregar_dados(tmp_path / "nao-existe.json")


# --- arquivo real (só leitura) e página HTML -----------------------------------------------


class _Coletor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.svgs = 0
        self.scripts = 0
        self.estilos = 0
        self.metas: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "svg":
            self.svgs += 1
        elif tag == "script":
            self.scripts += 1
        elif tag == "style":
            self.estilos += 1
        elif tag == "meta":
            self.metas.append(dict(attrs))


def test_pagina_html_autonoma_e_bem_formada_para_html_parser() -> None:
    dados = carregar_dados(ARQUIVO_REAL)
    html = gerar_pagina_html(dados)
    coletor = _Coletor()
    coletor.feed(html)
    coletor.close()
    assert coletor.svgs == 6
    assert coletor.scripts == 1
    assert coletor.estilos == 1
    csp = [m.get("content") for m in coletor.metas if m.get("http-equiv")]
    assert csp == ["default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"]
    assert any(m.get("charset") == "utf-8" for m in coletor.metas)
    assert any(m.get("name") == "viewport" for m in coletor.metas)
    semxmlns = html.replace(XMLNS, "")
    for proibido in ("http://", "https://", "src=", "href="):
        assert proibido not in semxmlns
    assert "/Users/" not in html


def test_pagina_html_e_determinista_e_ids_sao_unicos() -> None:
    dados = carregar_dados(ARQUIVO_REAL)
    html = gerar_pagina_html(dados)
    assert html == gerar_pagina_html(dados)
    ids = [parte.split('"')[1] for parte in html.split(' id="')[1:]]
    assert len(ids) == len(set(ids))


def test_pagina_html_escapa_nota_com_ampersand() -> None:
    dados = _dados([_ex(1, ponto_fraco=True, nota="A & B <x>")])
    html = gerar_pagina_html(dados)
    assert "A &amp; B &lt;x&gt;" in html
    assert "A & B <x>" not in html


def test_arquivo_real_gera_os_sete_arquivos(tmp_path: Path) -> None:
    dados = carregar_dados(ARQUIVO_REAL)
    caminhos = gerar_todos(dados, tmp_path / "saida")
    assert [c.name for c in caminhos] == [*NOMES_ARQUIVOS, NOME_PAGINA]
    assert all(c.exists() for c in caminhos)
    for caminho in caminhos:
        texto = caminho.read_text(encoding="utf-8")
        assert "/Users/" not in texto
        if caminho.suffix == ".svg":
            assert "{" not in texto
            assert "}" not in texto


def test_tooltips_do_arquivo_real() -> None:
    dados = carregar_dados(ARQUIVO_REAL)
    taxa = grafico_taxa_total(dados)
    assert "Execução 7: 98,0% de acerto (98 de 100 rodadas)" in _titulos(taxa)
    dica_fraca = [t for t in _titulos(taxa) if t.startswith("Execução 9:")]
    assert dica_fraca and "Marcador vazado:" in dica_fraca[0]
    dispersao = grafico_dispersao_iteracoes_tokens(dados)
    assert any(t.startswith("Execução 7:") and "mil tokens" in t for t in _titulos(dispersao))


# --- CLI ------------------------------------------------------------------------------------


def test_cli_graficos_caminho_feliz_imprime_sete(tmp_path: Path) -> None:
    buf = io.StringIO()
    codigo = main(
        ["graficos", str(ARQUIVO_REAL), "--saida", str(tmp_path / "saida")],
        saida=buf,
        raiz=tmp_path,
    )
    assert codigo == CODIGO_OK
    linhas = [linha for linha in buf.getvalue().splitlines() if linha.startswith("Gráfico gerado:")]
    assert len(linhas) == 7
    assert all((tmp_path / "saida" / nome).exists() for nome in NOMES_ARQUIVOS)
    assert (tmp_path / "saida" / NOME_PAGINA).exists()


def test_cli_graficos_arquivo_ausente_sai_com_2(tmp_path: Path) -> None:
    buf = io.StringIO()
    codigo = main(
        ["graficos", str(tmp_path / "nao-existe.json"), "--saida", str(tmp_path / "s")],
        saida=buf,
        raiz=tmp_path,
    )
    assert codigo == CODIGO_USO
    assert "não encontrado" in buf.getvalue()


# --- propriedades ---------------------------------------------------------------------------

_medida = st.one_of(
    st.none(), st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False)
)
_linha = st.tuples(
    _medida,
    _medida,
    _medida,
    st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
    st.booleans(),
)


@settings(max_examples=40, deadline=None)
@given(linhas=st.lists(_linha, max_size=6))
def test_numeros_aleatorios_nunca_quebram_e_sao_xml_valido(
    linhas: list[tuple[float | None, float | None, float | None, int | None, bool]],
) -> None:
    execucoes = [
        _ex(
            indice + 1,
            variante="repl" if indice % 2 == 0 else "json",
            taxa_pct=taxa,
            iteracoes_media=iteracoes,
            custo_por_rodada_usd=custo,
            tokens_entrada_por_rodada=tokens,
            ponto_fraco=fraco,
        )
        for indice, (taxa, iteracoes, custo, tokens, fraco) in enumerate(linhas)
    ]
    dados = _dados(execucoes)
    for funcao in FUNCOES:
        _raiz(funcao(dados))


@pytest.mark.parametrize(
    "funcao",
    [
        grafico_taxa_total,
        grafico_taxa_por_categoria,
        grafico_iteracoes,
        grafico_dispersao_iteracoes_tokens,
        grafico_custo_por_rodada,
    ],
)
def test_execucao_incompleta_gera_marcador_vazado_tooltip_e_legenda(
    funcao: Callable[[DadosEvolucao], str],
) -> None:
    motivo = "execução incompleta: teto de gasto estourado; 2 rodadas não executadas"
    dados = _dados([_ex(1), _ex(2, incompleta=True, motivo_incompleta=motivo)])
    svg = funcao(dados)
    raiz = _raiz(svg)
    vazados = [
        el
        for el in raiz.iter(f"{NS}circle")
        if el.find(f"{NS}title") is not None and motivo in (el.find(f"{NS}title").text or "")  # type: ignore[union-attr]
    ]
    assert vazados
    assert all(el.get("fill") == "#ffffff" for el in vazados)
    assert "marcador vazado: execução 2 incompleta" in _textos(svg)


def test_execucao_incompleta_e_ponto_fraco_juntos_combina_tooltip_e_legenda() -> None:
    motivo = "execução incompleta: 3 erros de infraestrutura"
    dados = _dados(
        [
            _ex(1),
            _ex(
                2,
                ponto_fraco=True,
                nota="1 de 4 rodadas válidas",
                incompleta=True,
                motivo_incompleta=motivo,
            ),
        ]
    )
    svg = grafico_taxa_total(dados)
    assert any("1 de 4 rodadas válidas" in t and motivo in t for t in _titulos(svg))
    assert "marcador vazado: execução incompleta ou com poucas rodadas válidas" in _textos(svg)
