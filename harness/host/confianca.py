"""Flag de confiança dos arquivos (spec v0, D10), compartilhada pelo catálogo e pelo rastreio.

A flag vem de um `manifesto.json` opcional na raiz de dados: sem manifesto, todo arquivo é
`confiavel`; qualquer valor que não seja `confiavel` no manifesto vira `externo` (falha fechada).
"""

import json
from pathlib import Path
from typing import Any, Final

NOME_MANIFESTO: Final = "manifesto.json"
CONFIAVEL: Final = "confiavel"
EXTERNO: Final = "externo"


class ErroManifesto(Exception):
    """O `manifesto.json` existe, mas não é um objeto JSON válido."""


def ler_manifesto(raiz: Path) -> dict[str, Any]:
    """Lê o manifesto da raiz. Ausente vira dicionário vazio; malformado levanta ErroManifesto."""
    caminho = raiz / NOME_MANIFESTO
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ErroManifesto("o manifesto.json da raiz de dados é inválido") from erro
    if not isinstance(dados, dict):
        raise ErroManifesto("o manifesto.json da raiz de dados é inválido")
    return dados


def confianca_de(manifesto: dict[str, Any], arquivo: str) -> str:
    """`confiavel` só se o manifesto disser isso; qualquer outra coisa vira `externo` (D10)."""
    if arquivo not in manifesto:
        return CONFIAVEL
    return CONFIAVEL if manifesto[arquivo] == CONFIAVEL else EXTERNO
