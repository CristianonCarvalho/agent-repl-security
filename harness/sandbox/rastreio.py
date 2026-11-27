"""Gancho de auditoria do REPL: avisa o host antes de cada `open` sob /dados (Fase 4).

O host decide a confiança do arquivo e marca a exposição da sessão. O gancho falha fechado:
se o aviso não for liberado, a abertura levanta PermissionError. Um gancho de auditoria não
pode ser removido depois de instalado.

Limite assumido: o gancho não detém código que fuja do interpretador (por exemplo, `ctypes`).
É defesa em camadas; o isolamento forte é o contêiner e, depois, o gVisor.

Este módulo só importa o cliente da ponte: nada de `harness.host`.
"""

import os
import sys
import threading
from collections.abc import Callable
from typing import Any, Final

RAIZ_DADOS: Final = "/dados"


def criar_gancho(ponte: Any, raiz: str = RAIZ_DADOS) -> Callable[[str, tuple[object, ...]], None]:
    """Monta a função de auditoria. Separada de `instalar_gancho` para ser testável sem
    registrar nada no processo."""
    prefixo = raiz.rstrip("/") + "/"
    local = threading.local()

    def gancho(evento: str, argumentos: tuple[object, ...]) -> None:
        if evento != "open" or not argumentos:
            return
        alvo = argumentos[0]
        if not isinstance(alvo, str | bytes | os.PathLike):
            return
        try:
            caminho = os.path.realpath(os.fsdecode(alvo))
        except (TypeError, ValueError):
            return
        if not caminho.startswith(prefixo):
            return
        if getattr(local, "avisando", False):
            return
        local.avisando = True
        try:
            ponte.avisar_abertura(caminho)
        except Exception as erro:
            raise PermissionError("a leitura em /dados foi bloqueada pelo rastreio") from erro
        finally:
            local.avisando = False

    return gancho


def instalar_gancho(ponte: Any, raiz: str = RAIZ_DADOS) -> None:
    """Registra o gancho no processo atual. Não há como desfazer."""
    sys.addaudithook(criar_gancho(ponte, raiz))
