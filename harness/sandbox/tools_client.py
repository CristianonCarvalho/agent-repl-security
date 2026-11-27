"""Objeto `tools` do REPL: chamadas de ferramenta que atravessam a ponte até o host (Tarefa 5).

As ferramentas executam no HOST. Aqui só se monta a requisição `tool_call`, se escreve no
canal do protocolo (o mesmo de `serve`, nunca o stdout do modelo) e se bloqueia lendo a
resposta `tool_result` com o mesmo `call_id`. O sandbox não conhece o registro de
ferramentas: o objeto `tools` é dinâmico, e uma ferramenta inexistente é erro do host.

Este módulo não importa nada de `harness.host`: essa separação é o que mantém o código do
host fora da imagem do sandbox.
"""

import json
from typing import Any, Final, TextIO

from harness.sandbox.protocol import (
    LIMITE_CHAMADAS_POR_CELULA,
    ArquivoAberto,
    ProtocolError,
    ToolCall,
    ToolResult,
    decode,
    encode,
)

_USO: Final = "use tools.<grupo>.<nome>(...) para chamar uma ferramenta"


class ToolError(Exception):
    """Falha de uma chamada de ferramenta, vista pelo código do modelo.

    A mensagem é a do host (`error` do tool_result) ou um aviso local do sandbox.
    """


class Ponte:
    """Canal de chamadas de ferramenta: escreve `tool_call` e espera o `tool_result` certo.

    `entrada` e `saida` são os mesmos objetos que `serve` usa para o protocolo. Como a célula
    roda numa thread enquanto o laço principal espera, as duas leituras nunca se sobrepõem.
    """

    def __init__(self, entrada: TextIO, saida: TextIO) -> None:
        self._entrada = entrada
        self._saida = saida
        # Contador que gera call_id únicos ao longo de toda a sessão do REPL.
        self._emitidas = 0
        # Chamadas feitas na célula em curso; zerado por nova_celula().
        self._na_celula = 0
        # Soma dos espera_s recebidos na célula em curso; zerado por nova_celula().
        self._credito_s = 0.0

    @property
    def credito_s(self) -> float:
        """Segundos de espera humana acumulados na célula em curso."""
        return self._credito_s

    def nova_celula(self) -> None:
        """Zera o limite de chamadas e o crédito de espera no início de cada célula."""
        self._na_celula = 0
        self._credito_s = 0.0

    def chamar(self, nome: str, args: dict[str, Any]) -> Any:
        """Envia uma chamada ao host e devolve `data`, ou levanta ToolError se `ok` for falso."""
        if self._na_celula >= LIMITE_CHAMADAS_POR_CELULA:
            raise ToolError(
                f"limite de {LIMITE_CHAMADAS_POR_CELULA} chamadas de ferramenta por célula "
                "atingido; a chamada não foi enviada ao host"
            )
        # Recusa aqui o que não vira JSON (NaN, objetos): o host descartaria o contêiner.
        try:
            json.dumps(args, allow_nan=False)
        except (TypeError, ValueError) as erro:
            raise ToolError(
                "os argumentos precisam ser valores JSON: texto, número, lista, objeto, "
                "booleano ou None (NaN e infinito não são aceitos)"
            ) from erro
        self._emitidas += 1
        call_id = f"c{self._emitidas}"
        quadro = encode(ToolCall(call_id=call_id, name=nome, args=args))
        self._na_celula += 1
        try:
            self._saida.write(quadro)
            self._saida.flush()
        except (OSError, ValueError) as erro:
            raise ToolError(
                f"o canal com o host está fechado; '{nome}' não foi executada"
            ) from erro
        return self._esperar_resultado(call_id, nome)

    def avisar_abertura(self, caminho: str) -> None:
        """Avisa o host que o código vai abrir `caminho` e espera a liberação.

        Não conta no limite de chamadas por célula. Levanta ToolError se o host recusar ou o
        canal falhar: quem chama deve tratar isso como abertura bloqueada.
        """
        self._emitidas += 1
        call_id = f"c{self._emitidas}"
        quadro = encode(ArquivoAberto(call_id=call_id, caminho=caminho))
        try:
            self._saida.write(quadro)
            self._saida.flush()
        except (OSError, ValueError) as erro:
            raise ToolError(
                "o canal com o host está fechado; a abertura não foi autorizada"
            ) from erro
        self._esperar_resultado(call_id, "abertura de arquivo")

    def _esperar_resultado(self, call_id: str, nome: str) -> Any:
        while True:
            try:
                linha = self._entrada.readline()
            except (OSError, ValueError) as erro:
                raise ToolError(f"o canal com o host falhou durante '{nome}'") from erro
            if linha == "":
                raise ToolError(f"o host fechou o canal antes de responder à ferramenta '{nome}'")
            mensagem = decode(linha)
            if isinstance(mensagem, ToolResult):
                if mensagem.call_id != call_id:
                    raise ProtocolError(
                        f"resposta da chamada '{mensagem.call_id}', mas a célula espera "
                        f"a chamada '{call_id}'"
                    )
                self._credito_s += mensagem.espera_s or 0.0
                if mensagem.ok:
                    return mensagem.data
                raise ToolError(mensagem.error or "erro desconhecido do host")
            raise ProtocolError(
                f"mensagem '{mensagem.method}' inesperada enquanto a célula aguarda a "
                f"ferramenta '{nome}'"
            )


class Ferramentas:
    """Namespace `tools.<grupo>.<nome>`: cada acesso desce um nível; a chamada faz a requisição.

    Sem ponte (testes antigos ou uso fora do serve), toda chamada levanta ToolError.
    """

    __slots__ = ("_caminho", "_ponte")

    def __init__(self, ponte: Ponte | None, caminho: tuple[str, ...] = ()) -> None:
        self._ponte = ponte
        self._caminho = caminho

    def __getattr__(self, nome: str) -> "Ferramentas":
        # Nomes privados ficam de fora: o modelo não alcança atributos internos por `tools`.
        if nome.startswith("_"):
            raise AttributeError(f"atributo inexistente: {nome!r}")
        if len(self._caminho) >= 2:
            raise ToolError(_USO)
        return Ferramentas(self._ponte, (*self._caminho, nome))

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if len(self._caminho) != 2:
            raise ToolError(_USO)
        if args:
            raise ToolError(
                "as ferramentas aceitam só argumentos nomeados, por exemplo "
                "tools.grupo.nome(campo=valor)"
            )
        if self._ponte is None:
            raise ToolError("ferramentas indisponíveis")
        return self._ponte.chamar(".".join(self._caminho), kwargs)
