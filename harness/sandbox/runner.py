"""Runner do REPL que roda dentro do sandbox (Tarefa 3, D12; ponte de ferramentas na Tarefa 5).

Este módulo executa o código do modelo num namespace persistente, captura stdout e
stderr por célula, transforma exceções em dados e aplica o limite de tempo por thread.
As chamadas de ferramenta passam pela `Ponte` de `tools_client`, que usa o mesmo canal
do protocolo. `FINAL(answer, sources)` (RF7, D8) grava a resposta no estado da célula e
encerra a célula com a mesma interrupção interna do timeout.

Contrato com o host: o protocolo trafega só pelos objetos `entrada` e `saida` passados
a `serve`. O stdout do código do modelo nunca é escrito neles diretamente.
"""

import argparse
import copy
import ctypes
import io
import linecache
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn, TextIO

from harness.sandbox.contexto import ValorContexto, descrever_contexto
from harness.sandbox.protocol import (
    Contexto,
    ContextoOk,
    Exec,
    ExecDone,
    Mensagem,
    ProtocolError,
    RespostaFinal,
    decode,
    encode,
)
from harness.sandbox.rastreio import instalar_gancho
from harness.sandbox.tools_client import Ferramentas, Ponte, ToolError


class _Interrupcao(BaseException):
    """Exceção injetada na thread da célula para encerrá-la.

    Usada de duas formas: pelo timeout da célula e por `FINAL`, que encerra a célula logo
    depois de gravar a resposta. Herda de BaseException para não ser apanhada por
    `except Exception` do código do modelo, embora um `except BaseException` ainda possa
    engoli-la (nesse caso a thread continua viva e o `FINAL` gravado continua valendo).
    """


@dataclass
class _EstadoFinal:
    """Estado do FINAL de UMA célula: se a célula aceita FINAL e a resposta gravada, se houver.

    Cada célula cria o seu. Assim uma referência a FINAL guardada numa célula anterior não
    grava resposta na célula atual (`ativa` é falso depois que a célula termina).
    """

    ativa: bool = False
    resposta: RespostaFinal | None = None


@dataclass(frozen=True)
class ResultadoCelula:
    """Resultado de uma célula: a mensagem `exec_done` e se o host precisa reiniciar o REPL."""

    done: ExecDone
    precisa_reiniciar: bool


class _Captura(io.StringIO):
    """StringIO que ignora close().

    Se o código do modelo fechar sys.stdout, o runner ainda precisa ler o que foi capturado.
    Por isso o close não apaga o conteúdo.
    """

    def close(self) -> None:
        pass


@dataclass
class _Relato:
    """Texto da exceção da célula, preenchido pela thread ao terminar com erro."""

    excecao: str | None = None


def _formatar_excecao(erro: BaseException, nome: str) -> str:
    """Formata a exceção com tipo, mensagem e só os quadros do código do modelo.

    Os quadros do próprio runner são descartados: o modelo não precisa ver o funcionamento
    interno do sandbox.
    """
    quadros = [
        quadro for quadro in traceback.extract_tb(erro.__traceback__) if quadro.filename == nome
    ]
    partes: list[str] = []
    if quadros:
        partes.append("Traceback (most recent call last):\n")
        partes.extend(traceback.format_list(quadros))
    partes.extend(traceback.format_exception_only(erro))
    return "".join(partes).rstrip("\n")


def _interromper(thread: threading.Thread) -> None:
    """Pede à thread que levante _Interrupcao no próximo ponto seguro do interpretador.

    Não interrompe chamadas bloqueantes em C (como time.sleep); nesses casos a thread
    continua viva e o chamador decide que o REPL precisa ser reiniciado.
    """
    if thread.ident is None:
        return
    # ctypes.pythonapi não tem tipos completos; a chamada é a API documentada do CPython.
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread.ident), ctypes.py_object(_Interrupcao)
    )


def _validar_final(answer: Any, sources: Any) -> RespostaFinal:
    """Confere os argumentos de FINAL e devolve a resposta validada.

    Regra D8: a lista de fontes é obrigatória e não pode ser vazia. Qualquer falha vira
    ValueError com mensagem em português, que o modelo vê como exceção da célula.
    """
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError(
            "FINAL: 'answer' precisa ser um texto não vazio, por exemplo "
            "FINAL('resposta', ['arquivo.csv'])"
        )
    if not isinstance(sources, list) or not sources:
        raise ValueError(
            "FINAL: 'sources' precisa ser uma lista com pelo menos uma fonte, por exemplo "
            "['arquivo.csv']"
        )
    if not all(isinstance(fonte, str) and fonte.strip() for fonte in sources):
        raise ValueError("FINAL: cada item de 'sources' precisa ser um texto não vazio")
    return RespostaFinal(answer=answer, sources=list(sources))


def _criar_final(estado: _EstadoFinal) -> Callable[[Any, Any], NoReturn]:
    """Cria a função FINAL ligada ao estado de uma célula.

    Chamada válida grava a resposta no estado e levanta `_Interrupcao`, para o resto da
    célula não rodar. Se o código do modelo engolir a interrupção, `run_cell` ainda lê o
    estado gravado e devolve a resposta.
    """

    def FINAL(answer: Any, sources: Any) -> NoReturn:
        if not estado.ativa:
            raise ValueError("FINAL só pode ser chamado pela célula que está em execução")
        estado.resposta = _validar_final(answer, sources)
        raise _Interrupcao()

    return FINAL


def _context_info_sem_contexto() -> dict[str, Any]:
    """`context_info` antes de qualquer carga: avisa, em português, que não há contexto."""
    raise ValueError(
        "não há contexto carregado neste pedido: context_info() só funciona quando o pedido "
        "traz uma entrada de contexto"
    )


def _criar_context_info(copia: ValorContexto) -> Callable[[], dict[str, Any]]:
    """Cria `context_info` ligada a uma CÓPIA do contexto original.

    A cópia é guardada em fechamento. Assim, se o modelo reatribuir `context` (ou alterá-lo
    no lugar), o resultado de `context_info()` continua descrevendo o contexto carregado.
    """

    def context_info() -> dict[str, Any]:
        descricao = descrever_contexto(copia)
        return {"tipo": descricao.tipo, "tamanho": descricao.tamanho, "previa": descricao.previa}

    return context_info


def _apelidos_de_descoberta(ferramentas: Ferramentas) -> dict[str, Callable[..., Any]]:
    """Apelidos `search_tools` e `describe_tool` do REPL (spec v0, contrato 1; Tarefa 10).

    Cada um chama uma ferramenta `descoberta.*` pela ponte, então a busca é feita no host.
    Os apelidos seguram o objeto `ferramentas` recebido, e não o nome `tools` do namespace:
    se o modelo reatribuir `tools`, os apelidos continuam funcionando.
    """

    def search_tools(query: str, limit: int = 5) -> Any:
        return ferramentas.descoberta.buscar(query=query, limit=limit)

    def describe_tool(name: str) -> Any:
        return ferramentas.descoberta.descrever(name=name)

    return {"search_tools": search_tools, "describe_tool": describe_tool}


class Repl:
    """REPL com namespace persistente: variáveis de uma célula existem nas seguintes."""

    def __init__(self, ponte: Ponte | None = None) -> None:
        self._ponte = ponte
        ferramentas = Ferramentas(ponte)
        # Um único dict de globais é reutilizado por todas as células. `tools`, a exceção
        # ToolError, FINAL e os apelidos de descoberta ficam disponíveis para o modelo; sem
        # ponte, `tools` e os apelidos só levantam erro. Um Repl novo (reinício) os recria.
        self._globais: dict[str, Any] = {
            "__name__": "__main__",
            "tools": ferramentas,
            "ToolError": ToolError,
            # FINAL inativo até a primeira célula; run_cell o troca a cada célula.
            "FINAL": _criar_final(_EstadoFinal()),
            # `context` só existe depois de carregar_contexto; context_info avisa até lá.
            "context_info": _context_info_sem_contexto,
            **_apelidos_de_descoberta(ferramentas),
        }

    def carregar_contexto(self, valor: ValorContexto) -> ContextoOk:
        """Define `context` e `context_info` no namespace e devolve tipo e tamanho.

        Um segundo carregamento substitui o primeiro. `context` guarda o valor recebido; a
        cópia usada por `context_info` é feita antes, para não depender de reatribuições.
        """
        copia = copy.deepcopy(valor)
        self._globais["context"] = valor
        self._globais["context_info"] = _criar_context_info(copia)
        descricao = descrever_contexto(copia)
        return ContextoOk(tipo=descricao.tipo, tamanho=descricao.tamanho)

    def run_cell(
        self, cell_id: int, code: str, timeout_s: float, graca_s: float = 1.0
    ) -> ResultadoCelula:
        """Executa uma célula numa thread e devolve o resultado com a saída capturada.

        Espera `timeout_s` pela thread. Se ela não terminar, levanta _Interrupcao nela e
        espera mais `graca_s`. Se ainda assim continuar viva, marca que é preciso reiniciar.
        """
        nome = f"<celula {cell_id}>"
        # O código do modelo aparece no traceback com as linhas reais, pois o linecache
        # passa a conhecer o texto desta célula.
        linecache.cache[nome] = (len(code), None, code.splitlines(keepends=True), nome)
        if self._ponte is not None:
            # O limite de chamadas de ferramenta vale por célula.
            self._ponte.nova_celula()

        saida_capturada = _Captura()
        erro_capturado = _Captura()
        relato = _Relato()
        # Estado novo por célula: FINAL de célula anterior não vale aqui (RF7).
        estado_final = _EstadoFinal(ativa=True)
        self._globais["FINAL"] = _criar_final(estado_final)
        original_out, original_err = sys.stdout, sys.stderr
        inicio = time.monotonic()
        estourou = False
        precisa_reiniciar = False
        sys.stdout = saida_capturada
        sys.stderr = erro_capturado
        try:
            thread = threading.Thread(
                target=self._corpo,
                args=(code, nome, relato),
                name=f"celula-{cell_id}",
                daemon=True,
            )
            thread.start()
            thread.join(timeout_s)
            if thread.is_alive():
                estourou = True
                _interromper(thread)
                thread.join(graca_s)
                precisa_reiniciar = thread.is_alive()
        finally:
            # Restaura mesmo que o código tenha reatribuído ou fechado sys.stdout/sys.stderr.
            sys.stdout = original_out
            sys.stderr = original_err
            # Depois da célula, FINAL deixa de aceitar resposta (ver _EstadoFinal).
            estado_final.ativa = False
        duracao_ms = int((time.monotonic() - inicio) * 1000)

        if estourou and precisa_reiniciar:
            excecao: str | None = (
                f"TimeoutError: a célula excedeu o limite de tempo de {timeout_s:g} s e não pôde "
                "ser interrompida. O REPL precisa ser reiniciado: as variáveis serão perdidas."
            )
        elif estourou:
            excecao = (
                f"TimeoutError: a célula excedeu o limite de tempo de {timeout_s:g} s e foi "
                "interrompida. "
                "O REPL continua disponível, com as variáveis já definidas."
            )
        else:
            excecao = relato.excecao

        done = ExecDone(
            cell_id=cell_id,
            stdout=saida_capturada.getvalue(),
            stderr=erro_capturado.getvalue(),
            exception=excecao,
            duration_ms=duracao_ms,
            # Vale mesmo se o timeout estourou depois de FINAL: a resposta já foi gravada.
            final=estado_final.resposta,
            # Sinal explícito para o host: a thread ficou viva mesmo depois da interrupção.
            reiniciar=precisa_reiniciar,
        )
        return ResultadoCelula(done=done, precisa_reiniciar=precisa_reiniciar)

    def _corpo(self, code: str, nome: str, relato: _Relato) -> None:
        """Corpo da thread da célula: executa o código e guarda a exceção, se houver."""
        try:
            try:
                exec(compile(code, nome, "exec"), self._globais)
            except _Interrupcao:
                # Interrupção de timeout: quem chamou run_cell já sabe que estourou.
                pass
            except BaseException as erro:
                relato.excecao = _formatar_excecao(erro, nome)
        except _Interrupcao:
            # A interrupção chegou fora do exec, por exemplo durante a formatação.
            pass


def serve(entrada: TextIO, saida: TextIO, repl: Repl | None = None, rastrear: bool = False) -> None:
    """Lê mensagens do host linha a linha e responde cada `exec` com um `exec_done`.

    Termina quando `entrada` chega ao fim. No nível de topo só `exec` e `contexto` são
    aceitos; qualquer outra mensagem levanta ProtocolError. Como a leitura é sequencial, um
    `contexto` é processado antes de qualquer `exec` que venha depois dele. Dentro de uma
    célula, `tool_call` e `tool_result` passam pela ponte que a `serve` cria sobre `entrada`
    e `saida`, a menos que um `repl` já ligado a uma ponte seja passado. Com `rastrear`,
    instala o gancho de auditoria de /dados.
    """
    ponte = Ponte(entrada, saida)
    ambiente = repl if repl is not None else Repl(ponte)
    if rastrear:
        instalar_gancho(ponte)
    while True:
        linha = entrada.readline()
        if linha == "":
            # EOF: o host fechou o canal e o sandbox termina.
            return
        msg: Mensagem = decode(linha)
        if isinstance(msg, Contexto):
            confirmacao = ambiente.carregar_contexto(msg.valor)
            saida.write(encode(confirmacao))
            saida.flush()
            continue
        if not isinstance(msg, Exec):
            raise ProtocolError(
                f"mensagem '{msg.method}' não é aceita pelo sandbox fora de uma célula "
                "(só 'exec' e 'contexto' chegam no nível de topo)"
            )
        resultado = ambiente.run_cell(msg.cell_id, msg.code, msg.timeout_s)
        saida.write(encode(resultado.done))
        saida.flush()


def _serve_por_stdio(rastrear: bool = False) -> None:
    """Modo --stdio: o protocolo usa stdin e stdout, mas o código do modelo não os alcança.

    O Docker só entrega stdin, stdout e stderr ao cliente, não descritores extras. Por isso
    o protocolo passa pelos descritores 0 e 1, duplicados antes da troca. Os fds 0 e 1
    originais são então apontados para /dev/null, e `print` ou `os.write(1, ...)` do
    código do modelo deixam de alcançar o canal. Assim a intenção do D12 é preservada
    dentro do contêiner.

    Limitação: o código do modelo ainda pode achar o descritor duplicado, porque roda no
    mesmo processo. A barreira real é o contêiner (sem rede, usuário sem privilégio e
    sistema de arquivos somente leitura), não esta separação de descritores.
    """
    entrada_fd = os.dup(0)
    saida_fd = os.dup(1)
    nulo = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(nulo, 0)
        os.dup2(nulo, 1)
    finally:
        os.close(nulo)
    with (
        os.fdopen(entrada_fd, "r", encoding="utf-8") as entrada,
        os.fdopen(saida_fd, "w", encoding="utf-8") as saida,
    ):
        serve(entrada, saida, rastrear=rastrear)


def main(argv: list[str] | None = None) -> None:
    """Ponto de entrada do processo do sandbox: abre os descritores e chama serve.

    Com `--fd-in` e `--fd-out` (testes e uso local) o protocolo usa esses descritores.
    Com `--stdio` (contêiner) o protocolo usa stdin e stdout, como explicado em
    `_serve_por_stdio`.
    """
    analisador = argparse.ArgumentParser(description="Runner do REPL dentro do sandbox.")
    analisador.add_argument("--fd-in", type=int, help="descritor de leitura do host")
    analisador.add_argument("--fd-out", type=int, help="descritor de escrita do host")
    analisador.add_argument(
        "--stdio", action="store_true", help="usa stdin e stdout como canal (modo contêiner)"
    )
    analisador.add_argument(
        "--rastrear-arquivos",
        action="store_true",
        help="instala o gancho que avisa o host antes de abrir arquivos de /dados",
    )
    args = analisador.parse_args(argv)
    if args.stdio:
        if args.fd_in is not None or args.fd_out is not None:
            analisador.error("--stdio não combina com --fd-in e --fd-out")
        _serve_por_stdio(rastrear=args.rastrear_arquivos)
        return
    if args.fd_in is None or args.fd_out is None:
        analisador.error("informe --fd-in e --fd-out, ou --stdio")
    with (
        os.fdopen(args.fd_in, "r", encoding="utf-8") as entrada,
        os.fdopen(args.fd_out, "w", encoding="utf-8") as saida,
    ):
        serve(entrada, saida, rastrear=args.rastrear_arquivos)


if __name__ == "__main__":
    main()
