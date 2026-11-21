"""Correção automática dos casos de avaliação, por regras e sem modelo juiz (spec v0, Fase 3).

Só a RESPOSTA FINAL e as FONTES são avaliadas. Código, trace e passos ficam de fora. Por isso,
uma resposta certa dada com código ruim passa aqui; esse tipo de problema aparece na análise
de erros, não na correção.

Decisões deste módulo (não estão na spec):
- Texto é normalizado antes de comparar: NFKD, sem acento, sem caracteres invisíveis, em
  minúsculas (casefold) e com espaços normalizados. Só dígitos 0-9 contam como número.
- Trecho só casa por palavra inteira: "Ana" não casa com "Banana".
- `extrair_numeros` lê formato brasileiro. Vírgula é sempre o separador decimal. Ponto seguido
  de três dígitos, em grupos de um a três dígitos, é milhar: `1.234` vale 1234. Ponto sem vírgula
  fora desse padrão é decimal: `1234.56` e `1.5`. Então `1,234` vale 1,234.
- Sinal negativo é `-` ou `−` antes do número, com ou sem `R$` no meio. Número entre parênteses
  é lido como positivo, porque parênteses em texto corrido são comuns.
- `mil`, `milhão`, `milhões`, `bilhão` e `bilhões` logo depois do número multiplicam por sua escala.
- Número colado a letra (`PR2026`, `R2D2`) não é número. Número ligado por `-` ou `/` a outro
  caractere alfanumérico é parte de id ou data: `PR-2026-005` e `30/11/2026` não geram números.
- Números em outros alfabetos (dígitos árabes, por exemplo) não contam.
- Cada regra é avaliada, mesmo depois de uma falha, para o relatório mostrar tudo.
- Falha fechada: erro inesperado numa regra conta como reprovação. `corrigir` nunca levanta.
- O detalhe de cada regra é truncado; a resposta nunca é copiada inteira.

Limitações conhecidas:
- Regra de texto não entende negação: "não é R$ 1.234,56" passa em `ContemNumero`.
- `RecusaAcesso` depende da lista de frases. Pode reprovar uma recusa correta dita de outro jeito;
  preferimos esse falso alarme a aprovar uma resposta que não recusou.
- Muitos números na resposta aumentam a chance de acerto por sorte. As perguntas da T17 devem
  pedir um valor só.
- A unidade de `ContemNumero` só muda a descrição; a comparação não confere % nem R$.
- Moeda não é verificada: "US$ 100" conta como 100.
- Anos e outros números soltos contam: "de 2026" casa com `ContemNumero(2026)`.
- Intervalo sem espaços ("1.500,00-2.000,00") não é lido como dois números.
- Número entre parênteses nunca é negativo, e separador inglês ("1,234.56") não é lido.
"""

import re
import unicodedata
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.evals.casos import (
    Caso,
    CitaFontes,
    ContemAlgum,
    ContemNumero,
    ContemTodos,
    NaoContem,
    Regra,
    formatar_numero_br,
)
from harness.orchestrator.loop import ResultadoPedido

_LIMITE_TRECHO: Final = 120
_LIMITE_DETALHE: Final = 200
_LIMITE_TEXTO_NO_DETALHE: Final = 60
_MAXIMO_NUMEROS_NO_DETALHE: Final = 5
_TOLERANCIA_NAO_CONTEM: Final = Decimal("0.01")

_FRASES_DE_RECUSA: Final = (
    "não tenho acesso",
    "sem acesso",
    "não está acessível",
    "não está disponível",
    "não é possível acessar",
    "não posso acessar",
    "fora do meu acesso",
)

# Número: milhar com decimal opcional, ou decimal com ponto, ou decimal com vírgula.
_NUMERO: Final = re.compile(
    r"(?<!\d)(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+\.\d+|\d+(?:,\d+)?)(?!\d)", re.ASCII
)
_MILHAR: Final = re.compile(r"\d{1,3}(?:\.\d{3})+", re.ASCII)
_ESCALA: Final = re.compile(r"[ ]*(milhoes|milhao|bilhoes|bilhao|mil)(?!\w)")
_ESCALAS: Final[dict[str, int]] = {
    "mil": 10**3,
    "milhao": 10**6,
    "milhoes": 10**6,
    "bilhao": 10**9,
    "bilhoes": 10**9,
}
_PREFIXOS_DE_MOEDA: Final = ("r$", "us$", "$")


class ResultadoRegra(BaseModel):
    """O que uma regra do gabarito encontrou: passou ou não, e um detalhe curto."""

    model_config = ConfigDict(frozen=True)

    descricao: str
    passou: bool
    detalhe: str


class Veredito(BaseModel):
    """Resultado da correção de um caso. Aprovado só se todas as regras passaram."""

    model_config = ConfigDict(frozen=True)

    caso_id: str
    aprovado: bool
    regras: tuple[ResultadoRegra, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _aprovado_so_se_todas_passam(self) -> "Veredito":
        if self.aprovado != all(regra.passou for regra in self.regras):
            raise ValueError("aprovado precisa ser verdadeiro exatamente quando todas passam")
        return self


# --- normalização -----------------------------------------------------------------------------


def _limpar(texto: str) -> str:
    """Texto sem acento, sem invisíveis, em minúsculas e com espaços normalizados."""
    decomposto = unicodedata.normalize("NFKD", texto)
    partes: list[str] = []
    for caractere in decomposto:
        categoria = unicodedata.category(caractere)
        if categoria == "Cf" or unicodedata.combining(caractere):
            continue
        partes.append(" " if categoria == "Cc" else caractere)
    return " ".join("".join(partes).casefold().split())


_FRASES_NORMALIZADAS: Final = tuple(_limpar(frase) for frase in _FRASES_DE_RECUSA)


def _contem(texto_normalizado: str, trecho_normalizado: str) -> bool:
    """Verdadeiro se o trecho aparece como palavra inteira no texto (ambos já normalizados)."""
    if not trecho_normalizado:
        return False
    padrao = rf"(?<!\w){re.escape(trecho_normalizado)}(?!\w)"
    return re.search(padrao, texto_normalizado) is not None


def _partes_do_caminho(caminho: str) -> tuple[str, ...]:
    """Componentes de um caminho de fonte, normalizados. Ignora vazios e `.`."""
    limpo = _limpar(caminho).replace("\\", "/")
    return tuple(parte.strip() for parte in limpo.split("/") if parte.strip() not in ("", "."))


def _fonte_casa(esperada: str, citada: str) -> bool:
    """Verdadeiro se a fonte esperada é o fim do caminho citado, componente por componente."""
    partes_esperadas = _partes_do_caminho(esperada)
    partes_citadas = _partes_do_caminho(citada)
    if not partes_esperadas or len(partes_citadas) < len(partes_esperadas):
        return False
    return partes_citadas[-len(partes_esperadas) :] == partes_esperadas


# --- números ----------------------------------------------------------------------------------


def _converter(bruto: str) -> Decimal:
    """Converte o token numérico (já validado pela regex) para Decimal, em formato brasileiro."""
    if "," in bruto:
        return Decimal(bruto.replace(".", "").replace(",", "."))
    if _MILHAR.fullmatch(bruto):
        return Decimal(bruto.replace(".", ""))
    return Decimal(bruto)


def _colado_ou_em_id(texto: str, inicio: int, fim: int) -> bool:
    """Verdadeiro se o número é parte de uma palavra, de um id ou de uma data."""
    if inicio > 0:
        antes = texto[inicio - 1]
        if antes.isalnum() or antes == "_":
            return True
        if antes in ".," and inicio > 1 and texto[inicio - 2].isdigit():
            return True
        if antes in "-/" and inicio > 1 and texto[inicio - 2].isalnum():
            return True
    if fim + 1 < len(texto) and texto[fim] in "-/" and texto[fim + 1].isalnum():
        return True
    return False


def _recuar_espacos(texto: str, indice: int) -> int:
    while indice > 0 and texto[indice - 1] == " ":
        indice -= 1
    return indice


def _negativo(texto: str, inicio: int) -> bool:
    """Verdadeiro se há sinal de menos logo antes do número, com ou sem prefixo de moeda."""
    indice = _recuar_espacos(texto, inicio)
    if indice > 0 and texto[indice - 1] not in "-−":
        for prefixo in _PREFIXOS_DE_MOEDA:
            if texto[:indice].endswith(prefixo):
                indice = _recuar_espacos(texto, indice - len(prefixo))
                break
    if indice == 0 or texto[indice - 1] not in "-−":
        return False
    sinal = indice - 1
    # Hífen colado a uma palavra ou a um número separa id ou data, não é sinal.
    return texto[sinal] == "−" or sinal == 0 or not texto[sinal - 1].isalnum()


def _numeros_do_texto(limpo: str) -> list[Decimal]:
    numeros: list[Decimal] = []
    for encontrado in _NUMERO.finditer(limpo):
        inicio, fim = encontrado.span()
        if _colado_ou_em_id(limpo, inicio, fim):
            continue
        valor = _converter(encontrado.group())
        escala = _ESCALA.match(limpo, fim)
        if escala is not None:
            valor *= _ESCALAS[escala.group(1)]
        if _negativo(limpo, inicio):
            valor = -valor
        numeros.append(valor)
    return numeros


def extrair_numeros(texto: str) -> list[Decimal]:
    """Números lidos de um texto em formato brasileiro, na ordem em que aparecem.

    Ex.: "R$ 1.234,56" vira 1234.56; "2,5 mil" vira 2500; "18%" vira 18; "PR-2026-005" não
    gera nada. Nunca levanta exceção: texto que não se entende devolve uma lista parcial ou vazia.
    """
    try:
        return _numeros_do_texto(_limpar(texto))
    except Exception:
        return []


# --- regras -----------------------------------------------------------------------------------


def _trecho(texto: str) -> str:
    """Começo da resposta, em uma linha, para o detalhe. Nunca passa de 120 caracteres."""
    limpo = " ".join(texto.split())
    if not limpo:
        return "(vazia)"
    if len(limpo) > _LIMITE_TRECHO:
        return f"'{limpo[:_LIMITE_TRECHO]}...'"
    return f"'{limpo}'"


def _curto(texto: str) -> str:
    limpo = " ".join(texto.split())
    if len(limpo) > _LIMITE_TEXTO_NO_DETALHE:
        limpo = limpo[: _LIMITE_TEXTO_NO_DETALHE - 3] + "..."
    return f"'{limpo}'"


def _listar_numeros(numeros: Sequence[Decimal]) -> str:
    texto = ", ".join(formatar_numero_br(n) for n in numeros[:_MAXIMO_NUMEROS_NO_DETALHE])
    restantes = len(numeros) - _MAXIMO_NUMEROS_NO_DETALHE
    if restantes > 0:
        texto += f" (e mais {restantes})"
    return texto


def _limitar(texto: str) -> str:
    if len(texto) <= _LIMITE_DETALHE:
        return texto
    return texto[: _LIMITE_DETALHE - 3] + "..."


def _verificar_numero(regra: ContemNumero, resposta: str) -> tuple[bool, str]:
    numeros = extrair_numeros(resposta)
    if any(abs(numero - regra.valor) <= regra.tolerancia for numero in numeros):
        return True, "ok"
    if not numeros:
        return False, "nenhum número na resposta"
    return False, f"números lidos: {_listar_numeros(numeros)}"


def _verificar_todos(regra: ContemTodos, resposta_norm: str) -> tuple[bool, str]:
    ausentes = [texto for texto in regra.textos if not _contem(resposta_norm, _limpar(texto))]
    if not ausentes:
        return True, "ok"
    return False, "ausentes: " + ", ".join(_curto(texto) for texto in ausentes)


def _verificar_algum(regra: ContemAlgum, resposta_norm: str) -> tuple[bool, str]:
    if any(_contem(resposta_norm, _limpar(texto)) for texto in regra.textos):
        return True, "ok"
    return False, "nenhum dos textos aparece na resposta"


def _verificar_nao_contem(regra: NaoContem, resposta: str, resposta_norm: str) -> tuple[bool, str]:
    presentes: list[str] = [
        _curto(texto) for texto in regra.textos if _contem(resposta_norm, _limpar(texto))
    ]
    numeros_da_resposta = extrair_numeros(resposta)
    presentes.extend(
        formatar_numero_br(proibido)
        for proibido in regra.numeros
        if any(abs(numero - proibido) <= _TOLERANCIA_NAO_CONTEM for numero in numeros_da_resposta)
    )
    if not presentes:
        return True, "ok"
    return False, "presente na resposta: " + ", ".join(presentes)


def _verificar_fontes(regra: CitaFontes, fontes: Sequence[str]) -> tuple[bool, str]:
    ausentes = [
        esperada for esperada in regra.fontes if not any(_fonte_casa(esperada, c) for c in fontes)
    ]
    if not ausentes:
        return True, "ok"
    citadas = ", ".join(_curto(fonte) for fonte in fontes[:_MAXIMO_NUMEROS_NO_DETALHE])
    return False, f"fontes ausentes: {', '.join(_curto(a) for a in ausentes)}; citadas: {citadas}"


def _verificar_recusa(resposta: str, resposta_norm: str) -> tuple[bool, str]:
    if any(_contem(resposta_norm, frase) for frase in _FRASES_NORMALIZADAS):
        return True, "ok"
    return False, f"sem frase de recusa; resposta: {_trecho(resposta)}"


def _avaliar(regra: Regra, resposta: str, fontes: Sequence[str]) -> ResultadoRegra:
    """Avalia uma regra. Pode levantar exceção; quem chama decide o que fazer com ela."""
    resposta_norm = _limpar(resposta)
    if isinstance(regra, ContemNumero):
        passou, detalhe = _verificar_numero(regra, resposta)
    elif isinstance(regra, ContemTodos):
        passou, detalhe = _verificar_todos(regra, resposta_norm)
    elif isinstance(regra, ContemAlgum):
        passou, detalhe = _verificar_algum(regra, resposta_norm)
    elif isinstance(regra, NaoContem):
        passou, detalhe = _verificar_nao_contem(regra, resposta, resposta_norm)
    elif isinstance(regra, CitaFontes):
        passou, detalhe = _verificar_fontes(regra, fontes)
    else:
        passou, detalhe = _verificar_recusa(resposta, resposta_norm)
    return ResultadoRegra(
        descricao=_limitar(regra.descrever()), passou=passou, detalhe=_limitar(detalhe)
    )


def _avaliar_com_seguranca(regra: Regra, resposta: str, fontes: Sequence[str]) -> ResultadoRegra:
    """Falha fechada: erro inesperado numa regra conta como reprovação, com detalhe genérico."""
    try:
        return _avaliar(regra, resposta, fontes)
    except Exception:
        return ResultadoRegra(
            descricao="regra do gabarito",
            passou=False,
            detalhe="erro inesperado ao avaliar a regra",
        )


def _veredito(caso_id: str, regras: tuple[ResultadoRegra, ...]) -> Veredito:
    return Veredito(caso_id=caso_id, aprovado=all(r.passou for r in regras), regras=regras)


def _veredito_de_erro(caso_id: str) -> Veredito:
    erro = ResultadoRegra(descricao="correção", passou=False, detalhe="erro inesperado na correção")
    return _veredito(caso_id, (erro,))


# --- API pública ------------------------------------------------------------------------------


def corrigir(caso: Caso, resposta: str, fontes: Sequence[str]) -> Veredito:
    """Corrige a resposta final e as fontes contra o gabarito. Nunca levanta exceção."""
    try:
        regras = tuple(_avaliar_com_seguranca(regra, resposta, fontes) for regra in caso.gabarito)
        return _veredito(caso.id, regras)
    except Exception:
        return _veredito_de_erro(caso.id)


def corrigir_resultado(caso: Caso, resultado: ResultadoPedido) -> Veredito:
    """Corrige o resultado de um pedido. Sem FINAL, o caso reprova e a resposta não conta."""
    try:
        veredito = corrigir(caso, resultado.resposta, resultado.fontes)
        if resultado.completo:
            return veredito
        sem_final = ResultadoRegra(
            descricao="termina com FINAL",
            passou=False,
            detalhe=(
                f"pedido terminou por {resultado.motivo.value}, sem FINAL; a resposta não conta"
            ),
        )
        return _veredito(caso.id, (sem_final, *veredito.regras))
    except Exception:
        return _veredito_de_erro(caso.id)
