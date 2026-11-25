"""Gráficos SVG das execuções reais da avaliação, a partir de `evolucao-execucoes.json` (T17d).

`carregar_dados` lê e valida o arquivo de métricas por execução. Cada `grafico_*` devolve o SVG
como texto, `gerar_pagina_html` monta uma página HTML autônoma com os seis gráficos, e
`gerar_todos` grava os seis SVG e a página numa pasta. O SVG traz fundo próprio (branco, com
borda), porque o GitHub o mostra em tema claro e escuro.

Só biblioteca padrão e pydantic. Não há rede, data, aleatoriedade nem chamada ao modelo: a mesma
entrada dá exatamente o mesmo texto.

Decisões deste módulo (não estão na spec):
- Eixos de medida começam em zero. O eixo de execução vai de meia unidade antes da primeira até
  meia depois da última, com um tick por execução (de 20 em 20 quando há mais de 20).
- Ticks em passos 1, 2, 5, 10, 20, 50... escolhidos para dar cerca de seis divisões. O teto do
  eixo Y é o máximo com 10% de folga, arredondado para cima no passo. Eixo de tokens e de turnos
  usa ticks inteiros.
- Rótulos de valor sobre os pontos só quando há até duas séries. Com mais séries, a legenda basta.
- Cada marcador tem um `<title>` filho (tooltip). Em gráfico com mais de uma variante, o título
  cita a variante. O marcador vazado (ponto fraco) acrescenta a nota da execução, ou a frase
  padrão quando não há nota.
- Marcador vazado na legenda: usa a `nota` quando ela tem até 60 caracteres; senão, o texto padrão.
- Os textos dos números saem em português (vírgula decimal, ponto como separador de milhar). As
  coordenadas do SVG usam ponto e no máximo duas casas.
- Um gráfico sem nenhum valor plotável sai com a mensagem "sem dados" no lugar do desenho.
- A página HTML não tem recurso externo, data nem aleatoriedade. Ela troca o `id` de cada SVG por
  um sufixo (t1, d1...) para que os ids não se repitam na mesma página.
- Na página, o script mostra o `<title>` do marcador num balão perto do cursor, toque ou foco de
  teclado, e zera o texto do `<title>` enquanto o balão está visível (sem tooltip nativo duplicado).

Comparação de variantes (Ajuste 2): `dados_de_relatorios` monta o `DadosEvolucao` a partir de
relatórios de execução (um por arquivo, na ordem dos arquivos). `grafico_comparacao_de_variantes`
usa só a execução mais recente de cada variante (maior `numero`), em seis painéis com escala
própria. Ele só entra em `gerar_todos` e na página quando há duas ou mais variantes, então o
arquivo de dados de uma variante só gera exatamente a mesma saída de antes.
- `dados_de_relatorios`: `taxa_pct` é sobre as rodadas válidas (aprovadas / válidas), como no
  arquivo de dados. `erro_infra_pct` é sobre todas as rodadas. Médias de iterações, tokens e
  custo são sobre as rodadas válidas (custo: só as que têm custo). `ponto_fraco` quando menos de
  metade das rodadas é válida; nesse caso a `nota` diz o nome e a contagem (ex.: "x.json: 3 de 10
  rodadas válidas"). Sem ponto fraco, a `nota` é o nome da linha de base ou do arquivo.
- Curva de contexto: uma por relatório que a tem. O nome leva "execução N" só quando a variante
  aparece mais de uma vez.
- `--saida` padrão do modo de relatórios: `docs/relatorios/comparacao-variantes`, para não
  sobrescrever os gráficos da evolução em `docs/relatorios/graficos`.
"""

import json
import math
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Self
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from harness.evals.relatorio import (
    MINIMO_RODADAS_PARA_CURVA,
    Classificacao,
    Relatorio,
    relatorio_de_json,
)

LARGURA: Final = 720
ALTURA: Final = 400
ESQUERDA: Final = 72
DIREITA: Final = 696
BASE_Y: Final = 350
TOPO_MINIMO: Final = 96
FONTE: Final = "Helvetica Neue, Helvetica, Arial, sans-serif"
COR_FUNDO: Final = "#ffffff"
COR_BORDA: Final = "#e1e0d9"
COR_TEXTO: Final = "#0b0b0b"
COR_EIXO_TEXTO: Final = "#52514e"
COR_TICK: Final = "#898781"
COR_GRADE: Final = "#e1e0d9"
COR_BASE: Final = "#c3c2b7"
PALETA: Final = ("#2a78d6", "#eb6834", "#6250d6", "#1baf7a", "#e87ba4")
FORMAS: Final = ("circulo", "triangulo", "quadrado", "losango")
ROTULOS_VARIANTE: Final = {
    "repl": "REPL (Code Mode)",
    "json": "Tool calling em JSON",
    "json-agregacao": "Tool calling em JSON, com agregação",
}
# Aqua para a variante JSON com agregação (T18a). Fora da PALETA, que segue igual para o REPL.
COR_JSON_AGREGACAO: Final = "#2bb5c8"
TETO_NOTA_CURTA: Final = 60
PASTA_GRAFICOS: Final = Path("docs") / "relatorios" / "graficos"
PASTA_COMPARACAO: Final = Path("docs") / "relatorios" / "comparacao-variantes"
NOME_PAGINA: Final = "graficos.html"
NOME_COMPARACAO: Final = "comparacao-de-variantes.svg"
ALTURA_COMPARACAO: Final = 600
NOME_ESCALA_PAINEIS: Final = "escala-paineis.svg"
ALTURA_ESCALA_PAINEIS: Final = 472
# (título da seção, frase curta de leitura) da comparação, que vai antes das demais na página.
SECAO_COMPARACAO: Final = (
    "Comparação entre variantes",
    "Cada barra é a execução mais recente de uma variante. Os seis painéis têm escala própria: "
    "leia o valor escrito sobre cada barra.",
)
SECAO_ESCALA: Final = (
    "Escala dos dados",
    "Métricas por número de linhas de pedidos em escala logarítmica, comparando as variantes.",
)
SECAO_ESCALA_PAINEIS: Final = (
    "Comparação em escala",
    "Cada linha é uma variante; o eixo horizontal mostra o número de linhas de pedidos em escala "
    "logarítmica. Os quatro painéis têm escala vertical própria.",
)
NOMES_ESCALA: Final = (
    "escala-tokens-por-rodada.svg",
    "escala-custo-por-rodada.svg",
    "escala-pico-contexto.svg",
    "escala-taxa-acerto.svg",
)
SECOES_ESCALA: Final = (
    (
        "escala-tokens-por-rodada.svg",
        "Tokens de entrada por rodada por escala",
        "Tokens de entrada totais divididos pelas rodadas executadas, em milhares, por número de "
        "linhas de pedidos (escala logarítmica).",
    ),
    (
        "escala-custo-por-rodada.svg",
        "Custo por rodada por escala",
        "Custo total dividido pelas rodadas executadas, em milésimos de dólar, por número de "
        "linhas de pedidos (escala logarítmica).",
    ),
    (
        "escala-pico-contexto.svg",
        "Pico de contexto médio por escala",
        "Maior número de tokens de entrada em um turno, em média nas rodadas válidas, por número "
        "de linhas de pedidos (escala logarítmica).",
    ),
    (
        "escala-taxa-acerto.svg",
        "Taxa de acerto por escala",
        "Porcentagem de rodadas aprovadas entre as válidas, por número de linhas de pedidos "
        "(escala logarítmica).",
    ),
)
NOMES_ARQUIVOS: Final = (
    "taxa-total.svg",
    "taxa-por-categoria.svg",
    "iteracoes-por-pedido.svg",
    "dispersao-iteracoes-tokens.svg",
    "curva-de-contexto.svg",
    "custo-por-rodada.svg",
)
# (arquivo, título da seção, frase curta de leitura), na ordem de NOMES_ARQUIVOS.
SECOES: Final = (
    (
        "taxa-total.svg",
        "Taxa de acerto por execução",
        "Cada ponto é uma execução real. Quanto mais alto, mais rodadas aprovadas. Cada variante "
        "tem a sua linha.",
    ),
    (
        "taxa-por-categoria.svg",
        "Taxa de acerto por categoria",
        "Uma linha por categoria, nas mesmas execuções da variante REPL.",
    ),
    (
        "iteracoes-por-pedido.svg",
        "Iterações por pedido",
        "Quantas chamadas ao modelo cada pedido levou, em média. Menos iterações custam menos.",
    ),
    (
        "dispersao-iteracoes-tokens.svg",
        "Iterações contra tokens de entrada",
        "Cada ponto é uma execução: mais à direita, mais iterações; mais alto, mais tokens por "
        "rodada.",
    ),
    (
        "curva-de-contexto.svg",
        "Contexto ao longo dos turnos",
        "Quanto cresce o contexto enviado ao modelo a cada turno do agente.",
    ),
    (
        "custo-por-rodada.svg",
        "Custo por rodada",
        "Custo médio de uma rodada, em milésimos de dólar, por execução.",
    ),
)
_CSP: Final = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"
_CSS: Final = """\
:root { color-scheme: light; }
body { margin: 0; background: #f4f4f1; color: #0b0b0b;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 8px; }
h2 { font-size: 17px; font-weight: 600; margin: 32px 0 4px; }
.intro, .leitura { color: #52514e; font-size: 14px; line-height: 1.5; margin: 4px 0 12px; }
section svg { display: block; width: 100%; max-width: 100%; height: auto; }
.dica { position: absolute; z-index: 10; max-width: 280px; padding: 6px 8px; border-radius: 6px;
  background: #0b0b0b; color: #ffffff; font-size: 12px; line-height: 1.4; pointer-events: none; }
.dica[hidden] { display: none; }
"""
_SCRIPT: Final = """\
(function () {
  'use strict';
  var balao = document.createElement('div');
  balao.className = 'dica';
  balao.setAttribute('role', 'tooltip');
  balao.hidden = true;
  document.body.appendChild(balao);
  var ativo = null;

  function achar(alvo) {
    var n = alvo;
    while (n && n.nodeType === 1 && n.localName !== 'svg') {
      for (var i = 0; i < n.children.length; i++) {
        if (n.children[i].localName === 'title') {
          return { titulo: n.children[i] };
        }
      }
      n = n.parentNode;
    }
    return null;
  }

  function posicionar(x, y) {
    var w = balao.offsetWidth;
    var h = balao.offsetHeight;
    var esquerda = x + 14;
    var topo = y + 14;
    if (esquerda + w > window.innerWidth - 8) esquerda = Math.max(8, x - w - 14);
    if (topo + h > window.innerHeight - 8) topo = Math.max(8, y - h - 14);
    balao.style.left = (esquerda + window.scrollX) + 'px';
    balao.style.top = (topo + window.scrollY) + 'px';
  }

  function esconder() {
    if (!ativo) return;
    ativo.titulo.textContent = ativo.texto;
    ativo = null;
    balao.hidden = true;
  }

  function mostrar(achado, x, y) {
    if (ativo && ativo.titulo === achado.titulo) {
      posicionar(x, y);
      return;
    }
    esconder();
    ativo = { titulo: achado.titulo, texto: achado.titulo.textContent };
    achado.titulo.textContent = '';
    balao.textContent = ativo.texto;
    balao.hidden = false;
    posicionar(x, y);
  }

  document.addEventListener('pointerover', function (e) {
    if (e.pointerType !== 'mouse') return;
    var achado = achar(e.target);
    if (achado) mostrar(achado, e.clientX, e.clientY); else esconder();
  });
  document.addEventListener('pointermove', function (e) {
    if (e.pointerType === 'mouse' && ativo) posicionar(e.clientX, e.clientY);
  });
  document.addEventListener('pointerout', function (e) {
    if (e.pointerType === 'mouse' && !e.relatedTarget) esconder();
  });
  document.addEventListener('pointerdown', function (e) {
    if (e.pointerType !== 'touch') return;
    var achado = achar(e.target);
    if (achado) mostrar(achado, e.clientX, e.clientY); else esconder();
  });
  document.addEventListener('focusin', function (e) {
    var achado = achar(e.target);
    if (!achado || !e.target.getBoundingClientRect) return;
    var r = e.target.getBoundingClientRect();
    mostrar(achado, r.left + r.width / 2, r.top);
  });
  document.addEventListener('focusout', esconder);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') esconder();
  });
})();
"""


class ErroDeGraficos(Exception):
    """Dados ausentes ou inválidos para os gráficos. A mensagem diz o problema, sem o conteúdo."""


class CasoDetalhe(BaseModel):
    """Resumo de um caso dentro de uma execução, com contagens e médias das rodadas válidas."""

    model_config = ConfigDict(frozen=True)

    id: str
    categoria: str
    aprovadas: int = Field(default=0, ge=0)
    reprovadas: int = Field(default=0, ge=0)
    erro_infra: int = Field(default=0, ge=0)
    iteracoes_media: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tokens_entrada_medio: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Execucao(BaseModel):
    """Métricas de uma execução real da avaliação. Campos opcionais têm valor padrão."""

    model_config = ConfigDict(frozen=True)

    numero: int = Field(ge=1)
    variante: str = Field(default="repl", min_length=1)
    relatorio: str = ""
    casos: int = Field(default=0, ge=0)
    rodadas: int = Field(default=0, ge=0)
    rodadas_validas: int = Field(default=0, ge=0)
    aprovadas: int = Field(default=0, ge=0)
    reprovadas: int = Field(default=0, ge=0)
    erro_infra: int = Field(default=0, ge=0)
    nao_executadas: int = Field(default=0, ge=0)
    taxa_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    erro_infra_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    iteracoes_media: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tokens_entrada_por_rodada: int | None = Field(default=None, ge=0)
    tokens_saida_por_rodada: int | None = Field(default=None, ge=0)
    custo_por_rodada_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duracao_total_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    categorias_pct: dict[str, float | None] = Field(default_factory=dict)
    casos_detalhe: tuple[CasoDetalhe, ...] = ()
    prompt: str = ""
    ponto_fraco: bool = False
    nota: str = ""
    incompleta: bool = False
    motivo_incompleta: str = ""
    # Campos da comparação de variantes (Ajuste 2). Opcionais: o arquivo antigo não os tem.
    pico_contexto_medio: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    caracteres_observacao_medio: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    custo_por_acerto_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Campos do modo escala (T18c). Opcionais: relatórios antigos não os têm.
    linhas_pedidos: int | None = Field(default=None, ge=1)
    tokens_entrada_por_rodada_executada: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    custo_por_rodada_executada_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    iniciado_em: str | None = None


class CurvaDeContexto(BaseModel):
    """Tokens de entrada por turno de uma curva; `pontos` são pares (turno, tokens)."""

    model_config = ConfigDict(frozen=True)

    nome: str = Field(min_length=1)
    variante: str = Field(default="repl", min_length=1)
    pontos: tuple[tuple[int, int], ...] = ()
    rodadas_por_ponto_minimo: int | None = Field(default=None, ge=0)


class DadosEvolucao(BaseModel):
    """Arquivo inteiro de métricas. `numero` é único dentro de cada variante."""

    model_config = ConfigDict(frozen=True)

    versao: Literal[1]
    descricao: str = ""
    execucoes: tuple[Execucao, ...] = ()
    curvas_de_contexto: tuple[CurvaDeContexto, ...] = ()

    @model_validator(mode="after")
    def _numeros_unicos(self) -> Self:
        vistos: set[tuple[str, int]] = set()
        for execucao in self.execucoes:
            chave = (execucao.variante, execucao.numero)
            if chave in vistos:
                raise ValueError(
                    f"número {execucao.numero} repetido na variante {execucao.variante!r}"
                )
            vistos.add(chave)
        return self


def _descrever_erro(erro: Mapping[str, object]) -> str:
    loc = erro.get("loc")
    campo = ".".join(str(parte) for parte in loc) if isinstance(loc, tuple) and loc else "raiz"
    return f"{campo}: {erro.get('msg')}"


def carregar_dados(caminho: Path) -> DadosEvolucao:
    """Lê e valida o arquivo de métricas. Erro de leitura, JSON ou esquema vira ErroDeGraficos."""
    try:
        texto = caminho.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ErroDeGraficos(f"arquivo de dados não encontrado: {caminho}") from None
    except (OSError, UnicodeDecodeError) as erro:
        motivo = getattr(erro, "strerror", None) or type(erro).__name__
        raise ErroDeGraficos(f"não foi possível ler {caminho}: {motivo}") from None
    try:
        bruto: object = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroDeGraficos(
            f"{caminho} não é JSON válido (linha {erro.lineno}, coluna {erro.colno})"
        ) from None
    try:
        return DadosEvolucao.model_validate(bruto)
    except ValidationError as erro:
        detalhes = "; ".join(_descrever_erro(e) for e in erro.errors()[:3])
        raise ErroDeGraficos(f"{caminho} tem esquema inválido: {detalhes}") from None


# --- escalas e ticks --------------------------------------------------------------------------


def _ler_relatorio_de_arquivo(caminho: Path) -> Relatorio:
    """Relatório de um arquivo. Erro vira ErroDeGraficos com só o nome do arquivo na mensagem."""
    nome = caminho.name
    try:
        texto = caminho.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ErroDeGraficos(f"relatório não encontrado: {nome}") from None
    except (OSError, UnicodeDecodeError):
        raise ErroDeGraficos(f"não foi possível ler o relatório {nome}") from None
    try:
        return relatorio_de_json(texto)
    except ValidationError:
        raise ErroDeGraficos(f"{nome} não é um relatório de avaliação válido") from None


def _media_de(valores: Sequence[float]) -> float | None:
    return statistics.fmean(valores) if valores else None


def _taxa_pct(aprovadas: int, validas: int) -> float | None:
    return aprovadas / validas * 100 if validas else None


def _motivo_incompleta(relatorio: Relatorio) -> str:
    """Descreve por que a execução foi incompleta, ou vazio quando completa."""
    partes: list[str] = []
    if relatorio.teto_estourado:
        partes.append("teto de gasto estourado")
    if relatorio.nao_executadas > 0:
        rotulo = (
            "rodada não executada" if relatorio.nao_executadas == 1 else "rodadas não executadas"
        )
        partes.append(f"{_inteiro_br(relatorio.nao_executadas)} {rotulo}")
    if relatorio.erros_infra > 0:
        rotulo = (
            "erro de infraestrutura" if relatorio.erros_infra == 1 else "erros de infraestrutura"
        )
        partes.append(f"{_inteiro_br(relatorio.erros_infra)} {rotulo}")
    if not partes:
        return ""
    return "execução incompleta: " + "; ".join(partes)


_LINHAS_PEDIDOS_PADRAO: Final = 60  # escala 1x do conjunto padrão de pedidos (dados.py)


def _detalhes_casos(relatorio: Relatorio) -> tuple[CasoDetalhe, ...]:
    """Monta os resumos por caso para os casos que possuem rodadas."""
    detalhes: list[CasoDetalhe] = []
    for caso in relatorio.casos:
        if not caso.rodadas:
            continue
        validas_caso = [
            r
            for r in caso.rodadas
            if r.classificacao in (Classificacao.APROVADA, Classificacao.REPROVADA)
        ]
        detalhes.append(
            CasoDetalhe(
                id=caso.caso_id,
                categoria=caso.categoria.value,
                aprovadas=sum(1 for r in validas_caso if r.classificacao is Classificacao.APROVADA),
                reprovadas=sum(
                    1 for r in validas_caso if r.classificacao is Classificacao.REPROVADA
                ),
                erro_infra=sum(
                    1 for r in caso.rodadas if r.classificacao is Classificacao.ERRO_INFRA
                ),
                iteracoes_media=_media_de([float(r.iteracoes) for r in validas_caso]),
                tokens_entrada_medio=_media_de([float(r.tokens_entrada) for r in validas_caso]),
            )
        )
    return tuple(detalhes)


def _execucao_de_relatorio(numero: int, nome: str, relatorio: Relatorio) -> Execucao:
    """Uma execução a partir de um relatório. Médias são sobre as rodadas válidas."""
    todas = [rodada for caso in relatorio.casos for rodada in caso.rodadas]
    validas = [
        r for r in todas if r.classificacao in (Classificacao.APROVADA, Classificacao.REPROVADA)
    ]
    aprovadas = sum(1 for r in validas if r.classificacao is Classificacao.APROVADA)
    reprovadas = sum(1 for r in validas if r.classificacao is Classificacao.REPROVADA)
    erros = sum(1 for r in todas if r.classificacao is Classificacao.ERRO_INFRA)
    nao_executadas = sum(1 for r in todas if r.classificacao is Classificacao.NAO_EXECUTADA)
    duracao_soma = sum(r.duracao_s for r in todas)
    duracao_total_s = duracao_soma if duracao_soma > 0 else None
    total = len(todas)
    base_legivel = relatorio.linha_de_base or nome
    # Por categoria: só casos com rodadas entram, e cada um conta as suas rodadas válidas.
    categorias = sorted({caso.categoria.value for caso in relatorio.casos if caso.rodadas})
    categorias_pct: dict[str, float | None] = {}
    for categoria in categorias:
        do_grupo = [
            r
            for caso in relatorio.casos
            if caso.categoria.value == categoria
            for r in caso.rodadas
            if r.classificacao in (Classificacao.APROVADA, Classificacao.REPROVADA)
        ]
        ganhas = sum(1 for r in do_grupo if r.classificacao is Classificacao.APROVADA)
        categorias_pct[categoria] = _taxa_pct(ganhas, len(do_grupo))
    fraco = total > 0 and len(validas) * 2 < total
    motivo_incompleta = _motivo_incompleta(relatorio)
    incompleta = bool(motivo_incompleta)
    custos = [r.custo for r in validas if r.custo is not None]
    executadas = sum(
        1
        for r in todas
        if r.classificacao
        in (Classificacao.APROVADA, Classificacao.REPROVADA, Classificacao.ERRO_INFRA)
    )
    tokens_executada = relatorio.tokens_entrada_total / executadas if executadas else None
    custo_executada = (
        relatorio.custo_total / executadas
        if executadas and relatorio.custo_total is not None
        else None
    )
    return Execucao(
        numero=numero,
        variante=relatorio.variante,
        relatorio=nome,
        casos=sum(1 for caso in relatorio.casos if caso.rodadas),
        rodadas=total,
        rodadas_validas=len(validas),
        aprovadas=aprovadas,
        reprovadas=reprovadas,
        erro_infra=erros,
        nao_executadas=nao_executadas,
        taxa_pct=_taxa_pct(aprovadas, len(validas)),
        erro_infra_pct=erros / total * 100 if total else None,
        iteracoes_media=_media_de([float(r.iteracoes) for r in validas]),
        tokens_entrada_por_rodada=_inteiro_ou_none([r.tokens_entrada for r in validas]),
        tokens_saida_por_rodada=_inteiro_ou_none([r.tokens_saida for r in validas]),
        custo_por_rodada_usd=_media_de(custos),
        duracao_total_s=duracao_total_s,
        categorias_pct=categorias_pct,
        casos_detalhe=_detalhes_casos(relatorio),
        prompt=(
            f"prompt {relatorio.hash_prompt[:8]}"
            if relatorio.hash_prompt
            else "sem prompt registrado"
        ),
        ponto_fraco=fraco,
        nota=(
            f"{base_legivel}: {len(validas)} de {total} rodadas válidas" if fraco else base_legivel
        ),
        incompleta=incompleta,
        motivo_incompleta=motivo_incompleta,
        pico_contexto_medio=relatorio.pico_contexto_medio,
        caracteres_observacao_medio=relatorio.caracteres_observacao_medio,
        custo_por_acerto_usd=relatorio.custo_por_acerto,
        linhas_pedidos=(
            relatorio.linhas_pedidos
            if relatorio.linhas_pedidos is not None
            else _LINHAS_PEDIDOS_PADRAO
        ),
        tokens_entrada_por_rodada_executada=tokens_executada,
        custo_por_rodada_executada_usd=custo_executada,
        iniciado_em=relatorio.iniciado_em,
    )


def _inteiro_ou_none(valores: Sequence[int]) -> int | None:
    media = _media_de([float(v) for v in valores])
    return round(media) if media is not None else None


def dados_de_relatorios(caminhos: Sequence[Path]) -> DadosEvolucao:
    """Monta os dados dos gráficos a partir de relatórios de execução, um por arquivo.

    A ordem dos arquivos define o `numero`, sequencial dentro de cada variante a partir de 1.
    Relatório ilegível ou inválido levanta `ErroDeGraficos`, citando só o nome do arquivo.
    """
    if not caminhos:
        raise ErroDeGraficos("nenhum relatório informado")
    lidos = [(caminho.name, _ler_relatorio_de_arquivo(caminho)) for caminho in caminhos]
    contagem: dict[str, int] = {}
    numerados: list[tuple[str, Relatorio, int]] = []
    for nome, relatorio in lidos:
        contagem[relatorio.variante] = contagem.get(relatorio.variante, 0) + 1
        numerados.append((nome, relatorio, contagem[relatorio.variante]))
    execucoes = tuple(
        _execucao_de_relatorio(numero, nome, relatorio) for nome, relatorio, numero in numerados
    )
    curvas = tuple(
        CurvaDeContexto(
            nome=_rotulo_variante(relatorio.variante)
            + (f" execução {numero}" if contagem[relatorio.variante] > 1 else ""),
            variante=relatorio.variante,
            pontos=relatorio.curva_de_contexto,
            rodadas_por_ponto_minimo=MINIMO_RODADAS_PARA_CURVA,
        )
        for _, relatorio, numero in numerados
        if relatorio.curva_de_contexto
    )
    return DadosEvolucao(
        versao=1,
        descricao="Comparação a partir de relatórios de avaliação, uma execução por arquivo.",
        execucoes=execucoes,
        curvas_de_contexto=curvas,
    )


@dataclass(frozen=True)
class _Eixo:
    lo: float
    hi: float
    valores: tuple[float, ...]
    rotulos: tuple[str, ...]


def _passo_bonito(intervalo: float, alvo: int = 6) -> float:
    """Passo 1, 2, 5, 10... (vezes uma potência de dez) que cobre o intervalo em cerca de `alvo`."""
    # Piso de 1e-12: valores quase zero (ou subnormais) não podem gerar passo zero.
    bruto = max(intervalo / alvo, 1e-12)
    potencia = 10.0 ** math.floor(math.log10(bruto))
    for multiplicador in (1, 2, 5, 10):
        candidato = multiplicador * potencia
        if candidato >= bruto * (1 - 1e-9):
            return candidato
    return 10 * potencia


def _numero_eixo(valor: float, casas: int, separador: bool) -> str:
    if casas == 0 and separador:
        return f"{round(valor):,}".replace(",", ".")
    return f"{valor:.{casas}f}".replace(".", ",")


def _eixo(lo: float, hi: float, *, inteiro: bool = False, separador: bool = False) -> _Eixo:
    passo = _passo_bonito(hi - lo)
    if inteiro:
        passo = float(max(1, math.ceil(passo)))
    k_min = math.ceil(lo / passo - 1e-9)
    k_max = math.floor(hi / passo + 1e-9)
    valores = tuple(round(k * passo, 12) for k in range(k_min, k_max + 1))
    casas = 0 if passo >= 1 else -math.floor(math.log10(passo))
    rotulos = tuple(_numero_eixo(valor, casas, separador) for valor in valores)
    return _Eixo(lo, hi, valores, rotulos)


def _teto(maximo: float, *, inteiro: bool = False) -> float:
    """Limite superior do eixo: o máximo com 10% de folga, arredondado para cima no passo."""
    if maximo <= 1e-9:
        return 1.0
    alvo = maximo * 1.1
    passo = _passo_bonito(alvo)
    if inteiro:
        passo = float(max(1, math.ceil(passo)))
    return math.ceil(alvo / passo - 1e-9) * passo


def _eixo_execucoes(maior_numero: int) -> _Eixo:
    passo = 1 if maior_numero <= 20 else math.ceil(maior_numero / 20)
    valores = tuple(float(numero) for numero in range(1, maior_numero + 1, passo))
    return _Eixo(0.5, maior_numero + 0.5, valores, tuple(str(int(v)) for v in valores))


def _px(valor: float, eixo: _Eixo, inicio: float, fim: float) -> float:
    return inicio + (valor - eixo.lo) / (eixo.hi - eixo.lo) * (fim - inicio)


def _c(valor: float) -> str:
    """Coordenada do SVG: ponto decimal, no máximo duas casas, sem `-0`."""
    texto = f"{valor:.2f}".rstrip("0").rstrip(".")
    return "0" if texto in ("-0", "") else texto


def _br(valor: float, casas: int) -> str:
    return f"{valor:.{casas}f}".replace(".", ",")


def _inteiro_br(valor: float) -> str:
    return f"{round(valor):,}".replace(",", ".")


def _esc(texto: str) -> str:
    return escape(texto, {'"': "&quot;", "'": "&#39;"})


# --- séries, legenda e desenho ----------------------------------------------------------------


@dataclass(frozen=True)
class _Ponto:
    x: float
    y: float
    oco: bool = False
    rotulo: str | None = None
    posicao: Literal["cima", "direita"] = "cima"
    dica: str | None = None


@dataclass(frozen=True)
class _Serie:
    nome: str
    cor: str
    pontos: tuple[_Ponto | None, ...]
    forma: str = "circulo"
    linha: bool = True


@dataclass(frozen=True)
class _Item:
    texto: str
    cor: str
    forma: str = "circulo"
    linha: bool = True
    oco: bool = False


def _marcador(forma: str, x: float, y: float, cor: str, oco: bool, dica: str | None = None) -> str:
    """Marcador de 4,5 px de raio. Com `dica`, ganha `<title>` (tooltip) e foco por teclado.

    Cheio: preenchimento da cor com anel de 2 px da cor do fundo. Vazado: fundo branco, contorno
    da cor (ponto fraco).
    """
    if oco:
        estilo = f'fill="{COR_FUNDO}" stroke="{cor}" stroke-width="2"'
    else:
        estilo = f'fill="{cor}" stroke="{COR_FUNDO}" stroke-width="2"'
    if forma == "triangulo":
        pontos = [(x, y - 5), (x + 4.5, y + 3.5), (x - 4.5, y + 3.5)]
        nome, atributos = "polygon", f'points="{_pontos_svg(pontos)}"'
    elif forma == "quadrado":
        nome = "rect"
        atributos = f'x="{_c(x - 4)}" y="{_c(y - 4)}" width="8" height="8"'
    elif forma == "losango":
        pontos = [(x, y - 5), (x + 5, y), (x, y + 5), (x - 5, y)]
        nome, atributos = "polygon", f'points="{_pontos_svg(pontos)}"'
    else:
        nome, atributos = "circle", f'cx="{_c(x)}" cy="{_c(y)}" r="4.5"'
    if dica is None:
        return f"<{nome} {atributos} {estilo}/>"
    return f'<{nome} {atributos} {estilo} tabindex="0"><title>{_esc(dica)}</title></{nome}>'


def _pontos_svg(pontos: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{_c(a)},{_c(b)}" for a, b in pontos)


def _cabecalho(titulo: str, descricao: str, altura: int = ALTURA) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{LARGURA}" height="{altura}" '
        f'viewBox="0 0 {LARGURA} {altura}" role="img" aria-labelledby="t d" '
        f'font-family="{FONTE}">',
        f'<title id="t">{_esc(titulo)}</title>',
        f'<desc id="d">{_esc(descricao)}</desc>',
        f'<rect x="0.5" y="0.5" width="{LARGURA - 1}" height="{altura - 1}" rx="8" '
        f'fill="{COR_FUNDO}" stroke="{COR_BORDA}" stroke-width="1"/>',
    ]


def _legenda(itens: Sequence[_Item]) -> tuple[list[str], float]:
    """Itens em linhas de até a largura útil. Devolve os elementos e o topo da área do gráfico."""
    elementos: list[str] = []
    x = 24.0
    y = 54.0
    for item in itens:
        largura = 22 + len(item.texto) * 6.5 + 18
        if x + largura > DIREITA and x > 24.0:
            x = 24.0
            y += 18
        if item.linha:
            elementos.append(
                f'<line x1="{_c(x)}" y1="{_c(y)}" x2="{_c(x + 16)}" y2="{_c(y)}" '
                f'stroke="{item.cor}" stroke-width="2" stroke-linecap="round"/>'
            )
        elementos.append(_marcador(item.forma, x + 8, y, item.cor, item.oco))
        elementos.append(
            f'<text x="{_c(x + 22)}" y="{_c(y + 4)}" font-size="12" '
            f'fill="{COR_EIXO_TEXTO}">{_esc(item.texto)}</text>'
        )
        x += largura
    return elementos, max(TOPO_MINIMO, y + 30)


def _grade(eixo_y: _Eixo, topo: float) -> list[str]:
    elementos: list[str] = []
    for valor, rotulo in zip(eixo_y.valores, eixo_y.rotulos, strict=True):
        y = _px(valor, eixo_y, BASE_Y, topo)
        if valor != eixo_y.lo:
            elementos.append(
                f'<line x1="{ESQUERDA}" y1="{_c(y)}" x2="{DIREITA}" y2="{_c(y)}" '
                f'stroke="{COR_GRADE}" stroke-width="1"/>'
            )
        elementos.append(
            f'<text x="{ESQUERDA - 8}" y="{_c(y + 4)}" text-anchor="end" font-size="11" '
            f'fill="{COR_TICK}">{_esc(rotulo)}</text>'
        )
    elementos.append(
        f'<line x1="{ESQUERDA}" y1="{BASE_Y}" x2="{DIREITA}" y2="{BASE_Y}" '
        f'stroke="{COR_BASE}" stroke-width="1"/>'
    )
    return elementos


def _rotulos_x(eixo_x: _Eixo) -> list[str]:
    elementos: list[str] = []
    for valor, rotulo in zip(eixo_x.valores, eixo_x.rotulos, strict=True):
        x = _px(valor, eixo_x, ESQUERDA, DIREITA)
        elementos.append(
            f'<text x="{_c(x)}" y="366" text-anchor="middle" font-size="11" '
            f'fill="{COR_TICK}">{_esc(rotulo)}</text>'
        )
    return elementos


def _series(series: Sequence[_Serie], eixo_x: _Eixo, eixo_y: _Eixo, topo: float) -> list[str]:
    """Linhas (por segmento, sem gaps), depois marcadores e rótulos de valor."""
    linhas: list[str] = []
    marcadores: list[str] = []
    rotulos: list[str] = []
    for serie in series:
        segmentos: list[list[tuple[float, float]]] = [[]]
        for ponto in serie.pontos:
            if ponto is None:
                segmentos.append([])
                continue
            x = _px(ponto.x, eixo_x, ESQUERDA, DIREITA)
            y = _px(ponto.y, eixo_y, BASE_Y, topo)
            segmentos[-1].append((x, y))
            marcadores.append(_marcador(serie.forma, x, y, serie.cor, ponto.oco, ponto.dica))
            if ponto.rotulo is not None:
                rotulos.append(_rotulo(ponto, x, y))
        if serie.linha:
            for segmento in segmentos:
                if len(segmento) >= 2:
                    trecho = " L".join(f"{_c(x)} {_c(y)}" for x, y in segmento)
                    linhas.append(
                        f'<path d="M{trecho}" fill="none" stroke="{serie.cor}" '
                        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
                    )
    return linhas + marcadores + rotulos


def _rotulo(ponto: _Ponto, x: float, y: float) -> str:
    texto = _esc(ponto.rotulo or "")
    if ponto.posicao == "direita":
        return (
            f'<text x="{_c(x + 7)}" y="{_c(y + 4)}" font-size="11" '
            f'fill="{COR_EIXO_TEXTO}">{texto}</text>'
        )
    return (
        f'<text x="{_c(x)}" y="{_c(y - 9)}" text-anchor="middle" font-size="11" '
        f'fill="{COR_TEXTO}">{texto}</text>'
    )


def _svg(
    *,
    titulo: str,
    descricao: str,
    eixo_x: _Eixo,
    eixo_y: _Eixo,
    titulo_x: str,
    titulo_y: str,
    series: Sequence[_Serie],
    legenda: Sequence[_Item],
) -> str:
    elementos = _cabecalho(titulo, descricao)
    elementos.append(
        f'<text x="24" y="34" font-size="16" font-weight="500" fill="{COR_TEXTO}">'
        f"{_esc(titulo)}</text>"
    )
    legenda_svg, topo = _legenda(legenda)
    elementos.extend(legenda_svg)
    elementos.extend(_grade(eixo_y, topo))
    elementos.extend(_rotulos_x(eixo_x))
    meio = (topo + BASE_Y) / 2
    elementos.append(
        f'<text x="22" y="{_c(meio)}" transform="rotate(-90 22 {_c(meio)})" '
        f'text-anchor="middle" font-size="11" fill="{COR_EIXO_TEXTO}">{_esc(titulo_y)}</text>'
    )
    elementos.append(
        f'<text x="{(ESQUERDA + DIREITA) // 2}" y="386" text-anchor="middle" font-size="11" '
        f'fill="{COR_EIXO_TEXTO}">{_esc(titulo_x)}</text>'
    )
    elementos.extend(_series(series, eixo_x, eixo_y, topo))
    elementos.append("</svg>")
    return "\n".join(elementos) + "\n"


def _svg_sem_dados(titulo: str) -> str:
    elementos = _cabecalho(titulo, "Sem dados para este gráfico.")
    elementos.append(
        f'<text x="24" y="34" font-size="16" font-weight="500" fill="{COR_TEXTO}">'
        f"{_esc(titulo)}</text>"
    )
    elementos.append(
        f'<text x="{LARGURA // 2}" y="{ALTURA // 2}" text-anchor="middle" font-size="14" '
        f'fill="{COR_EIXO_TEXTO}">sem dados</text>'
    )
    elementos.append("</svg>")
    return "\n".join(elementos) + "\n"


def _rotulo_variante(variante: str) -> str:
    return ROTULOS_VARIANTE.get(variante, variante)


def _variantes(execucoes: Iterable[Execucao]) -> list[str]:
    """REPL primeiro, depois JSON, depois as demais em ordem alfabética."""
    presentes = {execucao.variante for execucao in execucoes}
    prioritarias = [v for v in ("repl", "json") if v in presentes]
    return prioritarias + sorted(presentes - {"repl", "json"})


def _cores_variantes(variantes: Sequence[str]) -> dict[str, str]:
    cores: dict[str, str] = {}
    extras = PALETA[2:]
    for variante in variantes:
        if variante == "repl":
            cores[variante] = PALETA[0]
        elif variante == "json":
            cores[variante] = PALETA[1]
        elif variante == "json-agregacao":
            cores[variante] = COR_JSON_AGREGACAO
        else:
            cores[variante] = extras[len(cores) % len(extras)]
    return cores


def _ordenadas(execucoes: Iterable[Execucao]) -> list[Execucao]:
    return sorted(execucoes, key=lambda e: (e.variante, e.numero))


def _maior_numero(execucoes: Iterable[Execucao]) -> int:
    return max(execucao.numero for execucao in execucoes)


def _rotulo_execucao(execucao: Execucao, com_variante: bool) -> str:
    base = f"Execução {execucao.numero}"
    return f"{base} ({_rotulo_variante(execucao.variante)})" if com_variante else base


def _eh_oco(execucao: Execucao) -> bool:
    return execucao.ponto_fraco or execucao.incompleta


def _dica(texto: str, execucao: Execucao) -> str:
    """Tooltip de uma execução. Marcador vazado acrescenta a nota e/ou o motivo de incompletude."""
    ressalva = _motivos_ressalva(execucao)
    if not ressalva:
        return texto
    return f"{texto}. Marcador vazado: {ressalva}"


def _fmt_taxa(valor: float) -> str:
    return f"{_br(valor, 1)}%"


def _fmt_iteracoes(valor: float) -> str:
    return _br(valor, 2)


def _fmt_custo(valor: float) -> str:
    return _br(valor, 3)


def _fmt_tokens(valor: float) -> str:
    return _inteiro_br(valor)


def _series_por_variante(
    execucoes: Sequence[Execucao],
    valor: Callable[[Execucao], float | None],
    formatar: Callable[[float], str],
    dica: Callable[[str, Execucao, float], str],
) -> list[_Serie]:
    """Uma série por variante. Com mais de duas séries, os valores não aparecem sobre os pontos."""
    variantes = _variantes(execucoes)
    cores = _cores_variantes(variantes)
    com_rotulo = len(variantes) <= 2
    com_variante = len(variantes) > 1
    series: list[_Serie] = []
    for variante in variantes:
        pontos: list[_Ponto | None] = []
        for execucao in execucoes:
            if execucao.variante != variante:
                continue
            medida = valor(execucao)
            if medida is None:
                pontos.append(None)
                continue
            rotulo_execucao = _rotulo_execucao(execucao, com_variante)
            pontos.append(
                _Ponto(
                    x=float(execucao.numero),
                    y=medida,
                    oco=_eh_oco(execucao),
                    rotulo=formatar(medida) if com_rotulo else None,
                    dica=dica(rotulo_execucao, execucao, medida),
                )
            )
        series.append(
            _Serie(nome=_rotulo_variante(variante), cor=cores[variante], pontos=tuple(pontos))
        )
    return series


def _itens_series(series: Sequence[_Serie]) -> list[_Item]:
    return [
        _Item(texto=serie.nome, cor=serie.cor, forma=serie.forma, linha=serie.linha)
        for serie in series
    ]


def _item_ponto_fraco(plotadas: Sequence[Execucao]) -> list[_Item]:
    """Legenda do marcador vazado, se alguma execução plotada é ponto fraco ou incompleta."""
    vazadas = [execucao for execucao in plotadas if _eh_oco(execucao)]
    if not vazadas:
        return []
    fracas = [execucao for execucao in vazadas if execucao.ponto_fraco]
    incompletas = [execucao for execucao in vazadas if execucao.incompleta]
    if incompletas and not fracas:
        texto = (
            f"marcador vazado: execução {incompletas[0].numero} incompleta"
            if len(incompletas) == 1
            else "marcador vazado: execuções incompletas"
        )
    elif incompletas and fracas:
        texto = "marcador vazado: execução incompleta ou com poucas rodadas válidas"
    elif len(fracas) == 1 and 0 < len(fracas[0].nota) <= TETO_NOTA_CURTA:
        texto = f"marcador vazado: execução {fracas[0].numero}: {fracas[0].nota}"
    else:
        numeros = ", ".join(str(execucao.numero) for execucao in fracas)
        if len(fracas) == 1:
            texto = f"marcador vazado: execução {numeros} com poucas rodadas válidas"
        else:
            texto = f"marcador vazado: execuções {numeros} com poucas rodadas válidas"
    return [_Item(texto=texto, cor=COR_EIXO_TEXTO, linha=False, oco=True)]


def _descricao(
    base: str,
    series: Sequence[_Serie],
    prefixo: str,
    formatar: Callable[[float], str],
) -> str:
    trechos: list[str] = []
    for serie in series:
        valores = [
            f"{prefixo} {round(ponto.x)} {formatar(ponto.y)}"
            for ponto in serie.pontos
            if ponto is not None
        ]
        trechos.append(f"{serie.nome}: " + (", ".join(valores) if valores else "sem valores"))
    return f"{base}. " + "; ".join(trechos) + "."


# --- gráficos ---------------------------------------------------------------------------------


def grafico_taxa_total(dados: DadosEvolucao) -> str:
    """Taxa de acerto (%) por execução, uma série por variante. Y de 0 a 105."""
    titulo = "Taxa de acerto por execução"
    execucoes = _ordenadas(dados.execucoes)
    if not any(e.taxa_pct is not None for e in execucoes):
        return _svg_sem_dados(titulo)
    series = _series_por_variante(
        execucoes,
        lambda e: e.taxa_pct,
        _fmt_taxa,
        lambda rotulo, e, v: _dica(
            f"{rotulo}: {_fmt_taxa(v)} de acerto ({e.aprovadas} de {e.rodadas_validas} rodadas)",
            e,
        ),
    )
    plotadas = [e for e in execucoes if e.taxa_pct is not None]
    return _svg(
        titulo=titulo,
        descricao=_descricao(
            "Taxa de acerto por execução, em porcentagem", series, "execução", _fmt_taxa
        ),
        eixo_x=_eixo_execucoes(_maior_numero(execucoes)),
        eixo_y=_eixo(0.0, 105.0),
        titulo_x="Execução",
        titulo_y="Rodadas aprovadas (%)",
        series=series,
        legenda=[*_itens_series(series), *_item_ponto_fraco(plotadas)],
    )


def grafico_taxa_por_categoria(dados: DadosEvolucao, variante: str = "repl") -> str:
    """Taxa (%) de cada categoria por execução da variante pedida. Uma linha por categoria."""
    titulo = f"Taxa de acerto por categoria: {_rotulo_variante(variante)}"
    execucoes = [e for e in _ordenadas(dados.execucoes) if e.variante == variante]
    categorias = sorted({c for e in execucoes for c in e.categorias_pct})
    if not any(e.categorias_pct.get(c) is not None for e in execucoes for c in categorias):
        return _svg_sem_dados(titulo)
    series: list[_Serie] = []
    for indice, categoria in enumerate(categorias):
        nome = categoria[:1].upper() + categoria[1:]
        pontos: list[_Ponto | None] = []
        for execucao in execucoes:
            medida = execucao.categorias_pct.get(categoria)
            if medida is None:
                pontos.append(None)
                continue
            texto = f"Execução {execucao.numero}, {nome}: {_fmt_taxa(medida)} de acerto"
            pontos.append(
                _Ponto(
                    float(execucao.numero),
                    medida,
                    oco=_eh_oco(execucao),
                    dica=_dica(texto, execucao),
                )
            )
        series.append(
            _Serie(
                nome=nome,
                cor=PALETA[indice % len(PALETA)],
                pontos=tuple(pontos),
                forma=FORMAS[indice % len(FORMAS)],
            )
        )
    plotadas = [e for e in execucoes if any(v is not None for v in e.categorias_pct.values())]
    return _svg(
        titulo=titulo,
        descricao=_descricao(
            f"Taxa de acerto por categoria, em porcentagem ({_rotulo_variante(variante)})",
            series,
            "execução",
            _fmt_taxa,
        ),
        eixo_x=_eixo_execucoes(_maior_numero(execucoes)),
        eixo_y=_eixo(0.0, 105.0),
        titulo_x="Execução",
        titulo_y="Rodadas aprovadas (%)",
        series=series,
        legenda=[*_itens_series(series), *_item_ponto_fraco(plotadas)],
    )


def grafico_iteracoes(dados: DadosEvolucao) -> str:
    """Iterações médias por pedido por execução, uma série por variante."""
    titulo = "Iterações por pedido, por execução"
    execucoes = _ordenadas(dados.execucoes)
    valores = [e.iteracoes_media for e in execucoes if e.iteracoes_media is not None]
    if not valores:
        return _svg_sem_dados(titulo)
    series = _series_por_variante(
        execucoes,
        lambda e: e.iteracoes_media,
        _fmt_iteracoes,
        lambda rotulo, e, v: _dica(f"{rotulo}: {_br(v, 1)} iterações por pedido", e),
    )
    plotadas = [e for e in execucoes if e.iteracoes_media is not None]
    return _svg(
        titulo=titulo,
        descricao=_descricao(
            "Iterações médias por pedido, por execução", series, "execução", _fmt_iteracoes
        ),
        eixo_x=_eixo_execucoes(_maior_numero(execucoes)),
        eixo_y=_eixo(0.0, _teto(max(valores))),
        titulo_x="Execução",
        titulo_y="Iterações por pedido",
        series=series,
        legenda=[*_itens_series(series), *_item_ponto_fraco(plotadas)],
    )


def _nome_prompt(execucao: Execucao) -> str:
    return execucao.prompt or "sem prompt"


def grafico_dispersao_iteracoes_tokens(dados: DadosEvolucao) -> str:
    """Iterações por pedido (X) contra tokens de entrada por rodada, em milhares (Y).

    Cor por prompt quando há uma só variante; por variante quando há várias. O número da execução
    fica ao lado de cada ponto.
    """
    titulo = "Iterações por pedido contra tokens de entrada"
    pares: list[tuple[Execucao, float, float]] = []
    for execucao in _ordenadas(dados.execucoes):
        it_media = execucao.iteracoes_media
        tok_entrada = execucao.tokens_entrada_por_rodada
        if it_media is not None and tok_entrada is not None:
            pares.append((execucao, it_media, float(tok_entrada)))
    if not pares:
        return _svg_sem_dados(titulo)
    plotadas = [execucao for execucao, _, _ in pares]
    variantes = _variantes(plotadas)
    com_variante = len(variantes) > 1
    grupos: list[tuple[str, str, list[tuple[Execucao, float, float]]]] = []
    if not com_variante:
        prompts = list(dict.fromkeys(_nome_prompt(e) for e in plotadas))
        for indice, prompt in enumerate(prompts):
            membros = [par for par in pares if _nome_prompt(par[0]) == prompt]
            grupos.append((prompt, PALETA[indice % len(PALETA)], membros))
    else:
        cores = _cores_variantes(variantes)
        for variante in variantes:
            membros = [par for par in pares if par[0].variante == variante]
            grupos.append((_rotulo_variante(variante), cores[variante], membros))

    series: list[_Serie] = []
    trechos: list[str] = []
    for nome, cor, membros in grupos:
        pontos: list[_Ponto] = []
        for execucao, iteracoes, tokens in membros:
            rotulo_execucao = _rotulo_execucao(execucao, com_variante)
            texto = (
                f"{rotulo_execucao}: {_br(iteracoes, 1)} iterações, "
                f"{_br(tokens / 1000, 1)} mil tokens de entrada"
            )
            pontos.append(
                _Ponto(
                    x=iteracoes,
                    y=tokens / 1000,
                    oco=_eh_oco(execucao),
                    rotulo=str(execucao.numero),
                    posicao="direita",
                    dica=_dica(texto, execucao),
                )
            )
            trechos.append(
                f"execução {execucao.numero} ({nome}): {_br(iteracoes, 2)} iterações e "
                f"{_br(tokens / 1000, 1)} mil tokens de entrada"
            )
        series.append(_Serie(nome=nome, cor=cor, pontos=tuple(pontos), linha=False))
    descricao = (
        "Iterações médias por pedido (horizontal) contra tokens de entrada por rodada "
        "(vertical), uma marca por execução com o número ao lado. " + "; ".join(trechos) + "."
    )
    return _svg(
        titulo=titulo,
        descricao=descricao,
        eixo_x=_eixo(0.0, _teto(max(iteracoes for _, iteracoes, _ in pares))),
        eixo_y=_eixo(0.0, _teto(max(tokens / 1000 for _, _, tokens in pares)), separador=True),
        titulo_x="Iterações por pedido",
        titulo_y="Tokens de entrada por rodada (mil)",
        series=series,
        legenda=[*_itens_series(series), *_item_ponto_fraco(plotadas)],
    )


def grafico_curva_de_contexto(dados: DadosEvolucao) -> str:
    """Tokens de entrada no turno (Y) pelo turno do agente (X), uma linha por curva."""
    titulo = "Tokens de entrada por turno do agente"
    curvas = [c for c in dados.curvas_de_contexto if c.pontos]
    if not curvas:
        return _svg_sem_dados(titulo)
    maior_turno = max(turno for c in curvas for turno, _ in c.pontos)
    maior_tokens = max(tokens for c in curvas for _, tokens in c.pontos)
    com_rotulo = len(curvas) <= 2
    com_nome = len(curvas) > 1
    series: list[_Serie] = []
    trechos: list[str] = []
    for indice, curva in enumerate(curvas):
        pontos: list[_Ponto] = []
        for turno, tokens in sorted(curva.pontos):
            sufixo = f" ({curva.nome})" if com_nome else ""
            pontos.append(
                _Ponto(
                    x=float(turno),
                    y=float(tokens),
                    rotulo=_fmt_tokens(tokens) if com_rotulo else None,
                    dica=f"Turno {turno}{sufixo}: {_inteiro_br(tokens)} tokens de entrada",
                )
            )
        series.append(
            _Serie(nome=curva.nome, cor=PALETA[indice % len(PALETA)], pontos=tuple(pontos))
        )
        valores = ", ".join(
            f"turno {turno} {_fmt_tokens(tokens)}" for turno, tokens in curva.pontos
        )
        trechos.append(f"{curva.nome}: {valores}")
    return _svg(
        titulo=titulo,
        descricao="Tokens de entrada no turno, por turno do agente, uma linha por curva. "
        + "; ".join(trechos)
        + ".",
        eixo_x=_eixo(0.0, _teto(float(maior_turno), inteiro=True), inteiro=True),
        eixo_y=_eixo(0.0, _teto(float(maior_tokens)), separador=True),
        titulo_x="Turno do agente",
        titulo_y="Tokens de entrada no turno",
        series=series,
        legenda=_itens_series(series),
    )


def grafico_custo_por_rodada(dados: DadosEvolucao) -> str:
    """Custo por rodada em milésimos de US$ (valor × 1000), uma série por variante."""
    titulo = "Custo por rodada"
    execucoes = _ordenadas(dados.execucoes)
    custos = [e.custo_por_rodada_usd for e in execucoes if e.custo_por_rodada_usd is not None]
    if not custos:
        return _svg_sem_dados(titulo)

    def milesimos(execucao: Execucao) -> float | None:
        custo = execucao.custo_por_rodada_usd
        return None if custo is None else round(custo * 1000, 6)

    series = _series_por_variante(
        execucoes,
        milesimos,
        _fmt_custo,
        lambda rotulo, e, v: _dica(f"{rotulo}: US$ {_br(v, 2)} por mil rodadas", e),
    )
    plotadas = [e for e in execucoes if e.custo_por_rodada_usd is not None]
    return _svg(
        titulo=titulo,
        descricao=_descricao(
            "Custo por rodada, em milésimos de dólar", series, "execução", _fmt_custo
        ),
        eixo_x=_eixo_execucoes(_maior_numero(execucoes)),
        eixo_y=_eixo(0.0, _teto(max(custos) * 1000)),
        titulo_x="Execução",
        titulo_y="Custo por rodada (milésimos de US$)",
        series=series,
        legenda=[*_itens_series(series), *_item_ponto_fraco(plotadas)],
    )


# --- comparação de variantes ------------------------------------------------------------------

# Seis painéis em duas colunas e três linhas. Coordenadas relativas ao canto de cada painel.
_COLUNAS_PAINEIS: Final = (24.0, 372.0)
_PAINEIS_TOPO: Final = 100.0
_PAINEL_PASSO: Final = 170.0
_PLOT_TOPO: Final = 34.0
_PLOT_BASE: Final = 120.0
_PLOT_ESQUERDA: Final = 44.0
_PLOT_DIREITA: Final = 312.0
_BARRA_LARGURA: Final = 44.0
_NOMES_CURTOS: Final = {"repl": "REPL", "json": "JSON", "json-agregacao": "JSON agreg."}


@dataclass(frozen=True)
class _Painel:
    """Um painel da comparação. `eixo` leva o valor bruto para a unidade do eixo Y do painel."""

    titulo: str
    valor: Callable[[Execucao], float | None]
    eixo: Callable[[float], float]
    formatar: Callable[[float], str]
    unidade: str
    teto: float | None = None


def _identidade(valor: float) -> float:
    return valor


def _milesimos(valor: float) -> str:
    return _br(valor * 1000, 3)


def _opcional(valor: float | int | None) -> float | None:
    return None if valor is None else float(valor)


def _paineis_comparacao() -> tuple[_Painel, ...]:
    return (
        _Painel(
            "Taxa de acerto (%)",
            lambda e: e.taxa_pct,
            _identidade,
            _fmt_taxa,
            "de acerto",
            teto=105.0,
        ),
        _Painel(
            "Iterações por pedido",
            lambda e: e.iteracoes_media,
            _identidade,
            _fmt_iteracoes,
            "iterações por pedido",
        ),
        _Painel(
            "Tokens de entrada por rodada (mil)",
            lambda e: _opcional(e.tokens_entrada_por_rodada),
            lambda v: v / 1000,
            _fmt_tokens,
            "tokens de entrada por rodada",
        ),
        _Painel(
            "Pico de contexto médio (tokens)",
            lambda e: e.pico_contexto_medio,
            _identidade,
            _fmt_tokens,
            "tokens de pico de contexto",
        ),
        _Painel(
            "Caracteres de observação por rodada (mil)",
            lambda e: e.caracteres_observacao_medio,
            lambda v: v / 1000,
            _fmt_tokens,
            "caracteres de observação por rodada",
        ),
        _Painel(
            "Custo por acerto (milésimos de US$)",
            lambda e: e.custo_por_acerto_usd,
            lambda v: v * 1000,
            _milesimos,
            "milésimos de US$ por acerto",
        ),
    )


def _chave_mais_recente(execucao: Execucao) -> tuple[int, float, int]:
    """Chave de ordenação para escolher a execução mais recente: `iniciado_em`, depois `numero`."""
    if execucao.iniciado_em is not None:
        try:
            dt = datetime.fromisoformat(execucao.iniciado_em)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return (1, dt.timestamp(), execucao.numero)
        except ValueError:
            pass
    return (0, 0.0, execucao.numero)


def _ultimas_execucoes(execucoes: Iterable[Execucao]) -> dict[str, Execucao]:
    """A execução mais recente (`iniciado_em`, depois `numero`) de cada variante."""
    ultimas: dict[str, Execucao] = {}
    for execucao in sorted(execucoes, key=lambda e: (e.variante, _chave_mais_recente(e))):
        ultimas[execucao.variante] = execucao
    return ultimas


def _barra(x: float, y: float, largura: float, altura: float, cor: str, dica: str) -> str:
    """Barra com cantos de 4 px só no topo, tooltip e foco por teclado."""
    raio = min(4.0, largura / 2, altura)
    base = y + altura
    d = (
        f"M{_c(x)} {_c(base)} V{_c(y + raio)} Q{_c(x)} {_c(y)} {_c(x + raio)} {_c(y)} "
        f"H{_c(x + largura - raio)} Q{_c(x + largura)} {_c(y)} {_c(x + largura)} {_c(y + raio)} "
        f"V{_c(base)} Z"
    )
    return f'<path d="{d}" fill="{cor}" tabindex="0"><title>{_esc(dica)}</title></path>'


def _painel_svg(
    painel: _Painel,
    variantes: Sequence[str],
    ultimas: Mapping[str, Execucao],
    cores: Mapping[str, str],
    x0: float,
    topo_painel: float,
) -> list[str]:
    esquerda = x0 + _PLOT_ESQUERDA
    direita = x0 + _PLOT_DIREITA
    topo = topo_painel + _PLOT_TOPO
    base = topo_painel + _PLOT_BASE
    medidas = {variante: painel.valor(ultimas[variante]) for variante in variantes}
    eixos_valores = [painel.eixo(m) for m in medidas.values() if m is not None]
    maximo = max(eixos_valores, default=0.0)
    hi = painel.teto if painel.teto is not None else _teto(maximo)
    eixo = _eixo(0.0, hi, separador=True)

    elementos: list[str] = [
        f'<text x="{_c(x0)}" y="{_c(topo_painel + 14)}" font-size="13" font-weight="500" '
        f'fill="{COR_TEXTO}">{_esc(painel.titulo)}</text>'
    ]
    for valor, rotulo in zip(eixo.valores, eixo.rotulos, strict=True):
        y = _px(valor, eixo, base, topo)
        if valor != eixo.lo:
            elementos.append(
                f'<line x1="{_c(esquerda)}" y1="{_c(y)}" x2="{_c(direita)}" y2="{_c(y)}" '
                f'stroke="{COR_GRADE}" stroke-width="1"/>'
            )
        elementos.append(
            f'<text x="{_c(esquerda - 6)}" y="{_c(y + 4)}" text-anchor="end" font-size="10" '
            f'fill="{COR_TICK}">{_esc(rotulo)}</text>'
        )
    elementos.append(
        f'<line x1="{_c(esquerda)}" y1="{_c(base)}" x2="{_c(direita)}" y2="{_c(base)}" '
        f'stroke="{COR_BASE}" stroke-width="1"/>'
    )

    slot = (direita - esquerda) / len(variantes)
    largura = min(_BARRA_LARGURA, slot * 0.8)
    for indice, variante in enumerate(variantes):
        centro = esquerda + slot * (indice + 0.5)
        medida = medidas[variante]
        rotulo_variante = _rotulo_variante(variante)
        if medida is None:
            elementos.append(
                f'<text x="{_c(centro)}" y="{_c(base - 8)}" text-anchor="middle" font-size="10" '
                f'fill="{COR_TICK}"><title>{_esc(rotulo_variante)}: sem dado</title>sem dado</text>'
            )
            continue
        y_barra = _px(painel.eixo(medida), eixo, base, topo)
        dica = f"{rotulo_variante}: {painel.formatar(medida)} {painel.unidade}"
        elementos.append(
            _barra(centro - largura / 2, y_barra, largura, base - y_barra, cores[variante], dica)
        )
        elementos.append(
            f'<text x="{_c(centro)}" y="{_c(y_barra - 6)}" text-anchor="middle" font-size="11" '
            f'fill="{COR_TEXTO}">{_esc(painel.formatar(medida))}</text>'
        )
    for indice, variante in enumerate(variantes):
        centro = esquerda + slot * (indice + 0.5)
        nome_curto = _NOMES_CURTOS.get(variante, variante)
        elementos.append(
            f'<text x="{_c(centro)}" y="{_c(base + 16)}" text-anchor="middle" font-size="11" '
            f'fill="{COR_EIXO_TEXTO}">{_esc(nome_curto)}</text>'
        )
    return elementos


def _svg_comparacao_insuficiente() -> str:
    titulo = SECAO_COMPARACAO[0]
    elementos = _cabecalho(titulo, "comparação exige duas ou mais variantes")
    elementos.append(
        f'<text x="24" y="34" font-size="16" font-weight="500" fill="{COR_TEXTO}">'
        f"{_esc(titulo)}</text>"
    )
    elementos.append(
        f'<text x="{LARGURA // 2}" y="{ALTURA // 2}" text-anchor="middle" font-size="14" '
        f'fill="{COR_EIXO_TEXTO}">comparação exige duas ou mais variantes</text>'
    )
    elementos.append("</svg>")
    return "\n".join(elementos) + "\n"


def grafico_comparacao_de_variantes(dados: DadosEvolucao) -> str:
    """Seis painéis com uma barra por variante, na execução mais recente de cada uma.

    Só faz sentido com duas ou mais variantes. Com menos, devolve um SVG com a mensagem de que a
    comparação exige duas ou mais variantes, sem levantar exceção.
    """
    variantes = _variantes(dados.execucoes)
    if len(variantes) < 2:
        return _svg_comparacao_insuficiente()
    ultimas = _ultimas_execucoes(dados.execucoes)
    cores = _cores_variantes(variantes)
    paineis = _paineis_comparacao()
    trechos: list[str] = []
    for painel in paineis:
        partes: list[str] = []
        for variante in variantes:
            medida = painel.valor(ultimas[variante])
            texto = "sem dado" if medida is None else painel.formatar(medida)
            partes.append(f"{_rotulo_variante(variante)} {texto}")
        trechos.append(f"{painel.titulo}: " + ", ".join(partes) + ".")
    titulo = SECAO_COMPARACAO[0]
    descricao = "Comparação entre variantes, na execução mais recente de cada uma. " + " ".join(
        trechos
    )
    legenda_svg, _ = _legenda(
        [_Item(texto=_rotulo_variante(v), cor=cores[v], linha=False) for v in variantes]
    )
    elementos = _cabecalho(titulo, descricao, ALTURA_COMPARACAO)
    elementos.append(
        f'<text x="24" y="34" font-size="16" font-weight="500" fill="{COR_TEXTO}">'
        f"{_esc(titulo)}</text>"
    )
    elementos.extend(legenda_svg)
    for indice, painel in enumerate(paineis):
        x0 = _COLUNAS_PAINEIS[indice % 2]
        topo_painel = _PAINEIS_TOPO + (indice // 2) * _PAINEL_PASSO
        elementos.extend(_painel_svg(painel, variantes, ultimas, cores, x0, topo_painel))
    elementos.append("</svg>")
    return "\n".join(elementos) + "\n"


def _comparacao_se_houver(dados: DadosEvolucao) -> str | None:
    """A comparação, só quando há duas ou mais variantes. Com uma só, nada muda na saída."""
    if len(_variantes(dados.execucoes)) < 2:
        return None
    return grafico_comparacao_de_variantes(dados)


# --- modo escala ------------------------------------------------------------------------------

_PAINEL_ESCALA_PASSO: Final = 180.0
_TITULO_X_ESCALA: Final = "Linhas de pedidos (escala logarítmica)"


@dataclass(frozen=True)
class _MetricaEscala:
    """Definição compartilhada entre os quatro gráficos individuais e os quatro painéis."""

    titulo: str
    titulo_painel: str
    titulo_y: str
    descricao_base: str
    valor: Callable[[Execucao], float | None]
    formatar: Callable[[float], str]
    unidade_dica: str
    teto: float | None = None
    separador: bool = False


def _tokens_escala_mil(execucao: Execucao) -> float | None:
    bruto = (
        execucao.tokens_entrada_por_rodada_executada
        if execucao.tokens_entrada_por_rodada_executada is not None
        else _opcional(execucao.tokens_entrada_por_rodada)
    )
    return None if bruto is None else bruto / 1000


def _para_milesimos(valor: float | None) -> float | None:
    return None if valor is None else round(valor * 1000, 6)


def _custo_escala_milesimos(execucao: Execucao) -> float | None:
    bruto = (
        execucao.custo_por_rodada_executada_usd
        if execucao.custo_por_rodada_executada_usd is not None
        else execucao.custo_por_rodada_usd
    )
    return _para_milesimos(bruto)


def _fmt_mil(valor: float) -> str:
    return _br(valor, 1)


def _metricas_escala() -> tuple[_MetricaEscala, ...]:
    return (
        _MetricaEscala(
            titulo="Tokens de entrada por rodada por escala",
            titulo_painel="Tokens de entrada por rodada (mil)",
            titulo_y="Tokens de entrada por rodada (mil)",
            descricao_base=(
                "Tokens de entrada por rodada (em milhares) por número de linhas de pedidos"
            ),
            valor=_tokens_escala_mil,
            formatar=_fmt_mil,
            unidade_dica="mil tokens de entrada por rodada",
            separador=True,
        ),
        _MetricaEscala(
            titulo="Custo por rodada por escala",
            titulo_painel="Custo por rodada (milésimos de US$)",
            titulo_y="Custo por rodada (milésimos de US$)",
            descricao_base=(
                "Custo por rodada (em milésimos de dólar) por número de linhas de pedidos"
            ),
            valor=_custo_escala_milesimos,
            formatar=_fmt_custo,
            unidade_dica="milésimos de US$ por rodada",
        ),
        _MetricaEscala(
            titulo="Pico de contexto médio por escala",
            titulo_painel="Pico de contexto médio (tokens)",
            titulo_y="Pico de contexto médio (tokens)",
            descricao_base="Pico de contexto médio (em tokens) por número de linhas de pedidos",
            valor=lambda e: e.pico_contexto_medio,
            formatar=_fmt_tokens,
            unidade_dica="tokens de pico de contexto",
            separador=True,
        ),
        _MetricaEscala(
            titulo="Taxa de acerto por escala",
            titulo_painel="Taxa de acerto (%)",
            titulo_y="Rodadas aprovadas (%)",
            descricao_base="Taxa de acerto (em porcentagem) por número de linhas de pedidos",
            valor=lambda e: e.taxa_pct,
            formatar=_fmt_taxa,
            unidade_dica="de acerto",
            teto=105.0,
        ),
    )


def tem_modo_escala(dados: DadosEvolucao) -> bool:
    """Verdadeiro quando alguma variante tem relatórios em duas ou mais escalas diferentes."""
    por_variante: dict[str, set[int]] = {}
    for execucao in dados.execucoes:
        if execucao.linhas_pedidos is not None:
            por_variante.setdefault(execucao.variante, set()).add(execucao.linhas_pedidos)
    return any(len(escalas) >= 2 for escalas in por_variante.values())


def _ultimas_por_escala(
    execucoes: Iterable[Execucao],
) -> tuple[list[str], list[int], dict[tuple[str, int], Execucao]]:
    """Execução mais recente (`iniciado_em`, depois `numero`) por `(variante, linhas_pedidos)`."""
    com_escala = [e for e in execucoes if e.linhas_pedidos is not None]
    ultimas: dict[tuple[str, int], Execucao] = {}
    for execucao in sorted(com_escala, key=_chave_mais_recente):
        assert execucao.linhas_pedidos is not None
        ultimas[(execucao.variante, execucao.linhas_pedidos)] = execucao
    variantes = _variantes(ultimas.values())
    escalas = sorted({escala for _, escala in ultimas})
    return variantes, escalas, ultimas


def _eixo_log_escalas(escalas: Sequence[int]) -> _Eixo:
    """Eixo X em escala logarítmica (base 10) com marcas exatamente nos valores presentes."""
    ordenadas = sorted(set(escalas))
    logs = tuple(math.log10(v) for v in ordenadas)
    span = logs[-1] - logs[0]
    folga = span * 0.08 if span > 0 else 0.5
    return _Eixo(logs[0] - folga, logs[-1] + folga, logs, tuple(_inteiro_br(v) for v in ordenadas))


def _series_escala(
    metrica: _MetricaEscala,
    variantes: Sequence[str],
    escalas: Sequence[int],
    ultimas: Mapping[tuple[str, int], Execucao],
    cores: Mapping[str, str],
    *,
    com_rotulo: bool,
) -> tuple[list[_Serie], list[Execucao], list[float]]:
    series: list[_Serie] = []
    plotadas: list[Execucao] = []
    valores_y: list[float] = []
    for variante in variantes:
        pontos: list[_Ponto | None] = []
        for escala in escalas:
            execucao = ultimas.get((variante, escala))
            if execucao is None:
                continue
            medida = metrica.valor(execucao)
            if medida is None:
                pontos.append(None)
                continue
            plotadas.append(execucao)
            valores_y.append(medida)
            texto = (
                f"{_rotulo_variante(variante)}, {_inteiro_br(escala)} linhas: "
                f"{metrica.formatar(medida)} {metrica.unidade_dica}"
            )
            pontos.append(
                _Ponto(
                    x=math.log10(escala),
                    y=medida,
                    oco=_eh_oco(execucao),
                    rotulo=metrica.formatar(medida) if com_rotulo else None,
                    dica=_dica(texto, execucao),
                )
            )
        series.append(
            _Serie(nome=_rotulo_variante(variante), cor=cores[variante], pontos=tuple(pontos))
        )
    return series, plotadas, valores_y


def _grafico_escala(dados: DadosEvolucao, metrica: _MetricaEscala) -> str:
    if not tem_modo_escala(dados):
        return _svg_sem_dados(metrica.titulo)
    variantes, escalas, ultimas = _ultimas_por_escala(dados.execucoes)
    cores = _cores_variantes(variantes)
    series, plotadas, valores_y = _series_escala(
        metrica, variantes, escalas, ultimas, cores, com_rotulo=len(variantes) <= 2
    )
    if not valores_y:
        return _svg_sem_dados(metrica.titulo)
    eixo_x = _eixo_log_escalas(escalas)
    hi = metrica.teto if metrica.teto is not None else _teto(max(valores_y))
    eixo_y = _eixo(0.0, hi, separador=metrica.separador)
    trechos: list[str] = []
    for variante in variantes:
        partes: list[str] = []
        for escala in escalas:
            execucao = ultimas.get((variante, escala))
            if execucao is None:
                continue
            medida = metrica.valor(execucao)
            if medida is not None:
                partes.append(f"{_inteiro_br(escala)} linhas {metrica.formatar(medida)}")
        trechos.append(
            f"{_rotulo_variante(variante)}: " + (", ".join(partes) if partes else "sem valores")
        )
    descricao = f"{metrica.descricao_base} (escala logarítmica). " + "; ".join(trechos) + "."
    return _svg(
        titulo=metrica.titulo,
        descricao=descricao,
        eixo_x=eixo_x,
        eixo_y=eixo_y,
        titulo_x=_TITULO_X_ESCALA,
        titulo_y=metrica.titulo_y,
        series=series,
        legenda=[*_itens_series(series), *_item_ponto_fraco(plotadas)],
    )


def grafico_escala_tokens(dados: DadosEvolucao) -> str:
    """Tokens de entrada por rodada (milhares) por número de linhas de pedidos (escala log)."""
    return _grafico_escala(dados, _metricas_escala()[0])


def grafico_escala_custo(dados: DadosEvolucao) -> str:
    """Custo por rodada (milésimos de US$) por número de linhas de pedidos (escala log)."""
    return _grafico_escala(dados, _metricas_escala()[1])


def grafico_escala_pico_contexto(dados: DadosEvolucao) -> str:
    """Pico de contexto médio (tokens) por número de linhas de pedidos (escala log)."""
    return _grafico_escala(dados, _metricas_escala()[2])


def grafico_escala_taxa(dados: DadosEvolucao) -> str:
    """Taxa de acerto (%) por número de linhas de pedidos (escala log)."""
    return _grafico_escala(dados, _metricas_escala()[3])


def _painel_escala_svg(
    metrica: _MetricaEscala,
    variantes: Sequence[str],
    escalas: Sequence[int],
    ultimas: Mapping[tuple[str, int], Execucao],
    cores: Mapping[str, str],
    eixo_x: _Eixo,
    x0: float,
    topo_painel: float,
) -> list[str]:
    esquerda = x0 + _PLOT_ESQUERDA
    direita = x0 + _PLOT_DIREITA
    topo = topo_painel + _PLOT_TOPO
    base = topo_painel + _PLOT_BASE
    series, _, valores_y = _series_escala(
        metrica, variantes, escalas, ultimas, cores, com_rotulo=False
    )
    hi = metrica.teto if metrica.teto is not None else _teto(max(valores_y, default=0.0))
    eixo_y = _eixo(0.0, hi, separador=metrica.separador)

    elementos: list[str] = [
        f'<text x="{_c(x0)}" y="{_c(topo_painel + 14)}" font-size="13" font-weight="500" '
        f'fill="{COR_TEXTO}">{_esc(metrica.titulo_painel)}</text>'
    ]
    for valor, rotulo in zip(eixo_y.valores, eixo_y.rotulos, strict=True):
        y = _px(valor, eixo_y, base, topo)
        if valor != eixo_y.lo:
            elementos.append(
                f'<line x1="{_c(esquerda)}" y1="{_c(y)}" x2="{_c(direita)}" y2="{_c(y)}" '
                f'stroke="{COR_GRADE}" stroke-width="1"/>'
            )
        elementos.append(
            f'<text x="{_c(esquerda - 6)}" y="{_c(y + 4)}" text-anchor="end" font-size="10" '
            f'fill="{COR_TICK}">{_esc(rotulo)}</text>'
        )
    elementos.append(
        f'<line x1="{_c(esquerda)}" y1="{_c(base)}" x2="{_c(direita)}" y2="{_c(base)}" '
        f'stroke="{COR_BASE}" stroke-width="1"/>'
    )
    for valor, rotulo in zip(eixo_x.valores, eixo_x.rotulos, strict=True):
        x = _px(valor, eixo_x, esquerda, direita)
        elementos.append(
            f'<line x1="{_c(x)}" y1="{_c(base)}" x2="{_c(x)}" y2="{_c(base + 4)}" '
            f'stroke="{COR_BASE}" stroke-width="1"/>'
        )
        elementos.append(
            f'<text x="{_c(x)}" y="{_c(base + 15)}" text-anchor="middle" font-size="10" '
            f'fill="{COR_TICK}">{_esc(rotulo)}</text>'
        )
    elementos.append(
        f'<text x="{_c((esquerda + direita) / 2)}" y="{_c(base + 29)}" text-anchor="middle" '
        f'font-size="10" fill="{COR_EIXO_TEXTO}">{_esc(_TITULO_X_ESCALA)}</text>'
    )
    if not valores_y:
        elementos.append(
            f'<text x="{_c((esquerda + direita) / 2)}" y="{_c((topo + base) / 2)}" '
            f'text-anchor="middle" font-size="11" fill="{COR_TICK}">sem dado</text>'
        )
        return elementos

    linhas: list[str] = []
    marcadores: list[str] = []
    for serie in series:
        segmentos: list[list[tuple[float, float]]] = [[]]
        for ponto in serie.pontos:
            if ponto is None:
                segmentos.append([])
                continue
            x = _px(ponto.x, eixo_x, esquerda, direita)
            y = _px(ponto.y, eixo_y, base, topo)
            segmentos[-1].append((x, y))
            marcadores.append(_marcador(serie.forma, x, y, serie.cor, ponto.oco, ponto.dica))
        for segmento in segmentos:
            if len(segmento) >= 2:
                trecho = " L".join(f"{_c(x)} {_c(y)}" for x, y in segmento)
                linhas.append(
                    f'<path d="M{trecho}" fill="none" stroke="{serie.cor}" '
                    f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
                )
    return elementos + linhas + marcadores


def grafico_escala_paineis(dados: DadosEvolucao) -> str:
    """Quatro painéis de escala (tokens, custo, pico de contexto e taxa de acerto) em log X."""
    titulo = SECAO_ESCALA_PAINEIS[0]
    if not tem_modo_escala(dados):
        return _svg_sem_dados(titulo)
    variantes, escalas, ultimas = _ultimas_por_escala(dados.execucoes)
    cores = _cores_variantes(variantes)
    eixo_x = _eixo_log_escalas(escalas)
    metricas = _metricas_escala()
    trechos: list[str] = []
    for metrica in metricas:
        partes: list[str] = []
        for variante in variantes:
            vals = [
                f"{_inteiro_br(escala)} {metrica.formatar(medida)}"
                for escala in escalas
                if (execucao := ultimas.get((variante, escala))) is not None
                and (medida := metrica.valor(execucao)) is not None
            ]
            resumo_vals = ", ".join(vals) if vals else "sem dado"
            partes.append(f"{_rotulo_variante(variante)}: {resumo_vals}")
        trechos.append(f"{metrica.titulo_painel}: " + "; ".join(partes) + ".")
    descricao = (
        "Comparação em escala entre variantes, com linhas de pedidos em escala logarítmica. "
        + " ".join(trechos)
    )
    plotadas = [
        execucao
        for execucao in ultimas.values()
        if any(metrica.valor(execucao) is not None for metrica in metricas)
    ]
    legenda_svg, topo_legenda = _legenda(
        [
            *[_Item(texto=_rotulo_variante(v), cor=cores[v]) for v in variantes],
            *_item_ponto_fraco(plotadas),
        ]
    )
    topo_paineis = max(_PAINEIS_TOPO, topo_legenda)
    elementos = _cabecalho(titulo, descricao, ALTURA_ESCALA_PAINEIS)
    elementos.append(
        f'<text x="24" y="34" font-size="16" font-weight="500" fill="{COR_TEXTO}">'
        f"{_esc(titulo)}</text>"
    )
    elementos.extend(legenda_svg)
    for indice, metrica in enumerate(metricas):
        x0 = _COLUNAS_PAINEIS[indice % 2]
        topo_painel = topo_paineis + (indice // 2) * _PAINEL_ESCALA_PASSO
        elementos.extend(
            _painel_escala_svg(metrica, variantes, escalas, ultimas, cores, eixo_x, x0, topo_painel)
        )
    elementos.append("</svg>")
    return "\n".join(elementos) + "\n"


def _svgs_escala(dados: DadosEvolucao) -> list[str]:
    """Os quatro SVG de escala, na ordem de `NOMES_ESCALA`."""
    return [_grafico_escala(dados, metrica) for metrica in _metricas_escala()]


_ARQUIVOS_SEM_EVOLUCAO: Final = frozenset(
    {"dispersao-iteracoes-tokens.svg", "curva-de-contexto.svg"}
)


def _tem_evolucao_por_execucao(dados: DadosEvolucao) -> bool:
    """Verdadeiro quando alguma variante tem duas ou mais execuções."""
    contagem: dict[str, int] = {}
    for execucao in dados.execucoes:
        contagem[execucao.variante] = contagem.get(execucao.variante, 0) + 1
        if contagem[execucao.variante] >= 2:
            return True
    return False


def _geradores_fora_escala(
    dados: DadosEvolucao,
) -> list[tuple[str, str, str, Callable[[DadosEvolucao], str]]]:
    """Pares (arquivo, título, leitura, função) fora do modo escala.

    Os gráficos de linha por execução (`taxa-total`, `taxa-por-categoria`, `iteracoes-por-pedido`,
    `custo-por-rodada`) só entram quando alguma variante tem duas ou mais execuções. Quando cada
    variante tem no máximo uma execução, ficam apenas a dispersão e a curva de contexto (além do
    painel de comparação quando há duas ou mais variantes).
    """
    funcoes: tuple[Callable[[DadosEvolucao], str], ...] = (
        grafico_taxa_total,
        grafico_taxa_por_categoria,
        grafico_iteracoes,
        grafico_dispersao_iteracoes_tokens,
        grafico_curva_de_contexto,
        grafico_custo_por_rodada,
    )
    incluir_linhas = _tem_evolucao_por_execucao(dados)
    return [
        (arquivo, titulo, leitura, fn)
        for (arquivo, titulo, leitura), fn in zip(SECOES, funcoes, strict=True)
        if incluir_linhas or arquivo in _ARQUIVOS_SEM_EVOLUCAO
    ]


def _com_identificador(svg: str, sufixo: str) -> str:
    """Troca os ids `t` e `d` do SVG por ids com sufixo, para não repetir na mesma página."""
    return (
        svg.replace('aria-labelledby="t d"', f'aria-labelledby="t{sufixo} d{sufixo}"')
        .replace('<title id="t">', f'<title id="t{sufixo}">')
        .replace('<desc id="d">', f'<desc id="d{sufixo}">')
    )


_CSS_PAINEL: Final = """\
h3 { font-size: 15px; font-weight: 600; margin: 24px 0 4px; }
.painel-caixa { background: #ffffff; border: 1px solid #e1e0d9; border-radius: 8px; padding: 14px;
  box-sizing: border-box; max-width: 100%; }
.painel-controles { display: flex; flex-wrap: wrap; gap: 10px 16px; align-items: center;
  margin-bottom: 12px; }
.painel-grupo { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.painel-btn { border: 1px solid #c3c2b7; background: #f4f4f1; color: #0b0b0b; border-radius: 6px;
  padding: 5px 10px; font-size: 12px; cursor: pointer; }
.painel-btn[aria-pressed="true"],
.painel-btn[aria-selected="true"] { background: #0b0b0b; color: #ffffff; border-color: #0b0b0b; }
.painel-var { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
  padding: 4px 8px; border: 1px solid #e1e0d9; border-radius: 6px; background: #fafaf8;
  cursor: pointer; transition: opacity 0.15s ease; }
.painel-var[data-ativa="0"] { opacity: 0.48; }
.painel-var input { margin: 0; cursor: pointer; }
.painel-cor { width: 10px; height: 10px; border-radius: 999px; display: inline-block; flex: none; }
.painel-tabela-wrap { width: 100%; max-width: 100%; overflow-x: auto;
  -webkit-overflow-scrolling: touch; margin-top: 14px; }
.painel-tabela { width: 100%; border-collapse: collapse; font-size: 12px; }
.painel-tabela th, .painel-tabela td { text-align: left; padding: 6px 8px;
  border-bottom: 1px solid #e1e0d9; white-space: nowrap; }
.painel-tabela th { color: #52514e; font-weight: 600; }
.painel-tabela th[aria-sort] { cursor: pointer; user-select: none; }
.painel-tabela th[aria-sort]:hover { color: #0b0b0b; background: #fafaf8; }
.painel-tabela th.painel-num, .painel-tabela td.painel-num { text-align: right;
  font-variant-numeric: tabular-nums; }
.painel-ordem { margin-left: 4px; font-size: 10px; color: #898781; }
.painel-tabela tbody tr { cursor: pointer; }
.painel-tabela tbody tr:hover, .painel-tabela tbody tr[data-ativo="1"] { background: #f4f4f1; }
.painel-detalhe { margin-top: 14px; padding: 12px; border: 1px solid #e1e0d9; border-radius: 6px;
  background: #fafaf8; font-size: 12px; line-height: 1.5; }
.painel-detalhe .painel-tabela { margin-top: 10px; }
.painel-detalhe h4 { margin: 0 0 6px; font-size: 13px; font-weight: 600; }
.painel-vazio { color: #52514e; font-size: 13px; padding: 16px 0; text-align: center; }
.painel-resumo { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px; margin-bottom: 12px; }
.painel-cartao { border: 1px solid #e1e0d9; border-radius: 6px; padding: 10px 12px;
  background: #fafaf8; }
.painel-cartao-destaque { border-color: #c3c2b7; background: #f4f4f1; }
.painel-cartao-titulo { display: flex; align-items: center; gap: 6px; font-size: 12px;
  font-weight: 600; margin-bottom: 6px; color: #0b0b0b; }
.painel-cartao-valor { font-size: 18px; font-weight: 600; color: #0b0b0b; line-height: 1.2; }
.painel-cartao-meta { font-size: 11.5px; color: #52514e; margin-top: 3px; line-height: 1.4; }
.painel-grafico { position: relative; }
.painel-grafico svg:focus { outline: 2px solid #0b0b0b; outline-offset: 2px; border-radius: 6px; }
.painel-balao { position: absolute; top: 10px; z-index: 5; min-width: 220px; max-width: 310px;
  padding: 8px 10px; border-radius: 6px; background: rgba(11, 11, 11, 0.94); color: #ffffff;
  font-size: 11.5px; line-height: 1.45; pointer-events: none;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18); }
.painel-balao[hidden] { display: none; }
.painel-balao-titulo { font-weight: 600; margin-bottom: 5px; padding-bottom: 4px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.2); }
.painel-balao-item { margin-top: 5px; }
.painel-balao-var { display: flex; align-items: center; gap: 6px; font-weight: 600; }
.painel-balao-meta { color: #e1e0d9; padding-left: 16px; }
.painel-matriz { margin-top: 16px; }
.painel-matriz h4 { margin: 0 0 6px; font-size: 13px; font-weight: 600; }
.painel-matriz-legenda { display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 11.5px;
  color: #52514e; margin-bottom: 8px; align-items: center; }
.painel-matriz-item { display: inline-flex; align-items: center; gap: 5px; }
.painel-matriz-amostra { display: inline-block; width: 12px; height: 12px; border-radius: 3px;
  border: 1px solid transparent; }
.painel-matriz-tabela th, .painel-matriz-tabela td { text-align: center;
  font-variant-numeric: tabular-nums; }
.painel-matriz-tabela th:first-child, .painel-matriz-tabela td:first-child { text-align: left; }
.painel-cel-aprovada { background: #e6f4ea; color: #137333; border-color: #a8dab5;
  font-weight: 600; }
.painel-cel-mista { background: #fef7e0; color: #b06000; border-color: #fdd663; font-weight: 600; }
.painel-cel-reprovada { background: #fce8e6; color: #c5221f; border-color: #f6aea9;
  font-weight: 600; }
.painel-cel-infra { background: #f1f3f4; color: #52514e; border-color: #d2d5d9; font-weight: 600; }
.painel-cel-vazia { color: #898781; }
.painel-ressalvas { margin-top: 14px; padding: 12px 14px; border: 1px solid #e1e0d9;
  border-radius: 8px; background: #fafaf8; font-size: 12.5px; line-height: 1.5; }
.painel-ressalvas h3 { margin: 0 0 6px; font-size: 14px; font-weight: 600; color: #0b0b0b; }
.painel-ressalvas p { margin: 0 0 8px; color: #52514e; }
.painel-ressalvas p:last-child { margin-bottom: 0; }
.painel-ressalvas ul { margin: 6px 0 0; padding-left: 18px; color: #0b0b0b; }
.painel-ressalvas li { margin: 2px 0; }
"""

_SCRIPT_PAINEL: Final = """\
(function () {
  'use strict';
  var elDados = document.getElementById('dados-painel');
  var raiz = document.getElementById('painel-raiz');
  if (!elDados || !raiz) return;
  var dados = JSON.parse(elDados.textContent || '{}');
  var metricas = dados.metricas || [];
  var variantes = dados.variantes || [];
  var escalas = dados.escalas || [];
  var execucoes = dados.execucoes || [];
  if (!metricas.length || !execucoes.length) return;

  var NS = 'http://www.w3.org/2000/svg';
  var metricaAtiva = metricas[0].id;
  var escalaLog = true;
  var yZero = true;
  var ativas = {};
  variantes.forEach(function (v) { ativas[v.id] = true; });
  var selecionada = execucoes[0];
  var escalaGuia = null;
  var sortCol = null;
  var sortDir = 'asc';

  function el(tag, cls, texto) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (texto !== undefined && texto !== null) n.textContent = String(texto);
    return n;
  }

  function svgEl(tag, attrs) {
    var n = document.createElementNS(NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) { n.setAttribute(k, String(attrs[k])); });
    }
    return n;
  }

  function fmt(valor, casas) {
    if (valor === null || valor === undefined) return 'sem dado';
    return Number(valor).toLocaleString('pt-BR', {
      minimumFractionDigits: casas,
      maximumFractionDigits: casas
    });
  }

  function fmtInt(valor) {
    return Number(valor).toLocaleString('pt-BR');
  }

  function fmtRazao(valor) {
    var arred = Math.round(valor * 10) / 10;
    if (Math.abs(arred - Math.round(arred)) < 1e-9) {
      return fmtInt(Math.round(arred)) + 'x';
    }
    return fmt(arred, 1) + 'x';
  }

  function metricaAtual() {
    for (var i = 0; i < metricas.length; i++) {
      if (metricas[i].id === metricaAtiva) return metricas[i];
    }
    return metricas[0];
  }

  function corVariante(id) {
    for (var i = 0; i < variantes.length; i++) {
      if (variantes[i].id === id) return variantes[i].cor;
    }
    return '#2a78d6';
  }

  var caixa = el('div', 'painel-caixa');
  var controles = el('div', 'painel-controles');
  var grupoMetricas = el('div', 'painel-grupo');
  grupoMetricas.setAttribute('role', 'tablist');
  grupoMetricas.setAttribute('aria-label', 'M\u00e9tricas');
  var grupoEscalaX = el('div', 'painel-grupo');
  var grupoVars = el('div', 'painel-grupo');
  var areaResumo = el('div', 'painel-resumo');
  var areaSvg = el('div', 'painel-grafico');
  var areaTabela = el('div', 'painel-tabela-wrap');
  var areaDetalhe = el('div', 'painel-detalhe');
  var areaMatriz = el('div', 'painel-matriz');

  var btnsMetrica = [];
  function selecionarMetrica(idx, focar) {
    var m = metricas[idx];
    if (!m) return;
    metricaAtiva = m.id;
    btnsMetrica.forEach(function (item, i) {
      var ativo = i === idx;
      item.btn.setAttribute('aria-selected', ativo ? 'true' : 'false');
      item.btn.setAttribute('tabindex', ativo ? '0' : '-1');
      if (ativo && focar) item.btn.focus();
    });
    render();
  }

  metricas.forEach(function (m, idx) {
    var b = el('button', 'painel-btn', m.titulo);
    b.type = 'button';
    b.setAttribute('role', 'tab');
    var ativoInicial = m.id === metricaAtiva;
    b.setAttribute('aria-selected', ativoInicial ? 'true' : 'false');
    b.setAttribute('tabindex', ativoInicial ? '0' : '-1');
    b.addEventListener('click', function () {
      selecionarMetrica(idx, false);
    });
    b.addEventListener('keydown', function (ev) {
      var proximo = null;
      if (ev.key === 'ArrowRight') {
        proximo = (idx + 1) % metricas.length;
      } else if (ev.key === 'ArrowLeft') {
        proximo = (idx - 1 + metricas.length) % metricas.length;
      } else if (ev.key === 'Home') {
        proximo = 0;
      } else if (ev.key === 'End') {
        proximo = metricas.length - 1;
      }
      if (proximo !== null) {
        ev.preventDefault();
        selecionarMetrica(proximo, true);
      }
    });
    btnsMetrica.push({ id: m.id, btn: b });
    grupoMetricas.appendChild(b);
  });

  var btnLog = el('button', 'painel-btn', 'Eixo X logar\u00edtmico');
  btnLog.type = 'button';
  btnLog.setAttribute('aria-pressed', 'true');
  btnLog.addEventListener('click', function () {
    escalaLog = !escalaLog;
    btnLog.setAttribute('aria-pressed', escalaLog ? 'true' : 'false');
    btnLog.textContent = escalaLog ? 'Eixo X logar\u00edtmico' : 'Eixo X linear';
    render();
  });
  grupoEscalaX.appendChild(btnLog);

  var btnY = el('button', 'painel-btn', 'Eixo Y: come\u00e7ar em zero');
  btnY.type = 'button';
  btnY.setAttribute('aria-pressed', 'true');
  btnY.addEventListener('click', function () {
    yZero = !yZero;
    btnY.setAttribute('aria-pressed', yZero ? 'true' : 'false');
    btnY.textContent = yZero ? 'Eixo Y: come\u00e7ar em zero' : 'Eixo Y: escala pr\u00f3pria';
    render();
  });
  grupoEscalaX.appendChild(btnY);

  variantes.forEach(function (v) {
    var lbl = el('label', 'painel-var');
    lbl.setAttribute('data-ativa', '1');
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.addEventListener('change', function () {
      ativas[v.id] = cb.checked;
      lbl.setAttribute('data-ativa', cb.checked ? '1' : '0');
      render();
    });
    var dot = el('span', 'painel-cor');
    dot.style.background = v.cor;
    lbl.appendChild(cb);
    lbl.appendChild(dot);
    lbl.appendChild(document.createTextNode(v.rotulo));
    grupoVars.appendChild(lbl);
  });

  controles.appendChild(grupoMetricas);
  controles.appendChild(grupoEscalaX);
  controles.appendChild(grupoVars);
  caixa.appendChild(controles);
  caixa.appendChild(areaResumo);
  caixa.appendChild(areaSvg);
  caixa.appendChild(areaTabela);
  caixa.appendChild(areaDetalhe);
  caixa.appendChild(areaMatriz);
  raiz.appendChild(caixa);

  function renderResumo(filtradas) {
    areaResumo.textContent = '';
    if (!filtradas.length) return;
    var maiorEscala = filtradas[0].escala;
    for (var i = 1; i < filtradas.length; i++) {
      if (filtradas[i].escala > maiorEscala) {
        maiorEscala = filtradas[i].escala;
      }
    }
    var naMaior = filtradas.filter(function (e) {
      return e.escala === maiorEscala;
    });
    var custosValidos = [];
    variantes.forEach(function (v) {
      if (!ativas[v.id]) return;
      var exec = null;
      for (var j = 0; j < naMaior.length; j++) {
        if (naMaior[j].variante === v.id) {
          exec = naMaior[j];
          break;
        }
      }
      var cartao = el('div', 'painel-cartao');
      var tit = el('div', 'painel-cartao-titulo');
      var dot = el('span', 'painel-cor');
      dot.style.background = v.cor;
      tit.appendChild(dot);
      tit.appendChild(document.createTextNode(v.rotulo));
      cartao.appendChild(tit);

      var custo = exec && exec.valores ? exec.valores.custo : null;
      var taxa = exec && exec.valores ? exec.valores.taxa : null;
      var custoTxt = (custo === null || custo === undefined)
        ? 'sem dado'
        : (fmt(custo, 2) + ' mil\u00e9simos de US$');
      var taxaTxt = (taxa === null || taxa === undefined)
        ? 'sem dado'
        : (fmt(taxa, 1) + '%');

      cartao.appendChild(el('div', 'painel-cartao-valor', taxaTxt));
      cartao.appendChild(
        el('div', 'painel-cartao-meta', 'Taxa de acerto (' + fmtInt(maiorEscala) + ' linhas)')
      );
      cartao.appendChild(el('div', 'painel-cartao-meta', 'Custo/rodada: ' + custoTxt));
      areaResumo.appendChild(cartao);

      if (custo !== null && custo !== undefined && custo > 0) {
        custosValidos.push({ rotulo: v.rotulo, custo: custo });
      }
    });

    if (custosValidos.length >= 2) {
      var maisCara = custosValidos[0];
      var maisBarata = custosValidos[0];
      for (var k = 1; k < custosValidos.length; k++) {
        if (custosValidos[k].custo > maisCara.custo) maisCara = custosValidos[k];
        if (custosValidos[k].custo < maisBarata.custo) maisBarata = custosValidos[k];
      }
      var razao = maisCara.custo / maisBarata.custo;
      var cartaoRazao = el('div', 'painel-cartao painel-cartao-destaque');
      var tituloRazao = 'Raz\u00e3o de custo (' + fmtInt(maiorEscala) + ' linhas)';
      cartaoRazao.appendChild(el('div', 'painel-cartao-titulo', tituloRazao));
      cartaoRazao.appendChild(el('div', 'painel-cartao-valor', fmtRazao(razao)));
      cartaoRazao.appendChild(
        el('div', 'painel-cartao-meta', maisCara.rotulo + ' vs ' + maisBarata.rotulo)
      );
      areaResumo.appendChild(cartaoRazao);
    }
  }

  function renderSvg(met, filtradas) {
    areaSvg.textContent = '';
    var pontosValidos = filtradas.filter(function (e) {
      var val = e.valores ? e.valores[met.id] : null;
      return val !== null && val !== undefined;
    });
    if (!pontosValidos.length) {
      areaSvg.appendChild(el('p', 'painel-vazio', 'Nenhum dado para os filtros selecionados.'));
      return;
    }

    var W = 720, H = 300, L = 68, R = 696, T = 24, B = 254;
    var svg = svgEl('svg', {
      viewBox: '0 0 ' + W + ' ' + H,
      role: 'img',
      tabindex: 0,
      'aria-label': met.titulo + ' (use as setas esquerda e direita para percorrer as escalas)'
    });
    svg.appendChild(svgEl('rect', {
      x: 0.5, y: 0.5, width: W - 1, height: H - 1, rx: 6,
      fill: '#ffffff', stroke: '#e1e0d9'
    }));

    var xs = escalas.map(function (s) { return escalaLog ? Math.log10(s) : s; });
    var xMin = Math.min.apply(null, xs);
    var xMax = Math.max.apply(null, xs);
    var xSpan = xMax - xMin;
    var xLo = xMin - (xSpan > 0 ? xSpan * 0.08 : 0.5);
    var xHi = xMax + (xSpan > 0 ? xSpan * 0.08 : 0.5);

    var ys = pontosValidos.map(function (e) { return e.valores[met.id]; });
    var yMinRaw = Math.min.apply(null, ys);
    var yMaxRaw = Math.max.apply(null, ys);
    var yLo = 0;
    var yHi = met.teto !== null && met.teto !== undefined
      ? met.teto
      : (yMaxRaw > 0 ? yMaxRaw * 1.1 : 1);
    if (!yZero) {
      var ySpan = yMaxRaw - yMinRaw;
      if (ySpan > 0) {
        var folgaY = ySpan * 0.1;
        yLo = yMinRaw >= 0 ? Math.max(0, yMinRaw - folgaY) : yMinRaw - folgaY;
        yHi = yMaxRaw + folgaY;
      } else if (yMinRaw === 0) {
        yLo = 0;
        yHi = 1;
      } else {
        var margemY = Math.abs(yMinRaw) * 0.1 || 1;
        yLo = yMinRaw >= 0 ? Math.max(0, yMinRaw - margemY) : yMinRaw - margemY;
        yHi = yMaxRaw + margemY;
      }
      if (met.teto !== null && met.teto !== undefined && yMaxRaw <= met.teto) {
        yHi = Math.min(met.teto, yHi);
      }
    }

    function pxX(escala) {
      var xVal = escalaLog ? Math.log10(escala) : escala;
      return L + ((xVal - xLo) / (xHi - xLo)) * (R - L);
    }
    function pxY(val) {
      return B - ((val - yLo) / (yHi - yLo)) * (B - T);
    }

    for (var g = 0; g <= 4; g++) {
      var vTick = yLo + ((yHi - yLo) / 4) * g;
      var gy = pxY(vTick);
      svg.appendChild(svgEl('line', {
        x1: L, y1: gy.toFixed(2), x2: R, y2: gy.toFixed(2),
        stroke: g === 0 ? '#c3c2b7' : '#e1e0d9', 'stroke-width': 1
      }));
      var yt = svgEl('text', {
        x: L - 8, y: (gy + 4).toFixed(2), 'text-anchor': 'end',
        'font-size': 11, fill: '#52514e'
      });
      yt.textContent = fmt(vTick, met.casas);
      svg.appendChild(yt);
    }

    escalas.forEach(function (esc) {
      var gx = pxX(esc);
      svg.appendChild(svgEl('line', {
        x1: gx.toFixed(2), y1: B, x2: gx.toFixed(2), y2: B + 4,
        stroke: '#898781', 'stroke-width': 1
      }));
      var xt = svgEl('text', {
        x: gx.toFixed(2), y: B + 18, 'text-anchor': 'middle',
        'font-size': 11, fill: '#52514e'
      });
      xt.textContent = fmtInt(esc);
      svg.appendChild(xt);
    });

    variantes.forEach(function (v) {
      if (!ativas[v.id]) return;
      var serie = filtradas.filter(function (e) {
        return e.variante === v.id && e.valores &&
          e.valores[met.id] !== null && e.valores[met.id] !== undefined;
      });
      if (!serie.length) return;
      if (serie.length >= 2) {
        var pts = serie.map(function (e) {
          return pxX(e.escala).toFixed(2) + ',' + pxY(e.valores[met.id]).toFixed(2);
        }).join(' ');
        svg.appendChild(svgEl('polyline', {
          fill: 'none', stroke: v.cor, 'stroke-width': 2.2, points: pts
        }));
      }
      serie.forEach(function (e) {
        var cx = pxX(e.escala);
        var cy = pxY(e.valores[met.id]);
        var oco = e.incompleta || e.ponto_fraco;
        var ativo = selecionada &&
          selecionada.variante === e.variante && selecionada.escala === e.escala;
        var circ = svgEl('circle', {
          cx: cx.toFixed(2), cy: cy.toFixed(2), r: ativo ? 6.5 : 4.5,
          fill: oco ? '#ffffff' : v.cor, stroke: v.cor, 'stroke-width': ativo ? 2.5 : 2,
          tabindex: 0, style: 'cursor:pointer', 'data-escala': e.escala
        });
        var dica = v.rotulo + ', ' + fmtInt(e.escala) + ' linhas: ' +
          fmt(e.valores[met.id], met.casas) + ' ' + met.unidade;
        if (e.incompleta && e.motivo_incompleta) dica += ' (' + e.motivo_incompleta + ')';
        circ.setAttribute('aria-label', dica);
        circ.addEventListener('click', function () { selecionada = e; render(); });
        circ.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            selecionada = e;
            render();
          }
        });
        svg.appendChild(circ);
      });
    });

    var escalasVisiveis = escalas.filter(function (esc) {
      return pontosValidos.some(function (e) { return e.escala === esc; });
    });

    var linhaGuia = svgEl('line', {
      x1: L, y1: T, x2: L, y2: B,
      stroke: '#52514e', 'stroke-width': 1.2, 'stroke-dasharray': '4 3',
      'pointer-events': 'none', visibility: 'hidden', 'data-guia': '1'
    });
    svg.appendChild(linhaGuia);

    var balao = el('div', 'painel-balao');
    balao.setAttribute('role', 'tooltip');
    balao.hidden = true;

    function esconderGuia() {
      escalaGuia = null;
      linhaGuia.setAttribute('visibility', 'hidden');
      balao.hidden = true;
      balao.textContent = '';
    }

    function mostrarGuia(esc) {
      if (esc === null || esc === undefined || escalasVisiveis.indexOf(esc) === -1) {
        esconderGuia();
        return;
      }
      escalaGuia = esc;
      var gx = pxX(esc);
      linhaGuia.setAttribute('x1', gx.toFixed(2));
      linhaGuia.setAttribute('x2', gx.toFixed(2));
      linhaGuia.setAttribute('visibility', 'visible');

      balao.textContent = '';
      balao.appendChild(el('div', 'painel-balao-titulo', fmtInt(esc) + ' linhas'));
      variantes.forEach(function (v) {
        if (!ativas[v.id]) return;
        var exec = null;
        for (var i = 0; i < filtradas.length; i++) {
          if (filtradas[i].variante === v.id && filtradas[i].escala === esc) {
            exec = filtradas[i];
            break;
          }
        }
        if (!exec) return;
        var item = el('div', 'painel-balao-item');
        var cab = el('div', 'painel-balao-var');
        var dot = el('span', 'painel-cor');
        dot.style.background = v.cor;
        cab.appendChild(dot);
        var val = exec.valores ? exec.valores[met.id] : null;
        var valTexto = (val === null || val === undefined)
          ? 'sem dado'
          : (fmt(val, met.casas) + ' ' + met.unidade);
        cab.appendChild(document.createTextNode(v.rotulo + ': ' + valTexto));
        item.appendChild(cab);

        var meta = el(
          'div',
          'painel-balao-meta',
          'Rodadas: ' + exec.rodadas_validas + '/' + exec.rodadas +
            ' \u2014 Aprovadas: ' + exec.aprovadas +
            ' | Reprovadas: ' + exec.reprovadas +
            ' | Infra: ' + exec.erro_infra
        );
        item.appendChild(meta);
        balao.appendChild(item);
      });

      var pct = (gx / W) * 100;
      if (gx < W * 0.58) {
        balao.style.left = (pct + 1.5).toFixed(2) + '%';
        balao.style.right = 'auto';
      } else {
        balao.style.left = 'auto';
        balao.style.right = (100 - pct + 1.5).toFixed(2) + '%';
      }
      balao.hidden = false;
    }

    function escalaMaisProxima(clientX) {
      if (!escalasVisiveis.length) return null;
      var svgX = L;
      if (typeof clientX === 'number' && svg.getBoundingClientRect) {
        var rect = svg.getBoundingClientRect();
        if (rect && rect.width > 0) {
          svgX = ((clientX - rect.left) / rect.width) * W;
        }
      }
      var melhor = escalasVisiveis[0];
      var melhorDist = Math.abs(pxX(melhor) - svgX);
      for (var i = 1; i < escalasVisiveis.length; i++) {
        var d = Math.abs(pxX(escalasVisiveis[i]) - svgX);
        if (d < melhorDist) {
          melhorDist = d;
          melhor = escalasVisiveis[i];
        }
      }
      return melhor;
    }

    svg.addEventListener('pointermove', function (ev) {
      if (ev.pointerType === 'touch') return;
      mostrarGuia(escalaMaisProxima(ev.clientX));
    });
    svg.addEventListener('pointerleave', function () {
      esconderGuia();
    });
    svg.addEventListener('pointerdown', function (ev) {
      if (ev.pointerType === 'touch') {
        mostrarGuia(escalaMaisProxima(ev.clientX));
      }
    });
    svg.addEventListener('touchstart', function (ev) {
      if (ev.touches && ev.touches.length) {
        mostrarGuia(escalaMaisProxima(ev.touches[0].clientX));
      }
    }, { passive: true });
    svg.addEventListener('focusin', function (ev) {
      var tgt = ev.target;
      var escAttr = tgt && tgt.getAttribute ? tgt.getAttribute('data-escala') : null;
      if (escAttr !== null) {
        mostrarGuia(Number(escAttr));
      } else if (escalaGuia !== null && escalasVisiveis.indexOf(escalaGuia) !== -1) {
        mostrarGuia(escalaGuia);
      } else if (selecionada && escalasVisiveis.indexOf(selecionada.escala) !== -1) {
        mostrarGuia(selecionada.escala);
      } else if (escalasVisiveis.length) {
        mostrarGuia(escalasVisiveis[0]);
      }
    });
    svg.addEventListener('focusout', function (ev) {
      if (!ev.relatedTarget || (svg.contains && !svg.contains(ev.relatedTarget))) {
        esconderGuia();
      }
    });
    svg.addEventListener('keydown', function (ev) {
      if (!escalasVisiveis.length) return;
      var atual = escalaGuia;
      if (atual === null || escalasVisiveis.indexOf(atual) === -1) {
        atual = (selecionada && escalasVisiveis.indexOf(selecionada.escala) !== -1)
          ? selecionada.escala
          : escalasVisiveis[0];
      }
      var idx = escalasVisiveis.indexOf(atual);
      var novoIdx = null;
      if (ev.key === 'ArrowRight') {
        novoIdx = Math.min(escalasVisiveis.length - 1, idx + 1);
      } else if (ev.key === 'ArrowLeft') {
        novoIdx = Math.max(0, idx - 1);
      } else if (ev.key === 'Home') {
        novoIdx = 0;
      } else if (ev.key === 'End') {
        novoIdx = escalasVisiveis.length - 1;
      } else if (ev.key === 'Escape') {
        esconderGuia();
        return;
      }
      if (novoIdx !== null) {
        ev.preventDefault();
        mostrarGuia(escalasVisiveis[novoIdx]);
      }
    });

    areaSvg.appendChild(svg);
    areaSvg.appendChild(balao);
    if (escalaGuia !== null && escalasVisiveis.indexOf(escalaGuia) !== -1) {
      mostrarGuia(escalaGuia);
    }
  }

  var COLUNAS_TABELA = [
    {
      id: 'variante',
      titulo: 'Variante',
      tipo: 'texto',
      valor: function (e) { return e.rotulo; }
    },
    {
      id: 'escala',
      titulo: 'Escala',
      tipo: 'num',
      valor: function (e) { return e.escala / 60; },
      texto: function (e) { return fmtRazao(e.escala / 60); }
    },
    {
      id: 'linhas',
      titulo: 'Linhas',
      tipo: 'num',
      valor: function (e) { return e.escala; },
      texto: function (e) { return fmtInt(e.escala); }
    },
    {
      id: 'rodadas',
      titulo: 'Rodadas',
      tipo: 'num',
      valor: function (e) { return e.rodadas; },
      texto: function (e) { return e.rodadas_validas + '/' + e.rodadas; }
    },
    {
      id: 'aprovadas',
      titulo: 'Aprovadas',
      tipo: 'num',
      valor: function (e) { return e.aprovadas; },
      texto: function (e) { return fmtInt(e.aprovadas); }
    },
    {
      id: 'reprovadas',
      titulo: 'Reprovadas',
      tipo: 'num',
      valor: function (e) { return e.reprovadas; },
      texto: function (e) { return fmtInt(e.reprovadas); }
    },
    {
      id: 'infra',
      titulo: 'Infra',
      tipo: 'num',
      valor: function (e) { return e.erro_infra; },
      texto: function (e) { return fmtInt(e.erro_infra); }
    },
    {
      id: 'acerto',
      titulo: 'Acerto (v\u00e1lidas)',
      tipo: 'num',
      valor: function (e) { return e.valores ? e.valores.taxa : null; },
      texto: function (e) {
        var v = e.valores ? e.valores.taxa : null;
        return (v === null || v === undefined) ? 'sem dado' : (fmt(v, 1) + '%');
      }
    },
    {
      id: 'iteracoes',
      titulo: 'Itera\u00e7\u00f5es',
      tipo: 'num',
      valor: function (e) { return e.valores ? e.valores.iteracoes : null; },
      texto: function (e) {
        var v = e.valores ? e.valores.iteracoes : null;
        return (v === null || v === undefined) ? 'sem dado' : fmt(v, 2);
      }
    },
    {
      id: 'tokens_entrada',
      titulo: 'Tokens entrada',
      tipo: 'num',
      valor: function (e) { return e.tokens_entrada; },
      texto: function (e) {
        var v = e.tokens_entrada;
        return (v === null || v === undefined) ? 'sem dado' : fmtInt(Math.round(v));
      }
    },
    {
      id: 'tokens_saida',
      titulo: 'Tokens sa\u00edda',
      tipo: 'num',
      valor: function (e) { return e.tokens_saida; },
      texto: function (e) {
        var v = e.tokens_saida;
        return (v === null || v === undefined) ? 'sem dado' : fmtInt(Math.round(v));
      }
    },
    {
      id: 'custo_rodada',
      titulo: 'Custo por rodada',
      tipo: 'num',
      valor: function (e) { return e.valores ? e.valores.custo : null; },
      texto: function (e) {
        var v = e.valores ? e.valores.custo : null;
        return (v === null || v === undefined) ? 'sem dado' : (fmt(v, 2) + ' mUS$');
      }
    },
    {
      id: 'custo_acerto',
      titulo: 'Custo por acerto',
      tipo: 'num',
      valor: function (e) { return e.valores ? e.valores.custo_acerto : null; },
      texto: function (e) {
        var v = e.valores ? e.valores.custo_acerto : null;
        return (v === null || v === undefined) ? 'sem dado' : (fmt(v, 2) + ' mUS$');
      }
    },
    {
      id: 'pico',
      titulo: 'Pico de contexto',
      tipo: 'num',
      valor: function (e) { return e.valores ? e.valores.pico : null; },
      texto: function (e) {
        var v = e.valores ? e.valores.pico : null;
        return (v === null || v === undefined) ? 'sem dado' : fmt(v, 0);
      }
    },
    {
      id: 'estado',
      titulo: 'Estado',
      tipo: 'texto',
      valor: function (e) { return e.incompleta ? 'Incompleta' : 'Completa'; },
      texto: function (e) { return e.incompleta ? 'Incompleta' : 'Completa'; }
    }
  ];

  function alternarOrdenacao(colId) {
    if (sortCol === colId) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = colId;
      sortDir = 'asc';
    }
    render();
  }

  function ordenarExecucoes(lista) {
    if (!sortCol) return lista.slice();
    var col = null;
    for (var i = 0; i < COLUNAS_TABELA.length; i++) {
      if (COLUNAS_TABELA[i].id === sortCol) {
        col = COLUNAS_TABELA[i];
        break;
      }
    }
    if (!col) return lista.slice();
    var indexadas = lista.map(function (item, idx) {
      return { item: item, idx: idx, val: col.valor(item) };
    });
    indexadas.sort(function (a, b) {
      var va = a.val;
      var vb = b.val;
      var aNulo = va === null || va === undefined || (typeof va === 'number' && isNaN(va));
      var bNulo = vb === null || vb === undefined || (typeof vb === 'number' && isNaN(vb));
      if (aNulo && bNulo) return a.idx - b.idx;
      if (aNulo) return 1;
      if (bNulo) return -1;
      var cmp = 0;
      if (col.tipo === 'texto') {
        cmp = String(va).localeCompare(String(vb), 'pt-BR');
      } else {
        cmp = Number(va) - Number(vb);
      }
      if (cmp !== 0) return sortDir === 'asc' ? cmp : -cmp;
      return a.idx - b.idx;
    });
    return indexadas.map(function (x) { return x.item; });
  }

  function renderTabela(filtradas) {
    areaTabela.textContent = '';
    if (!filtradas.length) {
      areaTabela.appendChild(el('p', 'painel-vazio', 'Nenhuma variante selecionada.'));
      return;
    }
    var ordenadas = ordenarExecucoes(filtradas);
    var tbl = el('table', 'painel-tabela');
    var thead = el('thead');
    var hr = el('tr');
    COLUNAS_TABELA.forEach(function (col) {
      var th = el('th', col.tipo === 'num' ? 'painel-num' : null);
      var ativa = sortCol === col.id;
      var ariaSort = ativa ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none';
      th.setAttribute('aria-sort', ariaSort);
      th.setAttribute('tabindex', '0');
      th.setAttribute('data-coluna', col.id);
      th.appendChild(document.createTextNode(col.titulo));
      var seta = ativa ? (sortDir === 'asc' ? '\u25b2' : '\u25bc') : '\u21c5';
      var ind = el('span', 'painel-ordem', seta);
      ind.setAttribute('aria-hidden', 'true');
      th.appendChild(ind);
      th.addEventListener('click', function () {
        alternarOrdenacao(col.id);
      });
      th.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') {
          ev.preventDefault();
          alternarOrdenacao(col.id);
        }
      });
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    tbl.appendChild(thead);

    var tbody = el('tbody');
    ordenadas.forEach(function (e) {
      var tr = el('tr');
      if (selecionada && selecionada.variante === e.variante && selecionada.escala === e.escala) {
        tr.setAttribute('data-ativo', '1');
      }
      COLUNAS_TABELA.forEach(function (col) {
        if (col.id === 'variante') {
          var tdVar = el('td');
          var dot = el('span', 'painel-cor');
          dot.style.background = corVariante(e.variante);
          dot.style.marginRight = '6px';
          tdVar.appendChild(dot);
          tdVar.appendChild(document.createTextNode(e.rotulo));
          tr.appendChild(tdVar);
          return;
        }
        var td = el('td', col.tipo === 'num' ? 'painel-num' : null, col.texto(e));
        if (col.id === 'estado' && e.incompleta && e.motivo_incompleta) {
          td.setAttribute('title', e.motivo_incompleta);
        }
        tr.appendChild(td);
      });
      tr.addEventListener('click', function () {
        selecionada = e;
        render();
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    areaTabela.appendChild(tbl);
  }

  function renderDetalhe() {
    areaDetalhe.textContent = '';
    if (!selecionada) return;
    var e = selecionada;
    areaDetalhe.appendChild(el('h4', null,
      e.rotulo + ' \u2014 ' + fmtInt(e.escala) + ' linhas (execu\u00e7\u00e3o ' + e.numero + ')'
    ));
    var resumo = 'Casos: ' + e.casos + ' | Aprovadas: ' + e.aprovadas +
      ' | Reprovadas: ' + e.reprovadas + ' | Erro infra: ' + e.erro_infra +
      ' | N\u00e3o executadas: ' + e.nao_executadas;
    if (e.iteracoes_media !== null && e.iteracoes_media !== undefined) {
      resumo += ' | Itera\u00e7\u00f5es m\u00e9dias: ' + fmt(e.iteracoes_media, 1);
    }
    areaDetalhe.appendChild(el('div', null, resumo));
    if (e.motivo_incompleta) {
      areaDetalhe.appendChild(el('div', null, 'Motivo: ' + e.motivo_incompleta));
    } else if (e.nota) {
      areaDetalhe.appendChild(el('div', null, 'Nota: ' + e.nota));
    }
    if (e.casos_detalhe && e.casos_detalhe.length) {
      var tbl = el('table', 'painel-tabela');
      var thead = el('thead');
      var hr = el('tr');
      ['Caso', 'Categoria', 'Aprov.', 'Reprov.', 'Infra', 'Iter. m\u00e9dia'].forEach(function (h) {
        hr.appendChild(el('th', null, h));
      });
      thead.appendChild(hr);
      tbl.appendChild(thead);
      var tbody = el('tbody');
      e.casos_detalhe.forEach(function (c) {
        var tr = el('tr');
        tr.appendChild(el('td', null, c.id));
        tr.appendChild(el('td', null, c.categoria));
        tr.appendChild(el('td', null, c.aprovadas));
        tr.appendChild(el('td', null, c.reprovadas));
        tr.appendChild(el('td', null, c.erro_infra));
        tr.appendChild(el('td', null, fmt(c.iteracoes_media, 1)));
        tbody.appendChild(tr);
      });
      tbl.appendChild(tbody);
      areaDetalhe.appendChild(tbl);
    }
  }

  function estadoCelulaCaso(c) {
    var a = c.aprovadas || 0;
    var r = c.reprovadas || 0;
    var i = c.erro_infra || 0;
    if (a > 0 && r === 0 && i === 0) {
      return { id: 'aprovada', cls: 'painel-cel-aprovada', rotulo: 'Todas aprovadas' };
    }
    if (a === 0 && r === 0) {
      return { id: 'infra', cls: 'painel-cel-infra', rotulo: 'S\u00f3 infra' };
    }
    if (a === 0 && r > 0) {
      return { id: 'reprovada', cls: 'painel-cel-reprovada', rotulo: 'Nenhuma aprovada' };
    }
    return { id: 'mista', cls: 'painel-cel-mista', rotulo: 'Mistas' };
  }

  function renderMatriz(filtradas) {
    areaMatriz.textContent = '';
    if (!filtradas.length) return;

    var varsAtivas = variantes.filter(function (v) { return !!ativas[v.id]; });
    var escalasAtivas = escalas.filter(function (esc) {
      return filtradas.some(function (e) { return e.escala === esc; });
    });
    if (!varsAtivas.length || !escalasAtivas.length) return;

    var mapaExec = {};
    var casosMapa = {};
    filtradas.forEach(function (e) {
      var chaveExec = e.escala + '|' + e.variante;
      var porCaso = {};
      (e.casos_detalhe || []).forEach(function (c) {
        porCaso[c.id] = c;
        if (!casosMapa[c.id]) {
          casosMapa[c.id] = c.categoria || '';
        }
      });
      mapaExec[chaveExec] = porCaso;
    });

    var casosIds = Object.keys(casosMapa).sort();
    if (!casosIds.length) return;

    areaMatriz.appendChild(el('h4', null, 'Matriz por caso (aprovadas/reprovadas/infra)'));

    var legenda = el('div', 'painel-matriz-legenda');
    [
      { cls: 'painel-cel-aprovada', txt: 'Todas aprovadas' },
      { cls: 'painel-cel-mista', txt: 'Mistas' },
      { cls: 'painel-cel-reprovada', txt: 'Nenhuma aprovada' },
      { cls: 'painel-cel-infra', txt: 'S\u00f3 infra' }
    ].forEach(function (item) {
      var sp = el('span', 'painel-matriz-item');
      sp.appendChild(el('span', 'painel-matriz-amostra ' + item.cls));
      sp.appendChild(document.createTextNode(item.txt));
      legenda.appendChild(sp);
    });
    areaMatriz.appendChild(legenda);

    var wrap = el('div', 'painel-tabela-wrap');
    var tbl = el('table', 'painel-tabela painel-matriz-tabela');
    var thead = el('thead');
    var trEsc = el('tr');
    var thCaso = el('th', null, 'Caso');
    thCaso.setAttribute('rowspan', '2');
    thCaso.setAttribute('scope', 'col');
    trEsc.appendChild(thCaso);

    escalasAtivas.forEach(function (esc) {
      var th = el('th', null, fmtRazao(esc / 60) + ' (' + fmtInt(esc) + ')');
      th.setAttribute('colspan', String(varsAtivas.length));
      th.setAttribute('scope', 'colgroup');
      trEsc.appendChild(th);
    });
    thead.appendChild(trEsc);

    var trVar = el('tr');
    escalasAtivas.forEach(function () {
      varsAtivas.forEach(function (v) {
        var th = el('th');
        th.setAttribute('scope', 'col');
        var dot = el('span', 'painel-cor');
        dot.style.background = v.cor;
        dot.style.marginRight = '4px';
        th.appendChild(dot);
        th.appendChild(document.createTextNode(v.rotulo));
        trVar.appendChild(th);
      });
    });
    thead.appendChild(trVar);
    tbl.appendChild(thead);

    var tbody = el('tbody');
    casosIds.forEach(function (casoId) {
      var tr = el('tr');
      var thRow = el('th', null, casoId);
      thRow.setAttribute('scope', 'row');
      if (casosMapa[casoId]) {
        thRow.setAttribute('title', 'Categoria: ' + casosMapa[casoId]);
      }
      tr.appendChild(thRow);

      escalasAtivas.forEach(function (esc) {
        varsAtivas.forEach(function (v) {
          var porCaso = mapaExec[esc + '|' + v.id] || {};
          var c = porCaso[casoId];
          if (!c) {
            var tdVazio = el('td', 'painel-cel-vazia', '\u2014');
            tdVazio.setAttribute(
              'title',
              casoId + ' \u2014 ' + v.rotulo + ' (' + fmtInt(esc) + ' linhas): sem dado'
            );
            tr.appendChild(tdVazio);
            return;
          }
          var est = estadoCelulaCaso(c);
          var texto = (c.aprovadas || 0) + '/' + (c.reprovadas || 0) + '/' + (c.erro_infra || 0);
          var iterTxt = (c.iteracoes_media !== null && c.iteracoes_media !== undefined)
            ? fmt(c.iteracoes_media, 1)
            : 'sem dado';
          var tokTxt = (c.tokens_entrada_medio !== null && c.tokens_entrada_medio !== undefined)
            ? fmtInt(Math.round(c.tokens_entrada_medio))
            : 'sem dado';
          var dica = casoId + ' \u2014 ' + v.rotulo + ' (' + fmtInt(esc) + ' linhas): ' +
            est.rotulo + ' (' + texto + ') | Itera\u00e7\u00f5es m\u00e9dias: ' + iterTxt +
            ' | Tokens m\u00e9dios: ' + tokTxt;

          var td = el('td', est.cls, texto);
          td.setAttribute('data-estado', est.id);
          td.setAttribute('title', dica);
          td.setAttribute('aria-label', dica);
          tr.appendChild(td);
        });
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);
    areaMatriz.appendChild(wrap);
  }

  function render() {
    var met = metricaAtual();
    var filtradas = execucoes.filter(function (e) { return !!ativas[e.variante]; });
    if (filtradas.length && (!selecionada || !ativas[selecionada.variante])) {
      selecionada = filtradas[0];
    } else if (!filtradas.length) {
      selecionada = null;
    }
    renderResumo(filtradas);
    renderSvg(met, filtradas);
    renderTabela(filtradas);
    renderDetalhe();
    renderMatriz(filtradas);
  }

  render();
})();
"""


def _json_seguro_html(objeto: object) -> str:
    """Serializa JSON de forma determinística e segura para embutir em `<script>` HTML."""
    texto = json.dumps(objeto, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return texto.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _dados_painel_escala(dados: DadosEvolucao) -> dict[str, object]:
    """Extrai o payload determinístico do painel interativo para o modo escala."""
    variantes, escalas, ultimas = _ultimas_por_escala(dados.execucoes)
    cores = _cores_variantes(variantes)
    execucoes_payload: list[dict[str, object]] = []
    for variante in variantes:
        for escala in escalas:
            execucao = ultimas.get((variante, escala))
            if execucao is None:
                continue
            tokens_entrada = (
                execucao.tokens_entrada_por_rodada_executada
                if execucao.tokens_entrada_por_rodada_executada is not None
                else execucao.tokens_entrada_por_rodada
            )
            execucoes_payload.append(
                {
                    "aprovadas": execucao.aprovadas,
                    "casos": execucao.casos,
                    "casos_detalhe": [
                        {
                            "aprovadas": caso.aprovadas,
                            "categoria": caso.categoria,
                            "erro_infra": caso.erro_infra,
                            "id": caso.id,
                            "iteracoes_media": caso.iteracoes_media,
                            "reprovadas": caso.reprovadas,
                            "tokens_entrada_medio": caso.tokens_entrada_medio,
                        }
                        for caso in sorted(execucao.casos_detalhe, key=lambda c: c.id)
                    ],
                    "categorias_pct": dict(sorted(execucao.categorias_pct.items())),
                    "custo_por_acerto_usd": execucao.custo_por_acerto_usd,
                    "erro_infra": execucao.erro_infra,
                    "escala": escala,
                    "incompleta": execucao.incompleta,
                    "iniciado_em": execucao.iniciado_em,
                    "iteracoes_media": execucao.iteracoes_media,
                    "motivo_incompleta": execucao.motivo_incompleta,
                    "nao_executadas": execucao.nao_executadas,
                    "nota": execucao.nota,
                    "numero": execucao.numero,
                    "ponto_fraco": execucao.ponto_fraco,
                    "reprovadas": execucao.reprovadas,
                    "rodadas": execucao.rodadas,
                    "rodadas_validas": execucao.rodadas_validas,
                    "rotulo": _rotulo_variante(variante),
                    "tokens_entrada": tokens_entrada,
                    "tokens_saida": execucao.tokens_saida_por_rodada,
                    "valores": {
                        "custo": _custo_escala_milesimos(execucao),
                        "custo_acerto": _para_milesimos(execucao.custo_por_acerto_usd),
                        "iteracoes": execucao.iteracoes_media,
                        "pico": execucao.pico_contexto_medio,
                        "taxa": execucao.taxa_pct,
                        "tokens": _tokens_escala_mil(execucao),
                    },
                    "variante": variante,
                }
            )
    return {
        "escalas": escalas,
        "execucoes": execucoes_payload,
        "metricas": [
            {
                "casas": 1,
                "id": "tokens",
                "teto": None,
                "titulo": "Tokens de entrada por rodada (mil)",
                "unidade": "mil tokens",
            },
            {
                "casas": 2,
                "id": "custo",
                "teto": None,
                "titulo": "Custo por rodada (milésimos de US$)",
                "unidade": "milésimos de US$",
            },
            {
                "casas": 0,
                "id": "pico",
                "teto": None,
                "titulo": "Pico de contexto médio (tokens)",
                "unidade": "tokens",
            },
            {
                "casas": 1,
                "id": "taxa",
                "teto": 100.0,
                "titulo": "Taxa de acerto (%)",
                "unidade": "%",
            },
            {
                "casas": 2,
                "id": "iteracoes",
                "teto": None,
                "titulo": "Iterações por pedido",
                "unidade": "iterações por pedido",
            },
            {
                "casas": 2,
                "id": "custo_acerto",
                "teto": None,
                "titulo": "Custo por acerto (milésimos de US$)",
                "unidade": "milésimos de US$",
            },
        ],
        "variantes": [
            {
                "cor": cores[variante],
                "id": variante,
                "rotulo": _rotulo_variante(variante),
            }
            for variante in variantes
        ],
    }


def _motivos_ressalva(execucao: Execucao) -> str:
    """Descreve os motivos de ressalva (poucas rodadas válidas e/ou execução incompleta)."""
    motivos: list[str] = []
    if execucao.ponto_fraco:
        nota = execucao.nota.strip()
        motivos.append(nota or "poucas rodadas válidas")
    if execucao.incompleta:
        motivo = execucao.motivo_incompleta.strip()
        motivos.append(motivo or "execução incompleta")
    return "; ".join(motivos)


def _caixa_ressalvas_html(dados: DadosEvolucao) -> str:
    """Caixa HTML 'Como ler e ressalvas' com execuções incompletas/fracas e regra de leitura."""
    variantes, escalas, ultimas = _ultimas_por_escala(dados.execucoes)
    itens: list[str] = []
    for variante in variantes:
        for escala in escalas:
            execucao = ultimas.get((variante, escala))
            if execucao is None or not _eh_oco(execucao):
                continue
            rotulo = (
                f"{_rotulo_variante(variante)}, {_inteiro_br(escala)} linhas "
                f"(execução {execucao.numero})"
            )
            motivo = _motivos_ressalva(execucao)
            itens.append(f"<li><strong>{_esc(rotulo)}</strong>: {_esc(motivo)}</li>")
    lista_html = (
        "<ul>\n" + "\n".join(itens) + "\n</ul>"
        if itens
        else "<p>Nenhuma execução incompleta ou com poucas rodadas válidas.</p>"
    )
    return "\n".join(
        [
            '<div class="painel-ressalvas">',
            "<h3>Como ler e ressalvas</h3>",
            "<p><code>erro_infra</code> = chamada ao modelo passou do prazo; "
            "taxa de acerto conta só rodadas válidas.</p>",
            lista_html,
            "</div>",
        ]
    )


def _painel_interativo_html(dados: DadosEvolucao) -> str:
    """Seção HTML do painel interativo no modo escala, com JSON embutido e JS puro."""
    json_dados = _json_seguro_html(_dados_painel_escala(dados))
    partes = [
        '<section id="painel-interativo">',
        "<h2>Painel interativo</h2>",
        '<p class="leitura">Filtre variantes, alterne métricas e clique em um ponto ou linha '
        "da tabela para inspecionar a execução.</p>",
        '<div id="painel-raiz"></div>',
        _caixa_ressalvas_html(dados),
        f'<script type="application/json" id="dados-painel">{json_dados}</script>',
        f"<script>\n{_SCRIPT_PAINEL}</script>",
        "</section>",
    ]
    return "\n".join(partes)


def gerar_pagina_html(dados: DadosEvolucao) -> str:
    """Página HTML5 autônoma com os gráficos inline e tooltips. Sem recurso externo.

    No modo escala (`tem_modo_escala(dados)`), inclui a seção "Painel interativo" antes dos
    gráficos estáticos de escala (painéis e os quatro individuais). Os gráficos por execução
    (`taxa-total`, `taxa-por-categoria`, `iteracoes-por-pedido`, `custo-por-rodada`), o painel
    de barras da comparação (`comparacao-de-variantes`), a dispersão
    (`dispersao-iteracoes-tokens`) e a curva de contexto (`curva-de-contexto`) são omitidos porque
    misturam execuções de escalas diferentes sem distinguir a escala.

    Fora do modo escala, os gráficos de linha por execução só entram quando alguma variante tem
    duas ou mais execuções.
    """
    secoes: list[str] = []
    if tem_modo_escala(dados):
        secoes.append(_painel_interativo_html(dados))
        titulo_escala, leitura_escala = SECAO_ESCALA
        titulo_ep, leitura_ep = SECAO_ESCALA_PAINEIS
        partes_escala = [
            f"<h2>{_esc(titulo_escala)}</h2>",
            f'<p class="leitura">{_esc(leitura_escala)}</p>',
            f"<h3>{_esc(titulo_ep)}</h3>",
            f'<p class="leitura">{_esc(leitura_ep)}</p>',
            _com_identificador(grafico_escala_paineis(dados), "ep"),
        ]
        for indice, ((_, titulo_e, leitura_e), svg_e) in enumerate(
            zip(SECOES_ESCALA, _svgs_escala(dados), strict=True), 1
        ):
            partes_escala.extend(
                [
                    f"<h3>{_esc(titulo_e)}</h3>",
                    f'<p class="leitura">{_esc(leitura_e)}</p>',
                    _com_identificador(svg_e, f"e{indice}"),
                ]
            )
        secoes.append("<section>\n" + "\n".join(partes_escala) + "</section>")
    else:
        comparacao = _comparacao_se_houver(dados)
        if comparacao is not None:
            titulo_comparacao, leitura_comparacao = SECAO_COMPARACAO
            secoes.append(
                f"<section>\n<h2>{_esc(titulo_comparacao)}</h2>\n"
                f'<p class="leitura">{_esc(leitura_comparacao)}</p>\n'
                f"{_com_identificador(comparacao, 'c')}</section>"
            )
        for indice, (_, titulo, leitura, fn) in enumerate(_geradores_fora_escala(dados), 1):
            secoes.append(
                f"<section>\n<h2>{_esc(titulo)}</h2>\n"
                f'<p class="leitura">{_esc(leitura)}</p>\n'
                f"{_com_identificador(fn(dados), str(indice))}</section>"
            )
    intro = dados.descricao or "Métricas por execução real da avaliação."
    css = f"{_CSS}{_CSS_PAINEL}" if tem_modo_escala(dados) else _CSS
    linhas = [
        "<!doctype html>",
        '<html lang="pt-BR">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">',
        "<title>Avaliações do agente: gráficos</title>",
        f"<style>\n{css}</style>",
        "</head>",
        "<body>",
        "<main>",
        "<h1>Avaliações do agente: gráficos das execuções</h1>",
        f'<p class="intro">{_esc(intro)} Passe o mouse, toque ou use o Tab sobre um ponto '
        "para ver o valor.</p>",
        *secoes,
        "</main>",
        f"<script>\n{_SCRIPT}</script>",
        "</body>",
        "</html>",
    ]
    return "\n".join(linhas) + "\n"


def gerar_todos(dados: DadosEvolucao, pasta: Path) -> list[Path]:
    """Grava os SVG e a página `graficos.html`, com nomes fixos. Sobrescreve.

    Fora do modo escala, grava os seis SVG em `NOMES_ARQUIVOS` quando alguma variante tem duas ou
    mais execuções (e `comparacao-de-variantes.svg` quando há duas ou mais variantes) e depois a
    página. Quando cada variante tem no máximo uma execução, grava apenas a dispersão, a curva de
    contexto, `comparacao-de-variantes.svg` (se houver >=2 variantes) e a página.

    No modo escala (`tem_modo_escala(dados)`), grava apenas os quatro SVG de escala
    (`NOMES_ESCALA`), o painel combinado (`NOME_ESCALA_PAINEIS`) e a página: os gráficos de linha
    por execução (`taxa-total`, `taxa-por-categoria`, `iteracoes-por-pedido`, `custo-por-rodada`),
    o painel de barras da comparação (`comparacao-de-variantes`), a dispersão
    (`dispersao-iteracoes-tokens`) e a curva de contexto (`curva-de-contexto`) são omitidos porque
    misturam execuções de escalas diferentes sem distinguir a escala.
    """
    if tem_modo_escala(dados):
        arquivos = list(zip(NOMES_ESCALA, _svgs_escala(dados), strict=True))
        arquivos.append((NOME_ESCALA_PAINEIS, grafico_escala_paineis(dados)))
    else:
        arquivos = [(nome, fn(dados)) for nome, _, _, fn in _geradores_fora_escala(dados)]
        comparacao = _comparacao_se_houver(dados)
        if comparacao is not None:
            arquivos.append((NOME_COMPARACAO, comparacao))
    pagina = gerar_pagina_html(dados)
    caminhos: list[Path] = []
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        for nome, svg in arquivos:
            caminho = pasta / nome
            caminho.write_text(svg, encoding="utf-8")
            caminhos.append(caminho)
        caminho_pagina = pasta / NOME_PAGINA
        caminho_pagina.write_text(pagina, encoding="utf-8")
        caminhos.append(caminho_pagina)
    except OSError as erro:
        motivo = erro.strerror or type(erro).__name__
        raise ErroDeGraficos(f"não foi possível gravar os gráficos em {pasta}: {motivo}") from erro
    return caminhos
