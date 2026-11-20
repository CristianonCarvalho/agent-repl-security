"""Configuração dos servidores MCP (Tarefa 12): leitura e validação do arquivo JSON.

O arquivo tem um objeto `servidores` que mapeia o nome de cada servidor (ele vira o grupo de
ferramentas, como `eco` em `tools.eco.eco(...)`) para a sua configuração:

    {"servidores": {"eco": {"comando": "uv", "args": ["run", "servidor.py"],
                            "env": {"EXEMPLO": "1"}, "leitura": ["eco"]}}}

O campo `leitura` lista as ferramentas tratadas como LEITURA; as demais são ESCRITA_EXTERNA.
O `env` é a única forma de passar variáveis ao servidor: o ambiente do harness nunca é herdado.

Decisões deste módulo (não estão na spec):
- O nome do servidor é a chave do objeto, e não um campo da entrada. Uma entrada que repita
  `nome` é recusada, para não haver dois nomes para o mesmo servidor.
- `catalogo` e `descoberta` são grupos do próprio harness e não podem ser nomes de servidor.
- As mensagens de erro citam o nome do campo e o motivo, nunca o valor: `env` pode trazer
  segredos, e o pydantic repete o valor errado na mensagem.
"""

import json
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Nome de servidor: começa com letra minúscula. O Registro aceita só letras (ver adaptador.py).
PADRAO_NOME_SERVIDOR: Final = r"^[a-z][a-z0-9_]*$"
GRUPOS_RESERVADOS: Final = frozenset({"catalogo", "descoberta"})
TIMEOUT_CHAMADA_PADRAO_S: Final = 30.0
TIMEOUT_INICIO_PADRAO_S: Final = 15.0


class ErroConfigMcp(Exception):
    """Configuração MCP ausente, ilegível ou inválida. A mensagem nunca traz valores do arquivo."""


class ServidorMcp(BaseModel):
    """Configuração de um servidor MCP stdio."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nome: str = Field(pattern=PADRAO_NOME_SERVIDOR)
    comando: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # Nomes de ferramentas (sem o prefixo do servidor) tratadas como LEITURA.
    leitura: list[str] = Field(default_factory=list)
    timeout_chamada_s: float = Field(default=TIMEOUT_CHAMADA_PADRAO_S, gt=0)
    timeout_inicio_s: float = Field(default=TIMEOUT_INICIO_PADRAO_S, gt=0)


class ConfigMcp(BaseModel):
    """Todos os servidores MCP de uma execução, pelo nome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    servidores: dict[str, ServidorMcp] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _nome_vem_da_chave(cls, dados: Any) -> Any:
        """Copia a chave de cada servidor para o campo `nome`, recusando um `nome` repetido."""
        if not isinstance(dados, dict) or not isinstance(dados.get("servidores"), dict):
            return dados
        preparados: dict[str, Any] = {}
        for nome, entrada in dados["servidores"].items():
            if isinstance(entrada, dict):
                if "nome" in entrada:
                    raise ValueError("o nome do servidor é a chave do objeto, não um campo")
                entrada = {**entrada, "nome": nome}
            preparados[nome] = entrada
        return {**dados, "servidores": preparados}

    @field_validator("servidores")
    @classmethod
    def _sem_grupos_reservados(cls, servidores: dict[str, ServidorMcp]) -> dict[str, ServidorMcp]:
        for nome in servidores:
            if nome in GRUPOS_RESERVADOS:
                raise ValueError(f"o nome {nome!r} é reservado para um grupo do harness")
        return servidores


def _resumir(erro: ValidationError) -> str:
    """Caminho e motivo de cada erro, sem o valor que o pydantic costuma repetir."""
    partes: list[str] = []
    for item in erro.errors():
        caminho = ".".join(str(parte) for parte in item["loc"]) or "configuração"
        partes.append(f"{caminho}: {item['msg']}")
    return "; ".join(partes) or "configuração inválida"


def carregar_config_mcp(caminho: Path) -> ConfigMcp:
    """Lê e valida o arquivo de configuração. Qualquer problema levanta ErroConfigMcp."""
    if not caminho.is_file():
        raise ErroConfigMcp(f"arquivo de configuração MCP não encontrado: {caminho}")
    try:
        texto = caminho.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ErroConfigMcp(f"arquivo de configuração MCP não pode ser lido: {caminho}") from None
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ErroConfigMcp(
            f"JSON inválido na configuração MCP (linha {erro.lineno}): {caminho}"
        ) from None
    try:
        return ConfigMcp.model_validate(dados)
    except ValidationError as erro:
        raise ErroConfigMcp(f"configuração MCP inválida ({_resumir(erro)}): {caminho}") from None
