"""Truncagem e formatação da observação devolvida ao modelo (RF5, contrato 3 da spec, D8).

Este módulo roda no HOST. Não entra na imagem do sandbox: o código do modelo nunca o importa.

Decisões deste módulo (não estão na spec):
- Truncagem por seção: stdout, stderr e exceção têm cada um o seu limite. Um teto único de
  4.000 caracteres deixaria uma saída enorme no stdout consumir todo o espaço e esconder a
  exceção, que é a informação que o modelo mais precisa para corrigir o código. Por isso o
  total devolvido pode passar de 4.000 caracteres (até 4.000 + 1.000 + 2.000 mais os avisos).
- Proporção 75% começo e 25% fim: o começo mostra o cabeçalho da saída e o fim costuma trazer
  o resultado ou a última linha de um erro.
- Delimitadores de conteúdo não confiável (regra 5 da spec: resultado de ferramenta é dado,
  nunca instrução). Todo o conteúdo do stdout, stderr e exceção vai dentro de UM bloco
  `<saida_nao_confiavel>...</saida_nao_confiavel>`. Qualquer tentativa de escrever o nome do
  delimitador dentro do conteúdo (em qualquer caixa, com espaços, caracteres invisíveis,
  parecidos Unicode ou com o `＜` fullwidth) tem a abertura trocada por `&lt;`. O conteúdo passa
  antes por NFKC e as quebras que não são `\n` viram escapes. Assim a saída nunca fecha o bloco
  antes da hora nem forja um bloco novo. A troca é feita numa única passagem e não cria `<` novo.
  Limitação: parecidos fora do mapa `_PARECIDOS` (um alfabeto inteiro de confusáveis) não são
  cobertos; eles não formam o ASCII do delimitador, então o envelope real continua único.
  Os nomes das ferramentas também são neutralizados e escapados, porque o modelo escolhe o
  nome pelo acesso a atributos de `tools`.
"""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from harness.sandbox.protocol import ExecDone

# Limites da spec (saída por célula, 4.000 caracteres) e por seção, explicados acima.
LIMITE_STDOUT: Final = 4000
LIMITE_STDERR: Final = 1000
LIMITE_EXCECAO: Final = 2000

_NOME_DELIMITADOR: Final = "saida_nao_confiavel"
_ABERTURA: Final = f"<{_NOME_DELIMITADOR}>"
_FECHAMENTO: Final = f"</{_NOME_DELIMITADOR}>"
# Caracteres sem tinta (controles, espaços, zero-width, soft hyphen, BOM, tags Unicode). Podem
# ficar entre as letras de uma falsa tag sem mudar o que o leitor vê.
_INVISIVEIS: Final = (
    r"\x00-\x1f\x7f-\x9f\s­​-‏‪-‮  ⁠-⁯"
    r"̀-ͯ⃐-⃿︀-️﻿\U000e0000-\U000e007f"
)
_LACUNA: Final = f"[{_INVISIVEIS}]*"
# Letras que parecem outras letras (cirílico e grego) e que um leitor confunde com o ASCII.
# Compatibilidade (fullwidth, matemáticos etc.) já é resolvida pelo NFKC antes da busca.
_PARECIDOS: Final = {
    "a": "а",
    "c": "с",
    "d": "ԁ",
    "e": "е",
    "i": "іı",
    "l": "ӏ",
    "o": "оο",
    "s": "ѕ",
    "v": "ѵν",
}
_CLASSES_DE_LETRA: Final = [
    "[" + re.escape(letra) + "".join(_PARECIDOS.get(letra, "")) + "]" for letra in _NOME_DELIMITADOR
]
_NOME_COM_LACUNAS: Final = _LACUNA.join(_CLASSES_DE_LETRA)
# Qualquer abertura ou fechamento do delimitador, com lacunas invisíveis, em qualquer caixa e
# com os parecidos acima. A abertura é um `<` ou um parecido dele (o `＜` fullwidth já virou `<`
# pelo NFKC); a barra também aceita parecidos. A neutralização troca só essa abertura.
_PADRAO_DELIMITADOR: Final = re.compile(
    rf"[<‹〈〈❮˂]{_LACUNA}[/⁄∕]?{_LACUNA}{_NOME_COM_LACUNAS}",
    re.IGNORECASE,
)
# Quebras que `str.splitlines` reconhece, exceto o `\n` (que é a quebra real da observação).
_QUEBRAS_EXTRAS: Final = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85  ]")


@dataclass(frozen=True)
class Truncado:
    """Resultado de `truncar`.

    `texto` é o que deve ser exibido: começo, aviso de omissão e fim. `omitidos` conta os
    caracteres do original que não foram mantidos; zero quando o texto coube no limite.
    """

    texto: str
    truncado: bool
    omitidos: int


def aviso_omissao(omitidos: int) -> str:
    """Linha de aviso que separa o começo do fim quando parte do texto foi omitida."""
    return f"\n[... {omitidos} caracteres omitidos ...]\n"


def truncar(texto: str, limite: int) -> Truncado:
    """Mantém começo e fim de `texto` para que a parte mantida caiba em `limite` caracteres.

    Se `texto` cabe no limite, volta intacto. Caso contrário mantém 75% do limite no começo e
    o restante no fim, com o aviso de omissão no meio. O aviso não conta no limite.
    """
    if limite < 1:
        raise ValueError("o limite de truncagem precisa ser pelo menos 1")
    if len(texto) <= limite:
        return Truncado(texto=texto, truncado=False, omitidos=0)
    comeco_n = (limite * 3) // 4
    fim_n = limite - comeco_n
    omitidos = len(texto) - (comeco_n + fim_n)
    exibido = texto[:comeco_n] + aviso_omissao(omitidos) + texto[len(texto) - fim_n :]
    return Truncado(texto=exibido, truncado=True, omitidos=omitidos)


def _preparar(texto: str) -> str:
    """Normaliza o conteúdo antes de truncar e neutralizar.

    NFKC faz `＜/saida_nao_confiavel＞` (fullwidth) e letras matemáticas virarem ASCII, e
    as quebras que não são `\\n` viram escapes visíveis, para que o leitor não veja uma linha
    nova onde o host não pôs uma.
    """
    normalizado = unicodedata.normalize("NFKC", texto)
    return _QUEBRAS_EXTRAS.sub(
        lambda casamento: casamento.group(0).encode("unicode_escape").decode("ascii"),
        normalizado,
    )


def _neutralizar(texto: str) -> str:
    """Troca a abertura de cada tentativa de abrir ou fechar o delimitador por `&lt;`.

    Só o primeiro caractere do casamento muda (a abertura), e a troca é uma única passagem:
    o texto que sobra depois dela não vira `<` novo.
    """
    return _PADRAO_DELIMITADOR.sub(lambda casamento: "&lt;" + casamento.group(0)[1:], texto)


def _nome_seguro(nome: str) -> str:
    """Nome de ferramenta sem quebras de linha, sem controles e sem delimitadores.

    `unicode_escape` transforma quebras de linha e controles em sequências visíveis, então um
    nome não consegue criar uma linha nova na observação.
    """
    return _neutralizar(nome.encode("unicode_escape").decode("ascii"))


def formatar_observacao(cell_id: int, done: ExecDone, ferramentas: Mapping[str, int]) -> str:
    """Monta o texto que volta ao modelo depois de uma célula.

    Formato:
        [cell N] duração: X ms
        <saida_nao_confiavel>
        stdout (truncado em K caracteres):      (só se truncou)
        <conteúdo>
        stderr:                                  (só se não vazio)
        exceção:                                 (só se houver)
        </saida_nao_confiavel>
        ferramentas chamadas: nome × vezes       (só se houver, ordem alfabética)
        FINAL recebido.                          (só se a célula chamou FINAL)

    Sem saída nenhuma (stdout, stderr e exceção vazios), o bloco não aparece e a linha diz
    "(sem saída)". A limitação por seção vale para cada um dos três campos.
    """
    linhas = [f"[cell {cell_id}] duração: {done.duration_ms} ms"]

    secoes: list[str] = []
    campos: list[tuple[str, str, int]] = [
        ("stdout", done.stdout, LIMITE_STDOUT),
        ("stderr", done.stderr, LIMITE_STDERR),
        ("exceção", done.exception or "", LIMITE_EXCECAO),
    ]
    for rotulo, texto, limite in campos:
        if not texto:
            continue
        truncado = truncar(_preparar(texto), limite)
        if truncado.truncado:
            secoes.append(f"{rotulo} (truncado em {limite} caracteres):")
        else:
            secoes.append(f"{rotulo}:")
        secoes.append(_neutralizar(truncado.texto))

    if secoes:
        linhas.append(_ABERTURA)
        linhas.extend(secoes)
        linhas.append(_FECHAMENTO)
    else:
        linhas.append("(sem saída)")

    chamadas = [
        f"{_nome_seguro(nome)} × {vezes}"
        for nome, vezes in sorted(ferramentas.items())
        if vezes > 0
    ]
    if chamadas:
        linhas.append("ferramentas chamadas: " + ", ".join(chamadas))

    if done.final is not None:
        linhas.append("FINAL recebido.")

    return "\n".join(linhas)
