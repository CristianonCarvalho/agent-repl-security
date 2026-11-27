"""Ferramentas de arquivo da variante JSON de avaliação (T18a): planilhas, documentos e agregação.

EXCEÇÃO SÓ DE AVALIAÇÃO à regra "ler arquivo é código no sandbox". Estas ferramentas rodam NO
HOST e leem os arquivos dos departamentos do perfil, porque a variante JSON não tem sandbox. O
REPL continua lendo arquivo só dentro do contêiner, e nada deste módulo entra nele.

RISCO DE SEGURANÇA (aceito para a avaliação, com os limites abaixo):
- O parsing de .xlsx, .docx e .pptx roda no host, com openpyxl, python-docx e python-pptx. Um
  arquivo malicioso poderia explorar uma falha dessas bibliotecas ou gastar memória e CPU.
- Limites rígidos: 5 MiB no arquivo; 64 MiB de conteúdo descompactado somado, conferido pelo
  índice do zip antes de abrir; planilha com no máximo 50.000 linhas de dados; leitura de
  planilha em modo somente leitura (`read_only`, `data_only`).
- Antes de abrir .xlsx, .docx ou .pptx, o prólogo de cada parte `.xml` e `.rels` é inspecionado
  (no máximo 4096 bytes por parte, até 2.000 partes): DTD ou entidade recusa o arquivo. O
  `openpyxl` também usa `defusedxml` (dependência do projeto), que proíbe entidades no parser.
- Caminho: nada de `..`, de caminho absoluto, de barra invertida nem de symlink em nenhum nível.
  O arquivo precisa estar em um departamento do perfil e a extensão precisa ser suportada.
- Arquivo que o perfil não vê, e arquivo inexistente, recebem a MESMA mensagem: o perfil não
  descobre o que existe fora dele.
- Qualquer exceção de leitura vira uma mensagem genérica. Nunca sai caminho do host nem texto cru
  da biblioteca.
- O conteúdo lido é não confiável. Ele volta ao modelo dentro do envelope (ver observation.py).

O registro desta variante é próprio: `registro_json` monta um Registro novo com `catalogo.listar`
e as ferramentas de arquivo. O registro padrão e o perfil embutido não mudam.
"""

import csv
import io
import math
import re
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import openpyxl
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from harness.host.perfil import Perfil
from harness.host.registry import ErroFerramenta, Nivel, Registro
from harness.host.tools import catalogo

VARIANTES_JSON: Final = ("json", "json-agregacao")
CONJUNTO_BASICO: Final = "basicas"
CONJUNTO_AGREGACAO: Final = "basicas+agregacao"
CONCESSOES_BASICAS: Final = frozenset({"arquivos.ler_planilha", "arquivos.ler_documento"})
CONCESSOES_AGREGACAO: Final = frozenset({"arquivos.agregar_planilha"})

TAMANHO_MAXIMO_ARQUIVO: Final = 5 * 1024 * 1024
TAMANHO_MAXIMO_DESCOMPACTADO: Final = 64 * 1024 * 1024
LINHAS_MAXIMAS: Final = 50_000
LIMITE_LINHAS_RESPOSTA: Final = 200
LIMITE_CARACTERES_DOCUMENTO: Final = 20_000
LIMITE_GRUPOS: Final = 200
TAMANHO_MAXIMO_CAMINHO: Final = 300
TAMANHO_NOME_NA_MENSAGEM: Final = 80
EXTENSOES_PLANILHA: Final = frozenset({".xlsx", ".csv"})
EXTENSOES_DOCUMENTO: Final = frozenset({".docx", ".pptx", ".csv", ".txt"})
_EXTENSOES_ZIP: Final = frozenset({".xlsx", ".docx", ".pptx"})
TAMANHO_INSPECAO_PARTE: Final = 4096
MAXIMO_PARTES_XML: Final = 2000
MENSAGEM_DTD: Final = "o arquivo contém uma declaração de DTD e não é aceito"
_ERRO_DE_LEITURA: Final = "não foi possível ler o arquivo informado"
_BOM_UTF8: Final = b"\xef\xbb\xbf"
_ESPACOS_XML: Final = frozenset(b" \t\r\n")
_INICIO_NOME_XML: Final = re.compile(rb"[A-Za-z_:]")
_INTEIRO_CSV: Final = re.compile(r"-?(?:0|[1-9][0-9]*)")
_DECIMAL_CSV: Final = re.compile(r"-?(?:0|[1-9][0-9]*)\.[0-9]+")

OperadorFiltro = Literal["==", "!=", ">", ">=", "<", "<=", "contem", "em"]
FuncaoAgregacao = Literal["soma", "contagem", "media", "maximo", "minimo"]


class LerPlanilhaArgs(BaseModel):
    """Argumentos de `arquivos.ler_planilha`: uma página de até 200 linhas."""

    model_config = ConfigDict(extra="forbid")

    arquivo: str = Field(min_length=1, max_length=TAMANHO_MAXIMO_CAMINHO)
    aba: str | None = Field(default=None, max_length=100)
    colunas: list[str] | None = Field(default=None, max_length=50)
    inicio: StrictInt = Field(default=0, ge=0)
    limite: StrictInt = Field(default=50, ge=1, le=LIMITE_LINHAS_RESPOSTA)


class LerDocumentoArgs(BaseModel):
    """Argumentos de `arquivos.ler_documento`: um trecho do texto, por caracteres."""

    model_config = ConfigDict(extra="forbid")

    arquivo: str = Field(min_length=1, max_length=TAMANHO_MAXIMO_CAMINHO)
    inicio: StrictInt = Field(default=0, ge=0)
    limite_caracteres: StrictInt = Field(default=6000, ge=1, le=LIMITE_CARACTERES_DOCUMENTO)


class FiltroArgs(BaseModel):
    """Um filtro de linhas: `coluna operador valor`. `em` recebe uma lista."""

    model_config = ConfigDict(extra="forbid")

    coluna: str = Field(min_length=1, max_length=100)
    operador: OperadorFiltro
    valor: (
        StrictStr
        | StrictInt
        | StrictFloat
        | StrictBool
        | None
        | list[StrictStr | StrictInt | StrictFloat]
    )


class AgregacaoArgs(BaseModel):
    """Uma agregação sobre uma coluna."""

    model_config = ConfigDict(extra="forbid")

    coluna: str = Field(min_length=1, max_length=100)
    funcao: FuncaoAgregacao


class AgregarPlanilhaArgs(BaseModel):
    """Argumentos de `arquivos.agregar_planilha`: filtros, agrupamento e agregações."""

    model_config = ConfigDict(extra="forbid")

    arquivo: str = Field(min_length=1, max_length=TAMANHO_MAXIMO_CAMINHO)
    aba: str | None = Field(default=None, max_length=100)
    filtros: list[FiltroArgs] = Field(default_factory=list, max_length=20)
    agrupar_por: list[str] = Field(default_factory=list, max_length=5)
    agregacoes: list[AgregacaoArgs] = Field(min_length=1, max_length=10)


@dataclass(frozen=True)
class _Tabela:
    """Conteúdo de uma aba (ou de um CSV) já lido: cabeçalho e linhas de dados normalizadas."""

    abas: tuple[str, ...]
    aba: str | None
    cabecalho: tuple[str, ...]
    linhas: tuple[tuple[Any, ...], ...]


# --- caminho e permissão ---------------------------------------------------------------------


def _bem_formado(arquivo: str) -> bool:
    """Forma do caminho relativo: sem barra invertida, NUL, barra inicial nem `.`/`..`/vazio."""
    if len(arquivo) > TAMANHO_MAXIMO_CAMINHO or "\\" in arquivo or "\x00" in arquivo:
        return False
    partes = arquivo.split("/")
    return all(parte not in ("", ".", "..") for parte in partes)


def _arquivo_do_perfil(raiz: Path, perfil: Perfil, arquivo: str, extensoes: frozenset[str]) -> Path:
    """Caminho real do arquivo, se o perfil pode lê-lo. Levanta ErroFerramenta caso contrário.

    Inexistente e negado recebem a mesma mensagem. A extensão só é conferida depois de o perfil
    ver o arquivo, para que a mensagem de extensão não revele nada de fora do perfil.
    """
    mensagem = (
        f"o arquivo {arquivo[:TAMANHO_NOME_NA_MENSAGEM]!r} não existe ou não está disponível "
        "para o perfil"
    )
    partes = arquivo.split("/")
    if not _bem_formado(arquivo) or len(partes) < 2 or partes[0] not in perfil.departamentos:
        raise ErroFerramenta(mensagem)
    atual = raiz
    for parte in partes:
        atual = atual / parte
        if atual.is_symlink():
            raise ErroFerramenta(mensagem)
    if not atual.is_file():
        raise ErroFerramenta(mensagem)
    try:
        atual.resolve(strict=True).relative_to(raiz.resolve(strict=True))
    except (OSError, ValueError):
        raise ErroFerramenta(mensagem) from None
    if atual.suffix.lower() not in extensoes:
        raise ErroFerramenta(f"extensão não suportada para esta leitura: {atual.suffix.lower()!r}")
    return atual


def _declara_dtd(inicio: bytes) -> bool:
    """Se o prólogo do XML declara DTD ou entidade. `inicio` são os primeiros bytes da parte.

    DTD e entidade só podem aparecer antes do elemento raiz. A leitura para no primeiro elemento,
    então texto de célula ou de parágrafo (depois da raiz) nunca conta como declaração. Comentários
    e instruções de processamento do prólogo são pulados. Prólogo que não se fecha dentro da janela,
    ou que não é prólogo bem formado, é recusado: não dá para afirmar que não há DTD.
    """
    if not inicio:
        return False
    pos = len(_BOM_UTF8) if inicio.startswith(_BOM_UTF8) else 0
    while True:
        while pos < len(inicio) and inicio[pos] in _ESPACOS_XML:
            pos += 1
        if pos >= len(inicio):
            raise ErroFerramenta(_ERRO_DE_LEITURA)
        if inicio.startswith(b"<?", pos):
            fim = inicio.find(b"?>", pos + 2)
            if fim < 0:
                raise ErroFerramenta(_ERRO_DE_LEITURA)
            pos = fim + 2
            continue
        if inicio.startswith(b"<!--", pos):
            fim = inicio.find(b"-->", pos + 4)
            if fim < 0:
                raise ErroFerramenta(_ERRO_DE_LEITURA)
            pos = fim + 3
            continue
        if inicio[pos : pos + 9].upper().startswith((b"<!DOCTYPE", b"<!ENTITY")):
            return True
        if inicio[pos : pos + 1] == b"<" and _INICIO_NOME_XML.match(inicio, pos + 1):
            return False
        raise ErroFerramenta(_ERRO_DE_LEITURA)


def _verificar_dtd(pacote: zipfile.ZipFile, itens: Sequence[zipfile.ZipInfo]) -> None:
    """Recusa o pacote se alguma parte `.xml` ou `.rels` declarar DTD ou entidade.

    Só o começo de cada parte é lido (`TAMANHO_INSPECAO_PARTE` bytes), sem descompactar o
    restante. Pacote com mais de `MAXIMO_PARTES_XML` partes XML é recusado, para que a inspeção
    não vire um vetor de custo e para que nenhuma parte fique sem conferir.
    """
    partes = [item for item in itens if item.filename.lower().endswith((".xml", ".rels"))]
    if len(partes) > MAXIMO_PARTES_XML:
        raise ErroFerramenta(_ERRO_DE_LEITURA)
    for item in partes:
        with pacote.open(item) as parte:
            inicio = parte.read(TAMANHO_INSPECAO_PARTE)
        if _declara_dtd(inicio):
            raise ErroFerramenta(MENSAGEM_DTD)


def _verificar_pacote(caminho: Path) -> None:
    """Zip de OOXML: tamanho descompactado pelo índice e, antes de qualquer parser, as DTDs."""
    try:
        with zipfile.ZipFile(caminho) as pacote:
            itens = pacote.infolist()
            descompactado = sum(item.file_size for item in itens)
            if descompactado > TAMANHO_MAXIMO_DESCOMPACTADO:
                raise ErroFerramenta(
                    "o conteúdo descompactado do arquivo passa do limite de 64 MiB"
                )
            _verificar_dtd(pacote, itens)
    except ErroFerramenta:
        raise
    except Exception:
        raise ErroFerramenta(_ERRO_DE_LEITURA) from None


def _verificar_tamanho(caminho: Path) -> None:
    """Limites de tamanho e, nos formatos zip, a verificação do pacote (`_verificar_pacote`)."""
    try:
        tamanho = caminho.stat().st_size
    except OSError:
        raise ErroFerramenta(_ERRO_DE_LEITURA) from None
    if tamanho > TAMANHO_MAXIMO_ARQUIVO:
        raise ErroFerramenta("o arquivo passa do limite de 5 MiB")
    if caminho.suffix.lower() in _EXTENSOES_ZIP:
        _verificar_pacote(caminho)


# --- leitura de planilhas ---------------------------------------------------------------------


def _normalizar_valor(valor: Any) -> Any:
    """Valor de célula serializável: datas em ISO, decimais como número, resto como texto."""
    if valor is None or isinstance(valor, bool | int | str):
        return valor
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, datetime | date | time):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return float(valor)
    return str(valor)


def _valor_csv(texto: str) -> Any:
    """Célula de CSV: vazia vira None; número inteiro ou decimal (com ponto) vira número."""
    if texto == "":
        return None
    if _INTEIRO_CSV.fullmatch(texto):
        return int(texto)
    if _DECIMAL_CSV.fullmatch(texto):
        return float(texto)
    return texto


def _cabecalho(brutos: Sequence[Any]) -> tuple[str, ...]:
    """Nomes das colunas. Coluna sem nome vira `coluna_N`. Nome repetido é recusado."""
    nomes = [
        str(valor).strip() if valor is not None and str(valor).strip() else f"coluna_{indice}"
        for indice, valor in enumerate(brutos, start=1)
    ]
    if len(set(nomes)) != len(nomes):
        raise ErroFerramenta("o cabeçalho da aba tem nomes de coluna repetidos")
    return tuple(nomes)


def _linhas(
    brutas: Iterable[Sequence[Any]], largura: int, normalizar: Callable[[Any], Any]
) -> tuple[tuple[Any, ...], ...]:
    """Linhas de dados normalizadas, sem as que ficam totalmente vazias. Passar de 50.000 é erro.

    A linha vazia é decidida depois de normalizar, para que xlsx e CSV tenham a mesma regra
    (uma célula de CSV vazia já é None).
    """
    linhas: list[tuple[Any, ...]] = []
    for bruta in brutas:
        linha = tuple(
            normalizar(bruta[indice]) if indice < len(bruta) else None for indice in range(largura)
        )
        if all(valor is None for valor in linha):
            continue
        if len(linhas) >= LINHAS_MAXIMAS:
            raise ErroFerramenta("a planilha tem mais de 50.000 linhas de dados")
        linhas.append(linha)
    return tuple(linhas)


def _ler_xlsx(caminho: Path, aba_pedida: str | None) -> _Tabela:
    livro = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    try:
        abas = tuple(str(nome) for nome in livro.sheetnames)
        if not abas:
            raise ErroFerramenta("a planilha não tem abas")
        nome = abas[0] if aba_pedida is None else aba_pedida
        if nome not in abas:
            raise ErroFerramenta(f"a aba {nome[:TAMANHO_NOME_NA_MENSAGEM]!r} não existe no arquivo")
        brutas = livro[nome].iter_rows(values_only=True)
        cabecalho = _cabecalho(next(brutas, ()))
        linhas = _linhas(brutas, len(cabecalho), _normalizar_valor)
    finally:
        livro.close()
    return _Tabela(abas=abas, aba=nome, cabecalho=cabecalho, linhas=linhas)


def _ler_csv_tabela(caminho: Path, aba_pedida: str | None) -> _Tabela:
    if aba_pedida is not None:
        raise ErroFerramenta("o CSV não tem abas: não informe 'aba'")
    texto = caminho.read_bytes().decode("utf-8-sig")
    brutas = csv.reader(io.StringIO(texto))
    cabecalho = _cabecalho(next(brutas, []))
    linhas = _linhas(brutas, len(cabecalho), _valor_csv_bruto)
    return _Tabela(abas=(), aba=None, cabecalho=cabecalho, linhas=linhas)


def _valor_csv_bruto(valor: Any) -> Any:
    """Célula do csv.reader (sempre texto) convertida como número quando é número."""
    return _valor_csv(valor) if isinstance(valor, str) else valor


def _carregar_tabela(caminho: Path, aba: str | None) -> _Tabela:
    """Lê a aba (ou o CSV). Toda falha de parsing vira a mensagem genérica."""
    try:
        if caminho.suffix.lower() == ".xlsx":
            return _ler_xlsx(caminho, aba)
        return _ler_csv_tabela(caminho, aba)
    except ErroFerramenta:
        raise
    except Exception:
        raise ErroFerramenta(_ERRO_DE_LEITURA) from None


def _indices_das_colunas(cabecalho: tuple[str, ...], colunas: Sequence[str]) -> list[int]:
    """Posição de cada coluna pedida, na ordem pedida. Coluna desconhecida ou repetida é erro."""
    if len(set(colunas)) != len(colunas):
        raise ErroFerramenta("a lista de colunas tem nomes repetidos")
    indices: list[int] = []
    for nome in colunas:
        if nome not in cabecalho:
            raise ErroFerramenta(f"coluna inexistente: {nome[:TAMANHO_NOME_NA_MENSAGEM]!r}")
        indices.append(cabecalho.index(nome))
    return indices


# --- leitura de documentos ---------------------------------------------------------------------


def _texto_docx(caminho: Path) -> str:
    """Parágrafos e tabelas na ordem do documento. Cada linha de tabela vira `a | b | c`."""
    documento = Document(str(caminho))
    partes: list[str] = []
    for filho in documento.element.body.iterchildren():
        etiqueta = str(filho.tag).rsplit("}", 1)[-1]
        if etiqueta == "p":
            partes.append(Paragraph(filho, documento).text)
        elif etiqueta == "tbl":
            tabela = Table(filho, documento)
            for linha in tabela.rows:
                partes.append(" | ".join(celula.text for celula in linha.cells))
    return "\n".join(partes)


def _textos_da_forma(forma: Any) -> list[str]:
    """Textos de uma forma do slide: texto, tabela e, dentro de grupo, as formas do grupo."""
    try:
        eh_grupo = forma.shape_type == MSO_SHAPE_TYPE.GROUP
    except NotImplementedError:
        eh_grupo = False
    if eh_grupo:
        textos: list[str] = []
        for interna in forma.shapes:
            textos.extend(_textos_da_forma(interna))
        return textos
    textos = []
    if forma.has_text_frame:
        textos.extend(p.text for p in forma.text_frame.paragraphs if p.text.strip())
    if getattr(forma, "has_table", False):
        textos.extend(" | ".join(c.text for c in linha.cells) for linha in forma.table.rows)
    return textos


def _texto_pptx(caminho: Path) -> str:
    """Texto de cada slide, marcado por número, com as notas do apresentador logo depois."""
    apresentacao = Presentation(str(caminho))
    partes: list[str] = []
    for numero, slide in enumerate(apresentacao.slides, start=1):
        partes.append(f"--- Slide {numero} ---")
        for forma in slide.shapes:
            partes.extend(_textos_da_forma(forma))
        if slide.has_notes_slide:
            quadro = slide.notes_slide.notes_text_frame
            if quadro is not None and quadro.text.strip():
                partes.append(f"Notas do slide {numero}:")
                partes.append(quadro.text)
    return "\n".join(partes)


def _texto_do_documento(caminho: Path) -> str:
    """Texto do documento. Qualquer falha de parsing vira a mensagem genérica."""
    try:
        sufixo = caminho.suffix.lower()
        if sufixo == ".docx":
            return _texto_docx(caminho)
        if sufixo == ".pptx":
            return _texto_pptx(caminho)
        return caminho.read_bytes().decode("utf-8-sig")
    except ErroFerramenta:
        raise
    except Exception:
        raise ErroFerramenta(_ERRO_DE_LEITURA) from None


# --- filtros e agregação -----------------------------------------------------------------------


def _numero(valor: Any) -> int | float | None:
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return None
    return valor


def _iguais(valor: Any, referencia: Any) -> bool:
    """Igualdade estrita por tipo, com números comparados como números. Bool nunca é número."""
    if isinstance(valor, bool) or isinstance(referencia, bool):
        return type(valor) is type(referencia) and valor == referencia
    numero_a, numero_b = _numero(valor), _numero(referencia)
    if numero_a is not None and numero_b is not None:
        return float(numero_a) == float(numero_b)
    return bool(valor == referencia)


def _sinal(a: Any, b: Any) -> int:
    return -1 if a < b else (1 if a > b else 0)


def _comparar(valor: Any, referencia: Any) -> int | None:
    """-1, 0 ou 1 para números ou textos do mesmo tipo. None quando não dá para ordenar."""
    numero_a, numero_b = _numero(valor), _numero(referencia)
    if numero_a is not None and numero_b is not None:
        return _sinal(float(numero_a), float(numero_b))
    if isinstance(valor, str) and isinstance(referencia, str):
        return _sinal(valor, referencia)
    return None


def _casa(valor: Any, filtro: FiltroArgs) -> bool:
    """Se o valor da célula passa no filtro. Operador e tipo errados viram ErroFerramenta."""
    operador, referencia = filtro.operador, filtro.valor
    if operador == "em":
        if not isinstance(referencia, list):
            raise ErroFerramenta("o operador 'em' pede uma lista de valores")
        return any(_iguais(valor, item) for item in referencia)
    if operador == "contem":
        if not isinstance(referencia, str):
            raise ErroFerramenta("o operador 'contem' pede um texto")
        return valor is not None and referencia.casefold() in str(valor).casefold()
    if isinstance(referencia, list):
        raise ErroFerramenta(f"o operador {operador!r} pede um valor, não uma lista")
    if operador == "==":
        return _iguais(valor, referencia)
    if operador == "!=":
        return not _iguais(valor, referencia)
    ordem = _comparar(valor, referencia)
    if ordem is None:
        return False
    return {
        ">": ordem > 0,
        ">=": ordem >= 0,
        "<": ordem < 0,
        "<=": ordem <= 0,
    }[operador]


def _agregar(funcao: FuncaoAgregacao, coluna: str, valores: Sequence[Any]) -> Any:
    """Resultado de uma agregação sobre os valores de um grupo. Vazio: contagem 0, resto None."""
    if funcao == "contagem":
        return sum(1 for valor in valores if valor is not None)
    numeros: list[int | float] = []
    for valor in valores:
        if valor is None:
            continue
        numero = _numero(valor)
        if numero is None:
            raise ErroFerramenta(
                f"a coluna {coluna[:TAMANHO_NOME_NA_MENSAGEM]!r} tem valores não numéricos; "
                f"{funcao} só vale para números"
            )
        numeros.append(numero)
    if funcao == "soma":
        return sum(numeros)
    if funcao == "media":
        return sum(numeros) / len(numeros) if numeros else None
    if funcao == "maximo":
        return max(numeros) if numeros else None
    return min(numeros) if numeros else None


# --- ferramentas -------------------------------------------------------------------------------


def ler_planilha(args: LerPlanilhaArgs, perfil: Perfil, raiz: Path) -> dict[str, Any]:
    """Uma página de linhas de uma aba (ou de um CSV), com as colunas pedidas."""
    caminho = _arquivo_do_perfil(raiz, perfil, args.arquivo, EXTENSOES_PLANILHA)
    _verificar_tamanho(caminho)
    tabela = _carregar_tabela(caminho, args.aba)
    indices = (
        list(range(len(tabela.cabecalho)))
        if args.colunas is None
        else _indices_das_colunas(tabela.cabecalho, args.colunas)
    )
    nomes = [tabela.cabecalho[indice] for indice in indices]
    total = len(tabela.linhas)
    fatia = tabela.linhas[args.inicio : args.inicio + args.limite]
    return {
        "arquivo": args.arquivo,
        "aba": tabela.aba,
        "abas": list(tabela.abas),
        "colunas": list(tabela.cabecalho),
        "total_linhas": total,
        "inicio": args.inicio,
        "linhas": [
            {nome: linha[indice] for nome, indice in zip(nomes, indices, strict=True)}
            for linha in fatia
        ],
        "truncado": args.inicio + args.limite < total,
    }


def ler_documento(args: LerDocumentoArgs, perfil: Perfil, raiz: Path) -> dict[str, Any]:
    """Um trecho do texto do documento, por caracteres."""
    caminho = _arquivo_do_perfil(raiz, perfil, args.arquivo, EXTENSOES_DOCUMENTO)
    _verificar_tamanho(caminho)
    texto = _texto_do_documento(caminho)
    fim = args.inicio + args.limite_caracteres
    return {
        "arquivo": args.arquivo,
        "total_caracteres": len(texto),
        "inicio": args.inicio,
        "texto": texto[args.inicio : fim],
        "truncado": fim < len(texto),
    }


def agregar_planilha(args: AgregarPlanilhaArgs, perfil: Perfil, raiz: Path) -> dict[str, Any]:
    """Filtra, agrupa e agrega as linhas de uma aba. Até 200 grupos; o resto vem com aviso."""
    caminho = _arquivo_do_perfil(raiz, perfil, args.arquivo, EXTENSOES_PLANILHA)
    _verificar_tamanho(caminho)
    tabela = _carregar_tabela(caminho, args.aba)
    indices = {nome: posicao for posicao, nome in enumerate(tabela.cabecalho)}
    usadas = (
        [f.coluna for f in args.filtros] + args.agrupar_por + [a.coluna for a in args.agregacoes]
    )
    for nome in usadas:
        if nome not in indices:
            raise ErroFerramenta(f"coluna inexistente: {nome[:TAMANHO_NOME_NA_MENSAGEM]!r}")
    pares = [(a.funcao, a.coluna) for a in args.agregacoes]
    if len(set(pares)) != len(pares):
        raise ErroFerramenta("agregação repetida: a mesma função sobre a mesma coluna")

    considerados = [
        linha
        for linha in tabela.linhas
        if all(_casa(linha[indices[f.coluna]], f) for f in args.filtros)
    ]
    grupos: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
    if not args.agrupar_por:
        grupos[()] = considerados
    else:
        for linha in considerados:
            chave = tuple(linha[indices[coluna]] for coluna in args.agrupar_por)
            grupos.setdefault(chave, []).append(linha)

    ordenados = sorted(grupos.items(), key=lambda item: tuple(str(v) for v in item[0]))
    saida: list[dict[str, Any]] = []
    for chave, linhas in ordenados[:LIMITE_GRUPOS]:
        resultado: dict[str, Any] = dict(zip(args.agrupar_por, chave, strict=True))
        for agregacao in args.agregacoes:
            valores = [linha[indices[agregacao.coluna]] for linha in linhas]
            chave_resultado = f"{agregacao.funcao}({agregacao.coluna})"
            resultado[chave_resultado] = _agregar(agregacao.funcao, agregacao.coluna, valores)
        saida.append(resultado)
    return {
        "arquivo": args.arquivo,
        "aba": tabela.aba,
        "linhas_consideradas": len(considerados),
        "grupos": saida,
        "grupos_truncados": len(ordenados) > LIMITE_GRUPOS,
    }


def registrar_arquivos(registro: Registro, raiz: Path, *, agregacao: bool) -> None:
    """Registra as ferramentas de arquivo no registro informado. A agregação é opcional."""

    @registro.ferramenta(
        nome="arquivos.ler_planilha",
        nivel=Nivel.LEITURA,
        descricao=(
            "Lê linhas de uma planilha (.xlsx) ou CSV de um departamento do perfil, com as colunas "
            "pedidas e paginação de até 200 linhas."
        ),
        args=LerPlanilhaArgs,
        dado_privado=True,
        conteudo_nao_confiavel=True,
        exemplo='tools.arquivos.ler_planilha(arquivo="vendas/pedidos_2026-10.xlsx", limite=20)',
    )
    def executar_ler_planilha(args: LerPlanilhaArgs, perfil: Perfil) -> dict[str, Any]:
        return ler_planilha(args, perfil, raiz)

    @registro.ferramenta(
        nome="arquivos.ler_documento",
        nivel=Nivel.LEITURA,
        descricao=(
            "Lê o texto de um documento (.docx, .pptx, .csv ou .txt) de um departamento do perfil, "
            "em um trecho de até 20.000 caracteres."
        ),
        args=LerDocumentoArgs,
        dado_privado=True,
        conteudo_nao_confiavel=True,
        exemplo='tools.arquivos.ler_documento(arquivo="direcao/metas_2026.docx")',
    )
    def executar_ler_documento(args: LerDocumentoArgs, perfil: Perfil) -> dict[str, Any]:
        return ler_documento(args, perfil, raiz)

    if not agregacao:
        return

    @registro.ferramenta(
        nome="arquivos.agregar_planilha",
        nivel=Nivel.LEITURA,
        descricao=(
            "Filtra, agrupa e agrega as linhas de uma planilha (.xlsx ou CSV) do perfil: soma, "
            "contagem, média, máximo e mínimo, com até 200 grupos."
        ),
        args=AgregarPlanilhaArgs,
        dado_privado=True,
        conteudo_nao_confiavel=True,
        exemplo=(
            'tools.arquivos.agregar_planilha(arquivo="vendas/pedidos_2026-10.xlsx", '
            'agregacoes=[{"coluna": "valor", "funcao": "soma"}])'
        ),
    )
    def executar_agregar_planilha(args: AgregarPlanilhaArgs, perfil: Perfil) -> dict[str, Any]:
        return agregar_planilha(args, perfil, raiz)


def registro_json(raiz: Path, variante: str, *, politica: bool = False) -> Registro:
    """Registro próprio de uma variante JSON: `catalogo.listar` e as ferramentas de arquivo.

    `variante` é `json` (básicas) ou `json-agregacao` (básicas mais agregação). Outro valor
    levanta ValueError. A descoberta do REPL não entra aqui.
    """
    if variante not in VARIANTES_JSON:
        raise ValueError(f"variante JSON desconhecida: {variante!r}")
    registro = Registro(politica=politica)
    catalogo.registrar(registro, raiz)
    registrar_arquivos(registro, raiz, agregacao=variante == "json-agregacao")
    return registro


def perfil_json(perfil: Perfil, variante: str) -> Perfil:
    """Cópia do perfil com as concessões de arquivo da variante. O perfil embutido não muda."""
    if variante not in VARIANTES_JSON:
        raise ValueError(f"variante JSON desconhecida: {variante!r}")
    extra = CONCESSOES_BASICAS | (
        CONCESSOES_AGREGACAO if variante == "json-agregacao" else frozenset()
    )
    return perfil.com_concessoes(extra)


def nome_do_conjunto(variante: str) -> str:
    """Nome do conjunto de ferramentas da variante, para o relatório."""
    return CONJUNTO_AGREGACAO if variante == "json-agregacao" else CONJUNTO_BASICO
