"""Interface de modelo de linguagem usada pelo orquestrador (spec v0, "Modelo e provedor").

O loop fala só com este contrato: uma lista de mensagens entra, uma resposta com o texto e
a contagem de tokens sai. O cliente real do OpenRouter fica para a T9; aqui existe só o
`LLMFalso`, que devolve um roteiro determinístico e permite os testes do loop sem custo.

T18a acrescenta o tool calling nativo (variante JSON): mensagens `tool`, chamadas de ferramenta
na resposta e o protocolo `ModeloComFerramentas`. O caminho sem ferramentas não muda.
"""

from collections.abc import Sequence
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.llm.ferramentas import EsquemaFerramenta


class ChamadaFerramenta(BaseModel):
    """Uma chamada de ferramenta pedida pelo modelo, no formato da API.

    `nome` é o nome que a API usa (ver `harness.llm.ferramentas`). `argumentos_brutos` é o texto
    cru dos argumentos, sem validação: decidir se é JSON de objeto é papel do laço.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    nome: str
    argumentos_brutos: str


class Mensagem(BaseModel):
    """Uma mensagem do histórico enviado ao modelo.

    Papéis `system`, `user` e `assistant` como antes. `assistant` pode trazer `chamadas_ferramenta`
    (texto vazio é permitido nesse caso). `tool` é a resposta de uma chamada: exige `tool_call_id`
    e `name` (o nome da função na API).
    """

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    chamadas_ferramenta: tuple[ChamadaFerramenta, ...] = ()

    @model_validator(mode="after")
    def _campos_por_papel(self) -> Self:
        if self.role == "tool":
            if not self.tool_call_id or not self.name:
                raise ValueError("mensagem tool exige tool_call_id e name")
        elif self.tool_call_id is not None or self.name is not None:
            raise ValueError("tool_call_id e name só existem em mensagens tool")
        if self.chamadas_ferramenta and self.role != "assistant":
            raise ValueError("chamadas_ferramenta só existem em mensagens assistant")
        return self


class RespostaLLM(BaseModel):
    """Resposta do modelo: o texto cru e a contagem de tokens de entrada e de saída.

    `modelo`, `provedor` e `custo` são informativos (para a trace e os evals). Só o cliente
    real os preenche; o LLMFalso deixa None. `provedor` é o provedor efetivo que atendeu.

    `motivo_fim` é o `finish_reason` do provedor; `tokens_raciocinio` é a parte da saída gasta
    em raciocínio oculto, quando o provedor a informa; `tentativas_descartadas` conta quantas
    respostas vazias foram repetidas antes desta. Na resposta final do cliente, os tokens e o
    custo já incluem essas tentativas descartadas.

    `chamadas_ferramenta` são as chamadas pedidas pelo modelo, na ordem em que vieram. O texto
    pode vir vazio quando há chamadas.
    """

    model_config = ConfigDict(frozen=True)

    texto: str
    tokens_entrada: int = Field(ge=0)
    tokens_saida: int = Field(ge=0)
    modelo: str | None = None
    provedor: str | None = None
    custo: float | None = None
    motivo_fim: str | None = None
    tokens_raciocinio: int | None = None
    tentativas_descartadas: int = Field(default=0, ge=0)
    chamadas_ferramenta: tuple[ChamadaFerramenta, ...] = ()


class ErroLLM(Exception):
    """Falha do modelo que não se resolveu. O loop a trata como ERRO_LLM.

    A mensagem é curta e genérica: não traz o corpo da resposta, headers nem credenciais.
    """


class ModeloLLM(Protocol):
    """Qualquer objeto que responda a uma lista de mensagens com uma `RespostaLLM`."""

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        """Envia o histórico ao modelo e devolve a resposta."""
        ...


class ModeloComFerramentas(Protocol):
    """Modelo que aceita chamadas de ferramenta nativas (variante JSON, T18a)."""

    def responder_com_ferramentas(
        self,
        mensagens: Sequence[Mensagem],
        ferramentas: Sequence[EsquemaFerramenta],
        *,
        escolha: Literal["auto", "none"] = "auto",
    ) -> RespostaLLM:
        """Envia o histórico e as ferramentas ao modelo e devolve a resposta."""
        ...


class RoteiroEsgotado(Exception):
    """O LLMFalso recebeu mais chamadas do que respostas roteirizadas."""


class LLMFalso:
    """Modelo de teste que devolve, em ordem, as respostas de um roteiro.

    Cada item do roteiro é um texto (os tokens são estimados) ou uma `RespostaLLM` (os tokens
    são os informados; pode trazer `chamadas_ferramenta`). `chamadas` guarda uma cópia das
    mensagens de cada chamada, para que os testes possam inspecionar o que o loop enviou ao
    modelo. `ferramentas_recebidas` guarda, por chamada com ferramentas, o esquema e a escolha.
    """

    def __init__(self, roteiro: Sequence[str | RespostaLLM]) -> None:
        self._roteiro = list(roteiro)
        self._proxima = 0
        self.chamadas: list[list[Mensagem]] = []
        self.ferramentas_recebidas: list[tuple[tuple[EsquemaFerramenta, ...], str]] = []

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        return self._proxima_resposta(mensagens)

    def responder_com_ferramentas(
        self,
        mensagens: Sequence[Mensagem],
        ferramentas: Sequence[EsquemaFerramenta],
        *,
        escolha: Literal["auto", "none"] = "auto",
    ) -> RespostaLLM:
        self.ferramentas_recebidas.append((tuple(ferramentas), escolha))
        return self._proxima_resposta(mensagens)

    def _proxima_resposta(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
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
