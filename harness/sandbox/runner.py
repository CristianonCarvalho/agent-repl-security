"""Runner do REPL que roda dentro do sandbox (Tarefa 3, D12).

Este módulo executa o código do modelo num namespace persistente, captura stdout e
stderr por célula, transforma exceções em dados e aplica o limite de tempo por thread.
Ainda não há Docker, ferramentas nem FINAL: essas partes entram em tarefas seguintes.

Contrato com o host: o protocolo trafega só pelos objetos `entrada` e `saida` passados
a `serve`. O stdout do código do modelo nunca é escrito neles diretamente.
"""

import argparse
import ctypes
import io
import linecache
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, TextIO

from harness.sandbox.protocol import (
    Exec,
    ExecDone,
    Mensagem,
    ProtocolError,
    decode,
    encode,
)


class _Interrupcao(BaseException):
    """Exceção injetada na thread da célula quando ela estoura o tempo limite.

    Herda de BaseException para não ser apanhada por `except Exception` do código do modelo,
    embora um `except BaseException` ainda possa engoli-la (nesse caso a thread continua viva).
    """


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


class Repl:
    """REPL com namespace persistente: variáveis de uma célula existem nas seguintes."""

    def __init__(self) -> None:
        # Um único dict de globais é reutilizado por todas as células.
        self._globais: dict[str, Any] = {"__name__": "__main__"}

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

        saida_capturada = _Captura()
        erro_capturado = _Captura()
        relato = _Relato()
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
            final=None,
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


def serve(entrada: TextIO, saida: TextIO, repl: Repl | None = None) -> None:
    """Lê mensagens do host linha a linha e responde cada `exec` com um `exec_done`.

    Termina quando `entrada` chega ao fim. Qualquer mensagem que não seja `exec` levanta
    ProtocolError: ferramentas (tool_call e tool_result) entram na Tarefa 5.
    """
    ambiente = repl if repl is not None else Repl()
    while True:
        linha = entrada.readline()
        if linha == "":
            # EOF: o host fechou o canal e o sandbox termina.
            return
        msg: Mensagem = decode(linha)
        if not isinstance(msg, Exec):
            raise ProtocolError(
                f"mensagem '{msg.method}' não é aceita pelo sandbox nesta etapa "
                "(só 'exec' é suportada; ferramentas chegam na Tarefa 5)"
            )
        resultado = ambiente.run_cell(msg.cell_id, msg.code, msg.timeout_s)
        saida.write(encode(resultado.done))
        saida.flush()


def _serve_por_stdio() -> None:
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
        serve(entrada, saida)


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
    args = analisador.parse_args(argv)
    if args.stdio:
        if args.fd_in is not None or args.fd_out is not None:
            analisador.error("--stdio não combina com --fd-in e --fd-out")
        _serve_por_stdio()
        return
    if args.fd_in is None or args.fd_out is None:
        analisador.error("informe --fd-in e --fd-out, ou --stdio")
    with (
        os.fdopen(args.fd_in, "r", encoding="utf-8") as entrada,
        os.fdopen(args.fd_out, "w", encoding="utf-8") as saida,
    ):
        serve(entrada, saida)


if __name__ == "__main__":
    main()
