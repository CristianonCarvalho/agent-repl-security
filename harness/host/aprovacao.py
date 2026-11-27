"""Interface de aprovação humana e implementações para chamadas sensíveis no host.

O `PedidoDeAprovacao` traz apenas fatos medidos pelo próprio host (nome da ferramenta,
motivo e pares chave-valor saneados para exibição), nunca texto arbitrário vindo do modelo
sem limpeza. Cada aprovação vale exclusivamente para uma chamada.
"""

import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol, TextIO

LIMITE_EXIBICAO: Final = 200
_ANSI_RE: Final = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def limpar_para_exibicao(texto: str) -> str:
    """Remove escapes ANSI e caracteres não imprimíveis e limita a 200 caracteres."""
    sem_ansi = _ANSI_RE.sub("", texto)
    limpo = "".join(ch for ch in sem_ansi if ch.isprintable())
    if len(limpo) > LIMITE_EXIBICAO:
        return limpo[: LIMITE_EXIBICAO - 1] + "…"
    return limpo


class RespostaDeAprovacao(Enum):
    """Resultado de um pedido de aprovação humana."""

    APROVADO = "aprovado"
    RECUSADO = "recusado"
    EXPIRADO = "expirado"


@dataclass(frozen=True)
class PedidoDeAprovacao:
    """Pedido imutável com os fatos medidos pelo host para decisão de aprovação."""

    ferramenta: str
    motivo: str
    linhas: tuple[tuple[str, str], ...] = ()

    def texto(self) -> str:
        """Formata o pedido em várias linhas, saneando cada valor com `limpar_para_exibicao`."""
        partes = [
            f"Ferramenta: {limpar_para_exibicao(self.ferramenta)}",
            f"Motivo: {limpar_para_exibicao(self.motivo)}",
        ]
        for chave, valor in self.linhas:
            partes.append(f"{limpar_para_exibicao(chave)}: {limpar_para_exibicao(valor)}")
        return "\n".join(partes)


class Aprovador(Protocol):
    """Protocolo para decisores de aprovação de chamadas."""

    def __call__(self, pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao: ...


@dataclass(frozen=True)
class AprovadorFixo:
    """Aprovador determinístico que devolve sempre a mesma resposta."""

    resposta: RespostaDeAprovacao

    def __call__(self, pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao:
        return self.resposta


def sempre_aprova() -> AprovadorFixo:
    """Cria um aprovador fixo que sempre aprova."""
    return AprovadorFixo(RespostaDeAprovacao.APROVADO)


def sempre_recusa() -> AprovadorFixo:
    """Cria um aprovador fixo que sempre recusa."""
    return AprovadorFixo(RespostaDeAprovacao.RECUSADO)


class AprovadorDeTerminal:
    """Aprovador interativo que solicita confirmação em um par de streams de texto."""

    def __init__(self, entrada: TextIO, saida: TextIO) -> None:
        self._entrada = entrada
        self._saida = saida

    def __call__(self, pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao:
        self._saida.write(f"{pedido.texto()}\nAprovar? [s/N] ")
        self._saida.flush()

        lido: list[str] = []

        def _ler() -> None:
            lido.append(self._entrada.readline())

        thread = threading.Thread(target=_ler, daemon=True)
        thread.start()
        thread.join(prazo_s)
        if thread.is_alive():
            return RespostaDeAprovacao.EXPIRADO

        resposta = (lido[0] if lido else "").strip().lower()
        if resposta in {"s", "sim"}:
            return RespostaDeAprovacao.APROVADO
        return RespostaDeAprovacao.RECUSADO
