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
"""

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

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
NOME_PAGINA: Final = "graficos.html"
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
    taxa_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    erro_infra_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    iteracoes_media: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tokens_entrada_por_rodada: int | None = Field(default=None, ge=0)
    tokens_saida_por_rodada: int | None = Field(default=None, ge=0)
    custo_por_rodada_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    categorias_pct: dict[str, float | None] = Field(default_factory=dict)
    prompt: str = ""
    ponto_fraco: bool = False
    nota: str = ""


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


def _cabecalho(titulo: str, descricao: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{LARGURA}" height="{ALTURA}" '
        f'viewBox="0 0 {LARGURA} {ALTURA}" role="img" aria-labelledby="t d" '
        f'font-family="{FONTE}">',
        f'<title id="t">{_esc(titulo)}</title>',
        f'<desc id="d">{_esc(descricao)}</desc>',
        f'<rect x="0.5" y="0.5" width="{LARGURA - 1}" height="{ALTURA - 1}" rx="8" '
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


def _dica(texto: str, execucao: Execucao) -> str:
    """Tooltip de uma execução. Marcador vazado acrescenta a nota (ou a frase padrão)."""
    if not execucao.ponto_fraco:
        return texto
    nota = execucao.nota.strip()
    return f"{texto}. Marcador vazado: {nota or 'poucas rodadas válidas'}"


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
                    oco=execucao.ponto_fraco,
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
    """Legenda do marcador vazado, se alguma execução plotada tem ponto fraco."""
    fracas = [execucao for execucao in plotadas if execucao.ponto_fraco]
    if not fracas:
        return []
    if len(fracas) == 1 and 0 < len(fracas[0].nota) <= TETO_NOTA_CURTA:
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
                    oco=execucao.ponto_fraco,
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
                    oco=execucao.ponto_fraco,
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


def _todos_os_svgs(dados: DadosEvolucao) -> list[str]:
    """Os seis SVG, na ordem de `NOMES_ARQUIVOS`."""
    return [
        grafico_taxa_total(dados),
        grafico_taxa_por_categoria(dados),
        grafico_iteracoes(dados),
        grafico_dispersao_iteracoes_tokens(dados),
        grafico_curva_de_contexto(dados),
        grafico_custo_por_rodada(dados),
    ]


def _com_identificador(svg: str, sufixo: str) -> str:
    """Troca os ids `t` e `d` do SVG por ids com sufixo, para não repetir na mesma página."""
    return (
        svg.replace('aria-labelledby="t d"', f'aria-labelledby="t{sufixo} d{sufixo}"')
        .replace('<title id="t">', f'<title id="t{sufixo}">')
        .replace('<desc id="d">', f'<desc id="d{sufixo}">')
    )


def gerar_pagina_html(dados: DadosEvolucao) -> str:
    """Página HTML5 autônoma com os seis gráficos inline e tooltips. Sem recurso externo."""
    svgs = _todos_os_svgs(dados)
    secoes: list[str] = []
    for indice, ((_, titulo, leitura), svg) in enumerate(zip(SECOES, svgs, strict=True), 1):
        secoes.append(
            f"<section>\n<h2>{_esc(titulo)}</h2>\n"
            f'<p class="leitura">{_esc(leitura)}</p>\n'
            f"{_com_identificador(svg, str(indice))}</section>"
        )
    intro = dados.descricao or "Métricas por execução real da avaliação."
    linhas = [
        "<!doctype html>",
        '<html lang="pt-BR">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">',
        "<title>Avaliações do agente: gráficos</title>",
        f"<style>\n{_CSS}</style>",
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
    """Grava os seis SVG e a página `graficos.html`, com nomes fixos. Sobrescreve.

    A pasta é criada se preciso. Devolve os sete caminhos: os SVG na ordem de `NOMES_ARQUIVOS`,
    e depois a página.
    """
    svgs = _todos_os_svgs(dados)
    pagina = gerar_pagina_html(dados)
    caminhos: list[Path] = []
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        for nome, svg in zip(NOMES_ARQUIVOS, svgs, strict=True):
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
