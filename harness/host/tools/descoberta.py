"""Ferramentas `descoberta.buscar` e `descoberta.descrever`: descoberta sob demanda (Tarefa 10).

O prompt não traz o catálogo completo. O modelo procura uma ferramenta pelo que ela faz
(`search_tools`, no REPL) e lê os argumentos antes de chamar (`describe_tool`). Os apelidos
do REPL chamam estas ferramentas pela ponte, então a busca passa pelo despachante normal:
validação, limite de resultado e evento `chamada_ferramenta` na trace.

A lógica mora no host e recebe o Registro na fábrica `registrar`, como o catálogo recebe a
raiz de dados. As ferramentas do grupo `descoberta` nunca aparecem nos resultados.
"""

import difflib
import re
import unicodedata
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from harness.host.perfil import Perfil
from harness.host.registry import ErroFerramenta, Ferramenta, Nivel, Registro

GRUPO: Final = "descoberta"
LIMITE_PADRAO: Final = 5
LIMITE_MINIMO: Final = 1
LIMITE_MAXIMO: Final = 20
TAMANHO_RESUMO: Final = 120
# Pesos por campo: acertar o nome vale mais que acertar a descrição ou um parâmetro.
PESO_NOME: Final = 3
PESO_DESCRICAO: Final = 2
PESO_PARAMETRO: Final = 1
SUGESTOES_MAXIMAS: Final = 3
# Limite do nome pedido que volta ao modelo em mensagens de erro (como no registro).
_TAMANHO_NOME_NA_MENSAGEM: Final = 80
_PALAVRA: Final = re.compile(r"[a-z0-9]+")


class BuscarArgs(BaseModel):
    """Argumentos de `descoberta.buscar`. A consulta precisa ter texto depois do strip."""

    model_config = ConfigDict(extra="forbid")

    query: str
    limit: StrictInt = Field(default=LIMITE_PADRAO, ge=LIMITE_MINIMO, le=LIMITE_MAXIMO)

    @field_validator("query")
    @classmethod
    def _consulta_nao_vazia(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("a consulta não pode ser vazia")
        return valor


class DescreverArgs(BaseModel):
    """Argumentos de `descoberta.descrever`: o nome completo `grupo.nome`."""

    model_config = ConfigDict(extra="forbid")

    name: str


def _palavras(texto: str) -> set[str]:
    """Palavras alfanuméricas do texto, em minúsculas e sem acento."""
    decomposto = unicodedata.normalize("NFKD", texto.lower())
    sem_acento = "".join(
        caractere for caractere in decomposto if not unicodedata.combining(caractere)
    )
    return set(_PALAVRA.findall(sem_acento))


def _visiveis(registro: Registro, perfil: Perfil) -> tuple[Ferramenta, ...]:
    """Ferramentas que a descoberta mostra ao perfil. Único ponto de filtragem.

    São as concedidas ao perfil (`Registro.visiveis`), menos as do grupo `descoberta`. `buscar`
    e `descrever` passam só por aqui, então uma ferramenta não concedida não aparece em lugar
    nenhum da descoberta.
    """
    return tuple(
        ferramenta
        for ferramenta in registro.visiveis(perfil)
        if ferramenta.nome.split(".")[0] != GRUPO
    )


def _nomes_de_parametros(ferramenta: Ferramenta) -> list[str]:
    """Nomes dos parâmetros como o modelo os escreve: o original, no caso de ferramenta MCP."""
    if ferramenta.esquema is None:
        return list(ferramenta.args.model_fields)
    propriedades = ferramenta.esquema.get("properties")
    return [str(nome) for nome in propriedades] if isinstance(propriedades, dict) else []


def _pontuacao(ferramenta: Ferramenta, termos: set[str]) -> int:
    """Soma, por termo da consulta, os pesos dos campos que contêm o termo como palavra."""
    nome = _palavras(ferramenta.nome)
    descricao = _palavras(ferramenta.descricao)
    parametros = _palavras(" ".join(_nomes_de_parametros(ferramenta)))
    pontos = 0
    for termo in termos:
        if termo in nome:
            pontos += PESO_NOME
        if termo in descricao:
            pontos += PESO_DESCRICAO
        if termo in parametros:
            pontos += PESO_PARAMETRO
    return pontos


def _resumo(descricao: str) -> str:
    """A descrição em uma linha, truncada com reticências quando passa de TAMANHO_RESUMO."""
    linha = " ".join(descricao.split())
    if len(linha) <= TAMANHO_RESUMO:
        return linha
    return linha[: TAMANHO_RESUMO - 1].rstrip() + "…"


def buscar(args: BuscarArgs, perfil: Perfil, registro: Registro) -> list[dict[str, str]]:
    """Ferramentas que casam com a consulta, da mais relevante para a menos.

    Desempate pelo nome, para a ordem ser estável. Consulta sem casamento devolve lista vazia.
    """
    termos = _palavras(args.query)
    pontuadas: list[tuple[int, Ferramenta]] = []
    for ferramenta in _visiveis(registro, perfil):
        pontos = _pontuacao(ferramenta, termos)
        if pontos > 0:
            pontuadas.append((pontos, ferramenta))
    pontuadas.sort(key=lambda item: (-item[0], item[1].nome))
    return [
        {"nome": ferramenta.nome, "resumo": _resumo(ferramenta.descricao)}
        for _, ferramenta in pontuadas[: args.limit]
    ]


def _tipo(propriedade: Any) -> str:
    """Tipo curto de um campo do JSON Schema: 'string', 'integer', 'string|null' etc.

    Esquemas de ferramentas MCP não são confiáveis na forma: o que não for dicionário ou lista
    válida cai em 'qualquer', sem levantar exceção.
    """
    if not isinstance(propriedade, dict):
        return "qualquer"
    if "anyOf" in propriedade:
        opcoes = propriedade["anyOf"]
        tipos: list[str] = []
        for opcao in opcoes if isinstance(opcoes, list) else []:
            nome = _tipo(opcao)
            if nome not in tipos:
                tipos.append(nome)
        return "|".join(tipos) or "qualquer"
    if "$ref" in propriedade:
        return "objeto"
    tipo = propriedade.get("type")
    return tipo if isinstance(tipo, str) else "qualquer"


def _parametros(modelo: type[BaseModel]) -> list[dict[str, Any]]:
    """Resumo dos argumentos de um modelo, a partir do JSON Schema dele."""
    return _parametros_de_esquema(modelo.model_json_schema())


def _parametros_de_esquema(esquema: dict[str, Any]) -> list[dict[str, Any]]:
    """Resumo dos argumentos a partir de um JSON Schema de objeto (modelo ou ferramenta MCP)."""
    propriedades = esquema.get("properties")
    requeridos = esquema.get("required")
    obrigatorios: set[str] = (
        {nome for nome in requeridos if isinstance(nome, str)}
        if isinstance(requeridos, list)
        else set()
    )
    itens: list[dict[str, Any]] = []
    for nome, propriedade in propriedades.items() if isinstance(propriedades, dict) else []:
        if not isinstance(propriedade, dict):
            propriedade = {}
        item: dict[str, Any] = {
            "nome": nome,
            "tipo": _tipo(propriedade),
            "obrigatorio": nome in obrigatorios,
        }
        if "default" in propriedade:
            item["padrao"] = propriedade["default"]
        if "description" in propriedade:
            item["descricao"] = propriedade["description"]
        itens.append(item)
    return itens


def descrever(args: DescreverArgs, perfil: Perfil, registro: Registro) -> dict[str, Any]:
    """Assinatura de uma ferramenta visível ao perfil.

    Nome inexistente ou invisível (inclusive `descoberta.*`) levanta a mesma mensagem, com
    até três nomes parecidos entre as ferramentas visíveis.
    """
    visiveis = {ferramenta.nome: ferramenta for ferramenta in _visiveis(registro, perfil)}
    ferramenta = visiveis.get(args.name)
    if ferramenta is None:
        mensagem = f"a ferramenta {args.name[:_TAMANHO_NOME_NA_MENSAGEM]!r} não existe"
        sugestoes = difflib.get_close_matches(args.name, list(visiveis), n=SUGESTOES_MAXIMAS)
        if sugestoes:
            mensagem += "; talvez você quis dizer: " + ", ".join(sugestoes)
        raise ErroFerramenta(mensagem)
    return {
        "nome": ferramenta.nome,
        "nivel": ferramenta.nivel.value,
        "descricao": " ".join(ferramenta.descricao.split()),
        # Ferramenta MCP mostra o esquema original do servidor; as do host, o modelo pydantic.
        "parametros": (
            _parametros_de_esquema(ferramenta.esquema)
            if ferramenta.esquema is not None
            else _parametros(ferramenta.args)
        ),
        "exemplo": ferramenta.exemplo,
    }


def registrar(registro: Registro) -> None:
    """Registra `descoberta.buscar` e `descoberta.descrever`, presos ao registro informado."""

    @registro.ferramenta(
        nome="descoberta.buscar",
        nivel=Nivel.LEITURA,
        descricao=(
            "Procura ferramentas pelo que fazem. Devolve, por relevância, o nome e um resumo "
            "de até `limit` ferramentas que casam com a consulta."
        ),
        args=BuscarArgs,
        exemplo='tools.descoberta.buscar(query="vendas", limit=5)',
    )
    def executar_buscar(args: BuscarArgs, perfil: Perfil) -> list[dict[str, str]]:
        return buscar(args, perfil, registro)

    @registro.ferramenta(
        nome="descoberta.descrever",
        nivel=Nivel.LEITURA,
        descricao=(
            "Mostra a assinatura de uma ferramenta: parâmetros, tipos, obrigatoriedade, padrões "
            "e um exemplo de uso."
        ),
        args=DescreverArgs,
        exemplo='tools.descoberta.descrever(name="catalogo.listar")',
    )
    def executar_descrever(args: DescreverArgs, perfil: Perfil) -> dict[str, Any]:
        return descrever(args, perfil, registro)
