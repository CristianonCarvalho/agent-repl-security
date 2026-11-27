"""Mensagens da ponte JSON-RPC 2.0 entre o host e o sandbox (contrato 2 da spec, D12).

Cada mensagem ocupa exatamente uma linha (NDJSON) e trafega no descritor de arquivo
dedicado do processo do sandbox, separado do stdout do código do modelo.

Este módulo só modela, valida e codifica mensagens. Não executa código, não abre
rede e não fala com o LLM.
"""

import json
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

# Limite de chamadas de ferramenta por célula (spec v0, "Limites iniciais"). O sandbox aplica
# primeiro, em tools_client; o host reforça o mesmo teto, para não depender só do sandbox.
LIMITE_CHAMADAS_POR_CELULA: Final = 50


class ProtocolError(Exception):
    """Mensagem da ponte inválida: JSON malformado, método desconhecido ou campos inválidos."""


class _Mensagem(BaseModel):
    """Base comum das mensagens: rejeita campos fora do contrato e é imutável."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Toda mensagem JSON-RPC 2.0 carrega a versão do protocolo.
    jsonrpc: Literal["2.0"] = "2.0"


class Exec(_Mensagem):
    """host → sandbox: pede a execução de uma célula de código."""

    method: Literal["exec"] = "exec"
    cell_id: StrictInt
    code: str
    # Limite da célula em segundos; zero ou negativo não faz sentido e NaN/infinito são recusados.
    timeout_s: float = Field(gt=0, allow_inf_nan=False)


class ToolCall(_Mensagem):
    """sandbox → host: a célula chamou uma ferramenta e espera a resposta."""

    method: Literal["tool_call"] = "tool_call"
    call_id: str
    name: str
    args: dict[str, Any]


class ToolResult(_Mensagem):
    """host → sandbox: resposta de uma chamada de ferramenta.

    Regra: se ok é verdadeiro, error fica ausente (None); se ok é falso, error é obrigatório.
    """

    method: Literal["tool_result"] = "tool_result"
    call_id: str
    ok: StrictBool
    # Qualquer JSON: dict, lista, texto, número, booleano ou null.
    data: Any = None
    error: str | None = None
    espera_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _error_coerente_com_ok(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("tool_result com ok=true não pode trazer 'error'")
        if not self.ok and self.error is None:
            raise ValueError("tool_result com ok=false exige 'error'")
        return self


class RespostaFinal(BaseModel):
    """Objeto de término (FINAL) enviado dentro de exec_done."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str
    sources: list[str]


class ExecDone(_Mensagem):
    """sandbox → host: a célula terminou; traz stdout e stderr capturados por célula."""

    method: Literal["exec_done"] = "exec_done"
    cell_id: StrictInt
    stdout: str
    stderr: str
    exception: str | None
    duration_ms: Annotated[StrictInt, Field(ge=0)]
    final: RespostaFinal | None
    # Verdadeiro quando a célula não pôde ser interrompida e o host precisa reiniciar o REPL.
    # O padrão falso mantém compatível a mensagem antiga, que não trazia o campo.
    reiniciar: StrictBool = False


class Contexto(_Mensagem):
    """host → sandbox: carrega o contexto do pedido na variável `context` do REPL.

    O valor é dado de entrada, nunca código: precisa ser texto, objeto ou lista de JSON.
    Números, booleanos e null são recusados.
    """

    method: Literal["contexto"] = "contexto"
    valor: Any

    @field_validator("valor")
    @classmethod
    def _valor_e_texto_objeto_ou_lista(cls, valor: Any) -> Any:
        if not isinstance(valor, str | dict | list):
            raise ValueError("contexto precisa ser um texto, um objeto ou uma lista de JSON")
        return valor


class ContextoOk(_Mensagem):
    """sandbox → host: o contexto foi carregado; traz o tipo e o tamanho que o REPL viu."""

    method: Literal["contexto_ok"] = "contexto_ok"
    tipo: str
    tamanho: Annotated[StrictInt, Field(ge=0)]


class ArquivoAberto(_Mensagem):
    """sandbox → host: o código vai abrir um arquivo de /dados e espera a liberação.

    O host responde com um `tool_result` de mesmo `call_id`: `ok=True` libera a abertura;
    `ok=False` (com `error`) a bloqueia.
    """

    method: Literal["arquivo_aberto"] = "arquivo_aberto"
    call_id: str
    caminho: str


# União discriminada pelo campo `method`: cada método tem exatamente um modelo.
Mensagem = Exec | ToolCall | ToolResult | ExecDone | Contexto | ContextoOk | ArquivoAberto

_ADAPTADOR: TypeAdapter[Mensagem] = TypeAdapter(Annotated[Mensagem, Field(discriminator="method")])


def encode(msg: Mensagem) -> str:
    """Serializa a mensagem numa única linha JSON terminada em quebra de linha.

    O JSON sai com ensure_ascii=True: caracteres como U+2028 e U+2029 viram escapes,
    porque alguns leitores de linha (str.splitlines) os tratariam como quebra.
    """
    dados = msg.model_dump(mode="json")
    if isinstance(msg, ToolResult) and msg.espera_s is None:
        dados.pop("espera_s", None)
    corpo = json.dumps(dados, ensure_ascii=True, separators=(",", ":"))
    return corpo + "\n"


def decode(linha: str) -> Mensagem:
    """Lê uma linha da ponte e devolve a mensagem validada.

    Levanta ProtocolError para qualquer problema (JSON inválido, método desconhecido,
    campos inválidos ou quebra de linha no meio da mensagem). Nenhuma outra exceção sai daqui.
    """
    corpo = linha
    # Aceita um terminador de linha no fim (o que o readline devolve), nada além disso.
    if corpo.endswith("\n"):
        corpo = corpo[:-1]
    if corpo.endswith("\r"):
        corpo = corpo[:-1]
    if "\n" in corpo or "\r" in corpo:
        raise ProtocolError("mensagem com quebra de linha interna")
    try:
        return _ADAPTADOR.validate_json(corpo)
    except (ValueError, RecursionError) as erro:
        # ValidationError (pydantic) é subclasse de ValueError; cobre JSON inválido,
        # método desconhecido e campos inválidos.
        raise ProtocolError(f"mensagem inválida: {erro}") from erro
