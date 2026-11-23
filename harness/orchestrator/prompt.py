"""Prompt de sistema versionado (spec v0, passo 3 do ciclo e Contrato 1).

O texto vem de `prompts/sistema.md`, um arquivo versionado no repositório e empacotado no
wheel. O marcador `{grupos}` é trocado pela lista de NOMES DOS GRUPOS de ferramentas (a parte
antes do ponto, como `catalogo`). Descrições e nomes completos não entram no prompt: o modelo
descobre as ferramentas com `search_tools` e `describe_tool` (Tarefa 10). O grupo `descoberta`
fica de fora, porque são as próprias ferramentas de descoberta.

Só entram os grupos com pelo menos uma ferramenta concedida ao perfil (D14).

O hash SHA-256 é calculado sobre o TEXTO FINAL. Como o texto só traz os grupos, o hash muda
quando o arquivo muda ou quando surge um grupo novo para o perfil, e não quando uma ferramenta
é adicionada a um grupo que já existe nem quando uma descrição muda. Perfis com grupos
diferentes têm hashes diferentes. A trace (T8) pode gravá-lo.

Este módulo roda no HOST e nunca entra no sandbox.
"""

import hashlib
from dataclasses import dataclass
from importlib import resources
from typing import Final

from harness.host.perfil import Perfil
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


def carregar_modelo(nome_arquivo: str = NOME_ARQUIVO_PROMPT) -> str:
    """Lê o arquivo versionado do prompt, ainda com o marcador `{grupos}`."""
    arquivo = resources.files(PACOTE_PROMPTS).joinpath(nome_arquivo)
    return arquivo.read_text(encoding="utf-8")


def grupos_resumidos(registro: Registro, perfil: Perfil) -> str:
    """Uma linha por grupo com ferramenta concedida ao perfil, em ordem: `- grupo`.

    Sem o grupo `descoberta`, que são as próprias ferramentas de descoberta.
    """
    grupos = sorted(
        {ferramenta.nome.split(".")[0] for ferramenta in registro.visiveis(perfil)}
        - {GRUPO_DESCOBERTA}
    )
    linhas = [f"- {grupo}" for grupo in grupos]
    return "\n".join(linhas) if linhas else SEM_FERRAMENTAS


def montar_prompt_sistema(
    registro: Registro, perfil: Perfil, nome_arquivo: str = NOME_ARQUIVO_PROMPT
) -> PromptSistema:
    """Monta o prompt com os grupos visíveis ao perfil e calcula o hash do texto final.

    `nome_arquivo` escolhe o modelo: `sistema.md` (REPL, padrão) ou `sistema_json.md` (T18a).
    """
    texto = carregar_modelo(nome_arquivo).replace(
        MARCADOR_GRUPOS, grupos_resumidos(registro, perfil)
    )
    hash_sha256 = hashlib.sha256(texto.encode("utf-8")).hexdigest()
    return PromptSistema(texto=texto, hash_sha256=hash_sha256)
