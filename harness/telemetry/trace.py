"""Trace de um pedido em JSONL: uma linha por evento do orquestrador (Tarefa 8).

Cada chamada do `TraceJsonl` acrescenta uma linha com `seq`, `ts`, `session_id`, `tipo` e
`dados`. A escrita abre o arquivo em modo append e faz flush a cada linha, então um pedido
interrompido deixa um trace parcial, mas válido linha a linha.

Decisões deste módulo (não estão na spec):
- Falha de I/O nunca sai da chamada: é contada em `falhas_de_escrita` e o pedido segue. O
  `seq` avança mesmo na falha, então um buraco no arquivo sempre corresponde a uma falha.
- O diretório é criado na construção, se possível. Se não for, a tentativa acontece de novo
  em cada escrita e a falha entra na contagem.
- Valores que o JSON não aceita (objetos arbitrários) viram o `repr` deles.
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.orchestrator.loop import Evento


def _agora_utc() -> datetime:
    return datetime.now(UTC)


class TraceJsonl:
    """Consumidor de eventos que grava cada um como uma linha JSON num arquivo `.jsonl`."""

    def __init__(
        self,
        diretorio: Path,
        session_id: str | None = None,
        relogio: Callable[[], datetime] = _agora_utc,
    ) -> None:
        self._session_id = session_id if session_id is not None else uuid.uuid4().hex
        self._relogio = relogio
        self._caminho = diretorio / f"{self._session_id}.jsonl"
        self._seq = 0
        self._falhas = 0
        try:
            diretorio.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A escrita tenta criar o diretório de novo e conta a falha se ela persistir.
            pass

    @property
    def caminho(self) -> Path:
        """Arquivo onde o trace é gravado."""
        return self._caminho

    @property
    def falhas_de_escrita(self) -> int:
        """Quantos eventos não chegaram ao arquivo por falha de I/O."""
        return self._falhas

    def __call__(self, evento: Evento) -> None:
        """Acrescenta o evento ao trace. Nunca levanta exceção."""
        self._seq += 1
        try:
            registro = {
                "seq": self._seq,
                "ts": self._relogio().astimezone(UTC).isoformat(),
                "session_id": self._session_id,
                "tipo": evento.tipo,
                "dados": evento.dados,
            }
            linha = json.dumps(registro, ensure_ascii=False, default=repr) + "\n"
            self._caminho.parent.mkdir(parents=True, exist_ok=True)
            with self._caminho.open("a", encoding="utf-8") as arquivo:
                arquivo.write(linha)
                arquivo.flush()
        except Exception:
            self._falhas += 1


def ler_trace(caminho: Path) -> list[dict[str, Any]]:
    """Lê um trace JSONL de volta, na ordem em que foi gravado. Linhas em branco são ignoradas."""
    registros: list[dict[str, Any]] = []
    with caminho.open(encoding="utf-8") as arquivo:
        for linha in arquivo:
            if linha.strip():
                registros.append(json.loads(linha))
    return registros
