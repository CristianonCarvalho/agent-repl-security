"""Prompt de sistema versionado (spec v0, passo 3 do ciclo e Contrato 1).

O texto vem de `prompts/sistema.md`, um arquivo versionado no repositório e empacotado no
wheel. O marcador `{grupos}` é trocado pela lista de NOMES DOS GRUPOS de ferramentas (a parte
antes do ponto, como `catalogo`). Descrições e nomes completos não entram no prompt: o modelo
descobre as ferramentas com `search_tools` e `describe_tool` (Tarefa 10). O grupo `descoberta`
fica de fora, porque são as próprias ferramentas de descoberta.

O hash SHA-256 é calculado sobre o TEXTO FINAL. Como o texto só traz os grupos, o hash muda
quando o arquivo muda ou quando surge um grupo novo, e não quando uma ferramenta é adicionada
a um grupo que já existe. A trace (T8) pode gravá-lo.

Este módulo roda no HOST e nunca entra no sandbox.
"""

import hashlib
from dataclasses import dataclass
from importlib import resources
from typing import Final

from harness.host.registry import Registro
from harness.host.tools.descoberta import GRUPO as GRUPO_DESCOBERTA

NOME_ARQUIVO_PROMPT: Final = "sistema.md"
PACOTE_PROMPTS: Final = "harness.orchestrator.prompts"
MARCADOR_GRUPOS: Final = "{grupos}"
SEM_FERRAMENTAS: Final = "(nenhuma ferramenta registrada)"


@dataclass(frozen=True)
class PromptSistema:
    """Texto final do prompt de sistema e o seu hash."""

    texto: str
    hash_sha256: str


def carregar_modelo() -> str:
    """Lê o arquivo versionado do prompt, ainda com o marcador `{grupos}`."""
    arquivo = resources.files(PACOTE_PROMPTS).joinpath(NOME_ARQUIVO_PROMPT)
    return arquivo.read_text(encoding="utf-8")


def grupos_resumidos(registro: Registro) -> str:
    """Uma linha por grupo de ferramentas, em ordem: `- grupo`. Sem o grupo `descoberta`."""
    grupos = sorted(
        {ferramenta.nome.split(".")[0] for ferramenta in registro.listar()} - {GRUPO_DESCOBERTA}
    )
    linhas = [f"- {grupo}" for grupo in grupos]
    return "\n".join(linhas) if linhas else SEM_FERRAMENTAS


def montar_prompt_sistema(registro: Registro) -> PromptSistema:
    """Monta o prompt de sistema com os grupos do registro e calcula o hash do texto final."""
    texto = carregar_modelo().replace(MARCADOR_GRUPOS, grupos_resumidos(registro))
    hash_sha256 = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    return PromptSistema(texto=texto, hash_sha256=hash_sha256)
