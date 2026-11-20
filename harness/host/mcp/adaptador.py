"""Adaptador MCP → Registro (Tarefa 12): cada ferramenta vira `servidor.ferramenta`.

`registrar_servidores_mcp` inicia cada servidor da configuração, lista as ferramentas e registra
cada uma no Registro. O sandbox as chama pela ponte normal (`tools.servidor.ferramenta(...)`), e
o despachante do Registro valida os argumentos antes de qualquer chamada ao servidor.

Decisões deste módulo (não estão na spec):
- Nome válido é o do padrão MCP (`^[a-z][a-z0-9_]*$`) e também o do Registro, que aceita só
  letras e `_` (o teste de `grupo.1x` é de test_registro). Nomes com dígito são ignorados e
  contados em `ignoradas`; um servidor com dígito no nome nem é iniciado (vai para `falhas`).
- Argumentos: um modelo pydantic gerado do `inputSchema`, com `extra="forbid"`. Um campo cujo
  nome não serve como atributo de modelo (palavra reservada, `_` inicial, colisão com o
  BaseModel) recebe um nome interno e o nome original vira `alias`. Na chamada, só os argumentos
  que o modelo recebeu seguem para o servidor (`exclude_unset`), para os padrões do servidor
  valerem. Restrições além do tipo (enum, mínimo, padrão) ficam com o servidor.
- Falha de um servidor (não inicia, não responde, protocolo inválido) é registrada em `falhas`
  e não impede os outros servidores nem o pedido.
- O conteúdo do servidor é não confiável: `conteudo_nao_confiavel` e `canal_externo` são
  verdadeiros para todas as ferramentas MCP, e a descrição passa pela neutralização de tags.
"""

import keyword
import re
from collections.abc import Callable
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, create_model

from harness.host.mcp.cliente import ClienteMcp, DescritorFerramenta, ErroMcp
from harness.host.mcp.config import GRUPOS_RESERVADOS, ConfigMcp, ErroConfigMcp, ServidorMcp
from harness.host.perfil import Perfil
from harness.host.registry import ErroFerramenta, Nivel, Registro
from harness.orchestrator.observation import neutralizar_descricao

LIMITE_DESCRICAO: Final = 500
LIMITE_MENSAGEM_DE_ERRO: Final = 500
LIMITE_TIPO_DE_CONTEUDO: Final = 40
LIMITE_NOME_NA_MENSAGEM: Final = 80
_NOME_MCP: Final = re.compile(r"[a-z][a-z0-9_]*")
_NOME_REGISTRO: Final = re.compile(r"[a-z_]+")
_TIPOS_JSON: Final[dict[str, Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list[Any],
    "object": dict[str, Any],
}
# Atributos do BaseModel: um campo com esse nome sombrearia o próprio modelo.
_ATRIBUTOS_DO_MODELO: Final = frozenset(dir(BaseModel))


class GerenteMcp:
    """Servidores MCP iniciados numa execução. Use como context manager ou chame `fechar()`."""

    def __init__(self) -> None:
        self._clientes: dict[str, ClienteMcp] = {}
        # Ferramentas registradas, como `servidor.ferramenta`.
        self.ferramentas: list[str] = []
        # Ferramentas do servidor que o Registro não aceitou (nome fora do padrão ou repetido).
        self.ignoradas: list[str] = []
        # Servidor que não foi iniciado, com o motivo curto.
        self.falhas: dict[str, str] = {}

    def adicionar(self, nome: str, cliente: ClienteMcp) -> None:
        self._clientes[nome] = cliente

    def fechar(self) -> None:
        """Fecha todos os servidores. Nunca levanta exceção."""
        for cliente in self._clientes.values():
            cliente.fechar()
        self._clientes.clear()

    def __enter__(self) -> "GerenteMcp":
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()


def verificar_nomes_mcp(registro: Registro, config: ConfigMcp) -> None:
    """Recusa servidores cujo nome já é grupo de ferramentas no registro. Levanta ErroConfigMcp."""
    grupos = {ferramenta.nome.split(".")[0] for ferramenta in registro.listar()}
    for nome in config.servidores:
        if nome in grupos or nome in GRUPOS_RESERVADOS:
            raise ErroConfigMcp(f"o servidor MCP '{nome}' usa o nome de um grupo que já existe")


def registrar_servidores_mcp(registro: Registro, config: ConfigMcp) -> GerenteMcp:
    """Inicia os servidores da configuração e registra as ferramentas no registro.

    Erros de configuração (nome de grupo repetido) levantam antes de qualquer processo subir.
    Falha de um servidor vira aviso no gerente e não derruba a montagem. Se algo inesperado
    acontecer no meio, os servidores já iniciados são fechados antes de a exceção sair.
    """
    verificar_nomes_mcp(registro, config)
    gerente = GerenteMcp()
    try:
        for nome in sorted(config.servidores):
            _registrar_servidor(registro, config.servidores[nome], gerente)
    except BaseException:
        gerente.fechar()
        raise
    return gerente


def converter_resultado(resultado: dict[str, Any], servidor: str, ferramenta: str) -> Any:
    """Transforma o `result` de `tools/call` no valor da ferramenta, ou levanta ErroFerramenta.

    `isError` verdadeiro vira erro com o texto truncado. Senão, `structuredContent` (objeto ou
    lista) vem na frente; sem ele, o texto dos blocos `text` é concatenado.
    """
    texto = _renderizar_conteudo(resultado.get("content"))
    if resultado.get("isError") is True:
        mensagem = texto.strip() or f"a ferramenta '{servidor}.{ferramenta}' reportou erro"
        raise ErroFerramenta(_limitar(mensagem, LIMITE_MENSAGEM_DE_ERRO))
    estruturado = resultado.get("structuredContent")
    if isinstance(estruturado, dict | list):
        return estruturado
    return texto


def criar_modelo_argumentos(esquema: dict[str, Any] | None) -> type[BaseModel]:
    """Modelo pydantic com os argumentos de uma ferramenta, a partir do `inputSchema`.

    Propriedades obrigatórias não têm padrão. As demais têm o `default` do schema, ou None. Sem
    schema utilizável, o modelo não tem campo nenhum, e qualquer argumento é recusado.
    """
    propriedades = esquema.get("properties") if esquema is not None else None
    requeridos = esquema.get("required") if esquema is not None else None
    if not isinstance(propriedades, dict):
        propriedades = {}
    obrigatorios = (
        {campo for campo in requeridos if isinstance(campo, str)}
        if isinstance(requeridos, list)
        else set()
    )
    originais = {str(campo) for campo in propriedades}
    campos: dict[str, Any] = {}
    for indice, (original, propriedade) in enumerate(propriedades.items()):
        if not isinstance(propriedade, dict):
            propriedade = {}
        campo = _nome_do_campo(str(original), indice, set(campos), originais)
        tipo: Any = _tipo_do_campo(propriedade.get("type"))
        if original in obrigatorios:
            campos[campo] = (tipo, Field(alias=str(original)))
        else:
            padrao = propriedade.get("default")
            campos[campo] = (tipo | None, Field(default=padrao, alias=str(original)))
    return create_model("ArgumentosMcp", __config__=ConfigDict(extra="forbid"), **campos)


# --- internos -----------------------------------------------------------------------------


def _registrar_servidor(registro: Registro, servidor: ServidorMcp, gerente: GerenteMcp) -> None:
    nome = servidor.nome
    if not _NOME_REGISTRO.fullmatch(nome):
        gerente.falhas[nome] = "o nome tem dígitos, que o registro de ferramentas não aceita"
        return
    try:
        cliente = ClienteMcp(servidor)
    except ErroMcp as erro:
        gerente.falhas[nome] = str(erro)
        return
    except Exception:
        gerente.falhas[nome] = f"falha inesperada ao iniciar o servidor MCP '{nome}'"
        return
    try:
        descritores = cliente.listar_ferramentas()
    except ErroMcp as erro:
        cliente.fechar()
        gerente.falhas[nome] = str(erro)
        return
    except Exception:
        cliente.fechar()
        gerente.falhas[nome] = f"falha inesperada ao listar as ferramentas de '{nome}'"
        return
    gerente.adicionar(nome, cliente)
    for descritor in descritores:
        _registrar_ferramenta(registro, servidor, cliente, descritor, gerente)


def _registrar_ferramenta(
    registro: Registro,
    servidor: ServidorMcp,
    cliente: ClienteMcp,
    descritor: DescritorFerramenta,
    gerente: GerenteMcp,
) -> None:
    ferramenta = descritor.nome
    completo = f"{servidor.nome}.{ferramenta}"
    if not (_NOME_MCP.fullmatch(ferramenta) and _NOME_REGISTRO.fullmatch(ferramenta)):
        gerente.ignoradas.append(f"{servidor.nome}.{ferramenta[:LIMITE_NOME_NA_MENSAGEM]}")
        return
    nivel = Nivel.LEITURA if ferramenta in servidor.leitura else Nivel.ESCRITA_EXTERNA
    descricao = neutralizar_descricao(
        descritor.descricao or f"Ferramenta do servidor MCP '{servidor.nome}'.",
        LIMITE_DESCRICAO,
    )
    try:
        modelo = criar_modelo_argumentos(descritor.esquema)
        registro.ferramenta(
            nome=completo,
            nivel=nivel,
            descricao=descricao,
            args=modelo,
            conteudo_nao_confiavel=True,
            canal_externo=True,
            esquema=descritor.esquema,
        )(_funcao_mcp(cliente, servidor.nome, ferramenta))
    except Exception:
        # Ferramenta que não pode ser registrada (nome repetido, esquema que o pydantic recusa)
        # é ignorada, e as demais do servidor seguem. A causa não sai daqui: pode trazer texto
        # do servidor.
        gerente.ignoradas.append(completo[:LIMITE_NOME_NA_MENSAGEM])
        return
    gerente.ferramentas.append(completo)


def _funcao_mcp(
    cliente: ClienteMcp, servidor: str, ferramenta: str
) -> Callable[[BaseModel, Perfil], Any]:
    def executar(args: BaseModel, perfil: Perfil) -> Any:
        enviados = args.model_dump(by_alias=True, exclude_unset=True)
        try:
            resultado = cliente.chamar(ferramenta, enviados)
        except ErroMcp as erro:
            raise ErroFerramenta(str(erro)) from None
        return converter_resultado(resultado, servidor, ferramenta)

    return executar


def _renderizar_conteudo(conteudo: Any) -> str:
    """Texto dos blocos `text`; blocos de outro tipo viram um marcador, na mesma ordem."""
    if not isinstance(conteudo, list):
        return ""
    partes: list[str] = []
    for bloco in conteudo:
        if (
            isinstance(bloco, dict)
            and bloco.get("type") == "text"
            and isinstance(bloco.get("text"), str)
        ):
            partes.append(bloco["text"])
            continue
        tipo = bloco.get("type") if isinstance(bloco, dict) else None
        if not isinstance(tipo, str):
            tipo = "desconhecido"
        partes.append(f"[conteúdo do tipo {_limitar(tipo, LIMITE_TIPO_DE_CONTEUDO)} omitido]")
    return "\n".join(partes)


def _tipo_do_campo(tipo_json: Any) -> Any:
    if isinstance(tipo_json, str):
        return _TIPOS_JSON.get(tipo_json, Any)
    return Any


def _nome_do_campo(original: str, indice: int, escolhidos: set[str], originais: set[str]) -> str:
    """Nome de campo do modelo: o original quando serve, senão `campo_<indice>` sem colisão."""
    servivel = (
        original.isidentifier()
        and not keyword.iskeyword(original)
        and not original.startswith("_")
        and not original.startswith("model_")
        and original not in _ATRIBUTOS_DO_MODELO
    )
    if servivel and original not in escolhidos:
        return original
    candidato = f"campo_{indice}"
    while candidato in escolhidos or candidato in originais:
        candidato += "_"
    return candidato


def _limitar(texto: str, limite: int) -> str:
    if len(texto) <= limite:
        return texto
    return texto[: limite - 1].rstrip() + "…"
