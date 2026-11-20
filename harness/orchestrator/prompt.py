"""Prompt de sistema versionado (spec v0, passo 3 do ciclo e Contrato 1).

O texto vem de `prompts/sistema.md`, um arquivo versionado no repositório e empacotado no
wheel. O marcador `{catalogo}` é trocado pelo catálogo resumido do Registro: uma linha por
ferramenta, com nome e descrição. O hash SHA-256 é calculado sobre o TEXTO FINAL, então
muda sempre que o arquivo ou o catálogo mudar, e a trace (T8) pode gravá-lo.

Este módulo roda no HOST e nunca entra no sandbox.
"""

import hashlib
from dataclasses import dataclass
from importlib import resources
from typing import Final

from harness.host.registry import Registro

NOME_ARQUIVO_PROMPT: Final = "sistema.md"
PACOTE_PROMPTS: Final = "harness.orchestrator.prompts"
MARCADOR_CATALOGO: Final = "{catalogo}"
SEM_FERRAMENTAS: Final = "(nenhuma ferramenta registrada)"


@dataclass(frozen=True)
class PromptSistema:
    """Texto final do prompt de sistema e o seu hash."""

    texto: str
    hash_sha256: str


def carregar_modelo() -> str:
    """Lê o arquivo versionado do prompt, ainda com o marcador `{catalogo}`."""
    arquivo = resources.files(PACOTE_PROMPTS).joinpath(NOME_ARQUIVO_PROMPT)
    return arquivo.read_text(encoding="utf-8")


def catalogo_resumido(registro: Registro) -> str:
    """Uma linha por ferramenta: `- nome: descrição`. Descrições com quebras viram uma linha."""
    linhas = [
        f"- {ferramenta.nome}: {' '.join(ferramenta.descricao.split())}"
        for ferramenta in registro.listar()
    ]
    return "\n".join(linhas) if linhas else SEM_FERRAMENTAS


def montar_prompt_sistema(registro: Registro) -> PromptSistema:
    """Monta o prompt de sistema com o catálogo do registro e calcula o hash do texto final."""
    texto = carregar_modelo().replace(MARCADOR_CATALOGO, catalogo_resumido(registro))
    hash_sha256 = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    return PromptSistema(texto=texto, hash_sha256=hash_sha256)
