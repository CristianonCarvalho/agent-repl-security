"""Ferramentas do host, registradas numa instância de Registro (spec v0, "Ferramentas do host")."""

from pathlib import Path

from harness.host.registry import Registro
from harness.host.tools import catalogo


def registrar_todas(registro: Registro, raiz: Path) -> None:
    """Registra todas as ferramentas do host, lendo os dados de `raiz` (a pasta de /dados)."""
    catalogo.registrar(registro, raiz)
