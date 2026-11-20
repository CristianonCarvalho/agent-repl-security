"""Auxiliares dos testes MCP: caminho do servidor falso e checagens de processo."""

import os
from pathlib import Path
from typing import Final

SERVIDOR_FALSO: Final = Path(__file__).with_name("servidor_falso.py")


def processo_morto(pid: int) -> bool:
    """Verdadeiro quando o PID não existe mais. Um zumbi (não colhido) conta como vivo."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def ler_pid(caminho: Path) -> int:
    """PID que o servidor falso gravou ao iniciar."""
    return int(caminho.read_text(encoding="utf-8"))
