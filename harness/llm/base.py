"""Interface de modelo de linguagem usada pelo orquestrador (spec v0, "Modelo e provedor").

O loop fala só com este contrato: uma lista de mensagens entra, uma resposta com o texto e
a contagem de tokens sai. O cliente real do OpenRouter fica para a T9; aqui existe só o
`LLMFalso`, que devolve um roteiro determinístico e permite os testes do loop sem custo.
"""

from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Mensagem(BaseModel):
    """Uma mensagem do histórico enviado ao modelo."""

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class RespostaLLM(BaseModel):
    """Resposta do modelo: o texto cru e a contagem de tokens de entrada e de saída."""

    model_config = ConfigDict(frozen=True)

    texto: str
    tokens_entrada: int = Field(ge=0)
    tokens_saida: int = Field(ge=0)


class ModeloLLM(Protocol):
    """Qualquer objeto que responda a uma lista de mensagens com uma `RespostaLLM`."""

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        """Envia o histórico ao modelo e devolve a resposta."""
        ...


class RoteiroEsgotado(Exception):
    """O LLMFalso recebeu mais chamadas do que respostas roteirizadas."""


class LLMFalso:
    """Modelo de teste que devolve, em ordem, as respostas de um roteiro.

    Cada item do roteiro é um texto (os tokens são estimados) ou uma `RespostaLLM` (os tokens
    são os informados). `chamadas` guarda uma cópia das mensagens de cada chamada, para que
    os testes possam inspecionar o que o loop enviou ao modelo.
    """

    def __init__(self, roteiro: Sequence[str | RespostaLLM]) -> None:
        self._roteiro = list(roteiro)
        self._proxima = 0
        self.chamadas: list[list[Mensagem]] = []

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        self.chamadas.append(list(mensagens))
        if self._proxima >= len(self._roteiro):
            raise RoteiroEsgotado(
                f"o roteiro tinha {len(self._roteiro)} respostas e o modelo foi chamado de novo"
            )
        item = self._roteiro[self._proxima]
        self._proxima += 1
        if isinstance(item, RespostaLLM):
            return item
        # Estimativa de tokens: ~4 caracteres por token, com um mínimo de 1 na saída.
        entrada = sum(len(mensagem.content) for mensagem in mensagens) // 4
        return RespostaLLM(texto=item, tokens_entrada=entrada, tokens_saida=len(item) // 4 + 1)
