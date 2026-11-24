"""Testes da comparação de variantes (Ajuste 2): dados montados de relatórios, o gráfico de seis
painéis, `gerar_todos` com uma e com três variantes, e a CLI `graficos --de-relatorios`.

Nada aqui fala com a rede nem com o modelo. Os relatórios são montados com `agregar` e gravados
com `relatorio_para_json` em `tmp_path`. Nada é escrito em docs/.
"""

import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, main
from harness.evals.casos import Caso, Categoria, CitaFontes, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.graficos import (
    NOME_COMPARACAO,
    NOME_PAGINA,
    NOMES_ARQUIVOS,
    PASTA_COMPARACAO,
    SECAO_COMPARACAO,
    DadosEvolucao,
    ErroDeGraficos,
    Execucao,
    dados_de_relatorios,
    gerar_todos,
    grafico_comparacao_de_variantes,
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
HASH_PROMPT = "a1b2c3d4e5f6"


# --- montagem de relatórios e de execuções --------------------------------------------------


def _caso(identificador: str, categoria: Categoria) -> Caso:
    return Caso(
        id=identificador,
        categoria=categoria,
        perfil="Vendas",
        pedido="Qual é o total? Cite a fonte.",
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
    iteracoes: int = 2,
    entrada: int = 1000,
    saida: int = 100,
    custo: float | None = 0.001,
) -> RodadaCaso:
    return RodadaCaso(
        numero=numero,
        classificacao=Classificacao.APROVADA if aprovada else Classificacao.REPROVADA,
        veredito=_veredito("caso", aprovada),
        iteracoes=iteracoes,
        tokens_entrada=entrada,
        tokens_saida=saida,
        custo=custo,
        tokens_entrada_por_turno=(entrada,),
        pico_contexto=entrada,
        caracteres_observacao=50,
        hash_prompt=HASH_PROMPT,
    )


def _erro(numero: int) -> RodadaCaso:
    return RodadaCaso(numero=numero, classificacao=Classificacao.ERRO_INFRA, detalhe="RuntimeError")


def _relatorio(
    casos: dict[str, tuple[Categoria, list[RodadaCaso]]],
    *,
    variante: str = "repl",
) -> Relatorio:
    rodadas = {identificador: rs for identificador, (_, rs) in casos.items()}
    total = max(len(rs) for _, rs in casos.values())
    lista = [_caso(identificador, categoria) for identificador, (categoria, _) in casos.items()]
    return agregar(
        lista,
        rodadas,
        semente=42,
        rodadas_por_caso=total,
        variante=variante,  # type: ignore[arg-type]
    )


def _relatorio_padrao(variante: str = "repl") -> Relatorio:
    """Um caso, quatro rodadas: duas aprovadas, uma reprovada e um erro de infraestrutura."""
    return _relatorio(
        {
            "consulta-01": (
                Categoria.CONSULTA,
                [_valida(1), _valida(2), _valida(3, aprovada=False), _erro(4)],
            )
        },
        variante=variante,
    )


def _gravar(relatorio: Relatorio, caminho: Path) -> Path:
    caminho.write_text(relatorio_para_json(relatorio), encoding="utf-8")
    return caminho


def _exec(numero: int = 1, variante: str = "repl", **campos: Any) -> Execucao:
    base: dict[str, Any] = {
        "numero": numero,
        "variante": variante,
        "relatorio": f"{variante}-{numero}.json",
        "casos": 2,
        "rodadas": 10,
        "rodadas_validas": 10,
        "aprovadas": 5,
        "taxa_pct": 50.0,
        "erro_infra_pct": 0.0,
        "iteracoes_media": 4.0,
        "tokens_entrada_por_rodada": 10000,
        "tokens_saida_por_rodada": 500,
        "custo_por_rodada_usd": 0.001,
        "pico_contexto_medio": 12000.0,
        "caracteres_observacao_medio": 3000.0,
        "custo_por_acerto_usd": 0.002,
        "prompt": "abc",
    }
    base.update(campos)
    return Execucao(**base)


def _dados(*execucoes: Execucao) -> DadosEvolucao:
    return DadosEvolucao(versao=1, execucoes=tuple(execucoes))


def _raiz(svg: str) -> ET.Element:
    return ET.fromstring(svg)


def _titulos(svg: str) -> list[str]:
    """Textos dos `<title>` que não são o título do próprio SVG (esse tem id)."""
    return [el.text or "" for el in _raiz(svg).iter(f"{NS}title") if el.get("id") is None]


def _textos(svg: str) -> list[str]:
    """Texto de cada `<text>`, incluindo o que vem dentro de um `<title>` filho."""
    return ["".join(el.itertext()) for el in _raiz(svg).iter(f"{NS}text")]


# --- dados montados de relatórios -----------------------------------------------------------


def test_numeracao_por_variante_segue_a_ordem_dos_arquivos(tmp_path: Path) -> None:
    arquivos = [
        _gravar(_relatorio_padrao("repl"), tmp_path / "a.json"),
        _gravar(_relatorio_padrao("json"), tmp_path / "b.json"),
        _gravar(_relatorio_padrao("repl"), tmp_path / "c.json"),
    ]
    dados = dados_de_relatorios(arquivos)
    assert [(e.variante, e.numero, e.relatorio) for e in dados.execucoes] == [
        ("repl", 1, "a.json"),
        ("json", 1, "b.json"),
        ("repl", 2, "c.json"),
    ]


def test_taxa_e_erro_de_infra_saem_das_rodadas_certas(tmp_path: Path) -> None:
    caminho = _gravar(_relatorio_padrao(), tmp_path / "a.json")
    (execucao,) = dados_de_relatorios([caminho]).execucoes
    # Duas aprovadas em três válidas. O erro de infraestrutura fica fora da taxa.
    assert execucao.rodadas == 4
    assert execucao.rodadas_validas == 3
    assert execucao.aprovadas == 2
    assert execucao.taxa_pct == pytest.approx(200 / 3)
    assert execucao.erro_infra_pct == 25.0
    assert execucao.casos == 1
    assert execucao.prompt == f"prompt {HASH_PROMPT[:8]}"
    assert execucao.ponto_fraco is False


def test_medias_sao_sobre_as_validas_e_categorias_saem_por_caso(tmp_path: Path) -> None:
    relatorio = _relatorio(
        {
            "consulta-01": (
                Categoria.CONSULTA,
                [
                    _valida(1, iteracoes=2, entrada=1000, custo=0.001),
                    _valida(2, aprovada=False, iteracoes=4, entrada=2000, custo=0.003),
                ],
            ),
            "permissao-01": (Categoria.PERMISSAO, [_erro(1)]),
        }
    )
    (execucao,) = dados_de_relatorios([_gravar(relatorio, tmp_path / "a.json")]).execucoes
    assert execucao.rodadas_validas == 2
    assert execucao.iteracoes_media == 3.0
    assert execucao.tokens_entrada_por_rodada == 1500
    assert execucao.tokens_saida_por_rodada == 100
    assert execucao.custo_por_rodada_usd == pytest.approx(0.002)
    # Consulta: 1 de 2. Permissão só tem erro de infraestrutura: sem rodada válida, None.
    assert execucao.categorias_pct == {"consulta": 50.0, "permissao": None}


def test_ponto_fraco_quando_menos_da_metade_e_valida_com_nota_explicando(tmp_path: Path) -> None:
    relatorio = _relatorio(
        {"consulta-01": (Categoria.CONSULTA, [_valida(1), _erro(2), _erro(3), _erro(4)])}
    )
    (execucao,) = dados_de_relatorios([_gravar(relatorio, tmp_path / "fraca.json")]).execucoes
    assert execucao.ponto_fraco is True
    assert execucao.nota == "fraca.json: 1 de 4 rodadas válidas"


def test_nota_usa_o_nome_da_linha_de_base_quando_ha_uma(tmp_path: Path) -> None:
    relatorio = _relatorio_padrao().model_copy(
        update={"linha_de_base": "REPL (Code Mode), linha de base"}
    )
    (execucao,) = dados_de_relatorios([_gravar(relatorio, tmp_path / "a.json")]).execucoes
    assert execucao.nota == "REPL (Code Mode), linha de base"
    assert execucao.ponto_fraco is False


def test_relatorio_sem_prompt_registrado_recebe_rotulo_proprio(tmp_path: Path) -> None:
    relatorio = _relatorio(
        {"consulta-01": (Categoria.CONSULTA, [_valida(1, custo=None)])}
    ).model_copy(update={"hash_prompt": None})
    (execucao,) = dados_de_relatorios([_gravar(relatorio, tmp_path / "a.json")]).execucoes
    assert execucao.prompt == "sem prompt registrado"
    assert execucao.custo_por_rodada_usd is None


def test_relatorio_antigo_sem_campos_novos_deixa_none(tmp_path: Path) -> None:
    texto = json.loads(relatorio_para_json(_relatorio_padrao()))
    for campo in ("pico_contexto_medio", "caracteres_observacao_medio", "custo_por_acerto"):
        del texto[campo]
    caminho = tmp_path / "antigo.json"
    caminho.write_text(json.dumps(texto), encoding="utf-8")
    (execucao,) = dados_de_relatorios([caminho]).execucoes
    assert execucao.pico_contexto_medio is None
    assert execucao.caracteres_observacao_medio is None
    assert execucao.custo_por_acerto_usd is None


def test_curva_de_contexto_nomeada_por_variante_e_execucao(tmp_path: Path) -> None:
    curva = ((1, 1400), (2, 2800))
    a = _relatorio_padrao("repl").model_copy(update={"curva_de_contexto": curva})
    b = _relatorio_padrao("json").model_copy(update={"curva_de_contexto": curva})
    c = _relatorio_padrao("repl").model_copy(update={"curva_de_contexto": curva})
    dados = dados_de_relatorios(
        [
            _gravar(a, tmp_path / "a.json"),
            _gravar(b, tmp_path / "b.json"),
            _gravar(c, tmp_path / "c.json"),
        ]
    )
    assert [c_.nome for c_ in dados.curvas_de_contexto] == [
        "REPL (Code Mode) execução 1",
        "Tool calling em JSON",
        "REPL (Code Mode) execução 2",
    ]
    assert dados.curvas_de_contexto[0].pontos == curva
    assert dados.curvas_de_contexto[0].rodadas_por_ponto_minimo == 10


def test_relatorio_invalido_cita_so_o_nome_do_arquivo(tmp_path: Path) -> None:
    ruim = tmp_path / "ruim.json"
    ruim.write_text('{"segredo_xyz": ', encoding="utf-8")
    with pytest.raises(ErroDeGraficos) as info:
        dados_de_relatorios([ruim])
    mensagem = str(info.value)
    assert "ruim.json" in mensagem
    assert "segredo_xyz" not in mensagem
    assert str(tmp_path) not in mensagem


def test_relatorio_ausente_e_lista_vazia_viram_erro_em_portugues(tmp_path: Path) -> None:
    with pytest.raises(ErroDeGraficos, match="não encontrado: sumiu.json"):
        dados_de_relatorios([tmp_path / "sumiu.json"])
    with pytest.raises(ErroDeGraficos, match="nenhum relatório"):
        dados_de_relatorios([])


# --- gráfico de comparação ------------------------------------------------------------------


def _tres_variantes() -> DadosEvolucao:
    return _dados(
        _exec(1, "repl", taxa_pct=80.0),
        _exec(1, "json", taxa_pct=60.0, tokens_entrada_por_rodada=22672),
        _exec(1, "json-agregacao", taxa_pct=70.0),
    )


def test_comparacao_tem_seis_paineis_com_titulo_e_eixo_proprios() -> None:
    svg = grafico_comparacao_de_variantes(_tres_variantes())
    raiz = _raiz(svg)
    assert raiz.get("role") == "img"
    assert raiz.get("viewBox") == "0 0 720 600"
    assert raiz.find(f"{NS}title") is not None
    assert raiz.find(f"{NS}desc") is not None
    titulos_dos_paineis = [
        el.text
        for el in raiz.iter(f"{NS}text")
        if el.get("font-size") == "13" and el.get("font-weight") == "500"
    ]
    assert titulos_dos_paineis == [
        "Taxa de acerto (%)",
        "Iterações por pedido",
        "Tokens de entrada por rodada (mil)",
        "Pico de contexto médio (tokens)",
        "Caracteres de observação por rodada (mil)",
        "Custo por acerto (milésimos de US$)",
    ]


def test_uma_barra_por_variante_por_painel_com_largura_de_no_maximo_48() -> None:
    svg = grafico_comparacao_de_variantes(_tres_variantes())
    barras = [el for el in _raiz(svg).iter(f"{NS}path") if el.get("tabindex") == "0"]
    assert len(barras) == 3 * 6
    for barra in barras:
        numeros = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", barra.get("d", ""))]
        largura = numeros[8] - numeros[0]
        assert 0 < largura <= 48 + 1e-6
        assert "Q" in barra.get("d", "")


def test_tooltips_em_portugues_no_formato_da_comparacao() -> None:
    titulos = _titulos(grafico_comparacao_de_variantes(_tres_variantes()))
    assert "Tool calling em JSON: 22.672 tokens de entrada por rodada" in titulos
    assert "REPL (Code Mode): 80,0% de acerto" in titulos
    assert "REPL (Code Mode): 2,000 milésimos de US$ por acerto" in titulos


def test_sem_dado_vira_marca_no_painel() -> None:
    dados = _dados(_exec(1, "repl"), _exec(1, "json", pico_contexto_medio=None))
    svg = grafico_comparacao_de_variantes(dados)
    assert any(texto.endswith("sem dado") for texto in _textos(svg))
    assert "Tool calling em JSON: sem dado" in _titulos(svg)


def test_usa_a_execucao_mais_recente_de_cada_variante() -> None:
    dados = _dados(
        _exec(1, "repl", taxa_pct=10.0),
        _exec(2, "repl", taxa_pct=80.0),
        _exec(1, "json", taxa_pct=50.0),
    )
    titulos = _titulos(grafico_comparacao_de_variantes(dados))
    assert "REPL (Code Mode): 80,0% de acerto" in titulos
    assert not any("10,0%" in titulo for titulo in titulos)


def test_comparacao_sem_scripts_nem_url_externa_e_com_xml_valido() -> None:
    svg = grafico_comparacao_de_variantes(_tres_variantes())
    _raiz(svg)
    assert "<script" not in svg
    assert "javascript:" not in svg
    assert "http://" not in svg.replace(XMLNS, "")
    assert "https://" not in svg


def test_comparacao_e_deterministica() -> None:
    dados = _tres_variantes()
    assert grafico_comparacao_de_variantes(dados) == grafico_comparacao_de_variantes(dados)


def test_com_uma_variante_a_comparacao_so_diz_que_exige_duas() -> None:
    svg = grafico_comparacao_de_variantes(_dados(_exec(1), _exec(2)))
    _raiz(svg)
    assert "comparação exige duas ou mais variantes" in _textos(svg)


@settings(max_examples=30, deadline=None)
@given(
    taxas=st.lists(
        st.one_of(st.none(), st.floats(min_value=0, max_value=100, allow_nan=False)),
        min_size=2,
        max_size=2,
    ),
    tokens=st.lists(
        st.one_of(st.none(), st.integers(min_value=0, max_value=10**6)),
        min_size=2,
        max_size=2,
    ),
)
def test_valores_aleatorios_nunca_quebram_a_comparacao(
    taxas: list[float | None], tokens: list[int | None]
) -> None:
    dados = _dados(
        _exec(1, "repl", taxa_pct=taxas[0], tokens_entrada_por_rodada=tokens[0]),
        _exec(1, "json", taxa_pct=taxas[1], tokens_entrada_por_rodada=tokens[1]),
    )
    _raiz(grafico_comparacao_de_variantes(dados))


# --- gerar_todos ----------------------------------------------------------------------------


def test_uma_variante_nao_cria_comparacao_e_a_pagina_fica_sem_secao(tmp_path: Path) -> None:
    caminhos = gerar_todos(_dados(_exec(1), _exec(2)), tmp_path)
    assert [c.name for c in caminhos] == [*NOMES_ARQUIVOS, NOME_PAGINA]
    assert not (tmp_path / NOME_COMPARACAO).exists()
    pagina = (tmp_path / NOME_PAGINA).read_text(encoding="utf-8")
    assert SECAO_COMPARACAO[0] not in pagina


def test_tres_variantes_criam_comparacao_e_secao_antes_das_demais(tmp_path: Path) -> None:
    caminhos = gerar_todos(_tres_variantes(), tmp_path)
    assert [c.name for c in caminhos] == [*NOMES_ARQUIVOS, NOME_COMPARACAO, NOME_PAGINA]
    assert (tmp_path / NOME_COMPARACAO).read_text(encoding="utf-8").startswith("<svg")
    pagina = (tmp_path / NOME_PAGINA).read_text(encoding="utf-8")
    assert pagina.index(SECAO_COMPARACAO[0]) < pagina.index("Taxa de acerto por execução")
    assert SECAO_COMPARACAO[1] in pagina


def test_pagina_com_comparacao_mantem_ids_unicos_e_sem_recurso_externo(tmp_path: Path) -> None:
    gerar_todos(_tres_variantes(), tmp_path)
    pagina = (tmp_path / NOME_PAGINA).read_text(encoding="utf-8")
    ids = re.findall(r'\sid="([^"]+)"', pagina)
    assert len(ids) == len(set(ids))
    assert "/Users/" not in pagina
    assert "tc" in ids and "dc" in ids


# --- CLI ------------------------------------------------------------------------------------


def test_cli_de_relatorios_com_saida_explicita(tmp_path: Path) -> None:
    a = _gravar(_relatorio_padrao("repl"), tmp_path / "a.json")
    b = _gravar(_relatorio_padrao("json"), tmp_path / "b.json")
    buf = io.StringIO()
    codigo = main(
        ["graficos", "--de-relatorios", str(a), str(b), "--saida", str(tmp_path / "saida")],
        saida=buf,
        raiz=tmp_path,
    )
    assert codigo == CODIGO_OK
    assert (tmp_path / "saida" / NOME_COMPARACAO).exists()
    assert (tmp_path / "saida" / NOME_PAGINA).exists()
    linhas = [x for x in buf.getvalue().splitlines() if x.startswith("Gráfico gerado:")]
    assert len(linhas) == 8


def test_cli_de_relatorios_padrao_grava_na_pasta_de_comparacao(tmp_path: Path) -> None:
    a = _gravar(_relatorio_padrao("repl"), tmp_path / "a.json")
    buf = io.StringIO()
    codigo = main(["graficos", "--de-relatorios", str(a)], saida=buf, raiz=tmp_path)
    assert codigo == CODIGO_OK
    assert (tmp_path / PASTA_COMPARACAO / "taxa-total.svg").exists()
    assert not (tmp_path / "docs" / "relatorios" / "graficos").exists()


def test_cli_os_dois_modos_juntos_sai_com_dois(tmp_path: Path) -> None:
    a = _gravar(_relatorio_padrao("repl"), tmp_path / "a.json")
    buf = io.StringIO()
    codigo = main(
        ["graficos", str(tmp_path / "dados.json"), "--de-relatorios", str(a)],
        saida=buf,
        raiz=tmp_path,
    )
    assert codigo == CODIGO_USO
    assert "não os dois" in buf.getvalue()
    assert not (tmp_path / PASTA_COMPARACAO).exists()


def test_cli_sem_nenhum_modo_sai_com_dois(tmp_path: Path) -> None:
    buf = io.StringIO()
    codigo = main(["graficos"], saida=buf, raiz=tmp_path)
    assert codigo == CODIGO_USO
    assert "informe DADOS.json ou --de-relatorios" in buf.getvalue()


def test_cli_relatorio_invalido_sai_com_dois_e_cita_so_o_nome(tmp_path: Path) -> None:
    ruim = tmp_path / "ruim.json"
    ruim.write_text("segredo_xyz", encoding="utf-8")
    buf = io.StringIO()
    codigo = main(
        ["graficos", "--de-relatorios", str(ruim), "--saida", str(tmp_path / "s")],
        saida=buf,
        raiz=tmp_path,
    )
    assert codigo == CODIGO_USO
    texto = buf.getvalue()
    assert "ruim.json" in texto
    assert "segredo_xyz" not in texto


def test_execucao_incompleta_sinaliza_teto_nao_executadas_e_erros_infra(tmp_path: Path) -> None:
    completa = _relatorio({"consulta-01": (Categoria.CONSULTA, [_valida(1), _valida(2)])})
    parcial = _relatorio(
        {"consulta-01": (Categoria.CONSULTA, [_valida(1), _erro(2)])}
    ).model_copy(
        update={
            "teto_estourado": True,
            "teto_motivo": "custo",
            "nao_executadas": 9,
            "erros_infra": 5,
        }
    )
    singular = _relatorio(
        {"consulta-01": (Categoria.CONSULTA, [_valida(1), _erro(2)])}
    ).model_copy(update={"nao_executadas": 1, "erros_infra": 1})

    dados = dados_de_relatorios(
        [
            _gravar(completa, tmp_path / "c.json"),
            _gravar(parcial, tmp_path / "p.json"),
            _gravar(singular, tmp_path / "s.json"),
        ]
    )
    exec_c, exec_p, exec_s = dados.execucoes
    assert exec_c.incompleta is False
    assert exec_c.motivo_incompleta == ""

    assert exec_p.incompleta is True
    assert exec_p.motivo_incompleta == (
        "execução incompleta: teto de gasto estourado; 9 rodadas não executadas; "
        "5 erros de infraestrutura"
    )

    assert exec_s.incompleta is True
    assert exec_s.motivo_incompleta == (
        "execução incompleta: 1 rodada não executada; 1 erro de infraestrutura"
    )
