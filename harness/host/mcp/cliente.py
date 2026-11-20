"""Cliente MCP stdio escrito à mão (Tarefa 12): JSON-RPC 2.0, uma mensagem JSON por linha.

Fala a era legada do protocolo (versões 2025-11-25 e anteriores): `initialize`, depois a
notificação `notifications/initialized`, depois `tools/list` e `tools/call`. Não usa o SDK `mcp`.

Decisões deste módulo (não estão na spec):
- Ambiente mínimo: o subprocesso recebe só PATH e HOME do harness, mais as variáveis do `env`
  configurado. O ambiente do harness não é herdado, então a chave do OpenRouter não chega ao
  servidor.
- O servidor roda num grupo de processos próprio. Fechar o cliente sinaliza o grupo inteiro,
  para que um filho do servidor (por exemplo, `uv run` chamando o Python) também morra.
- Leitura numa thread que alimenta uma fila, para a espera ter prazo. A escrita também roda
  numa thread: um servidor que não lê o stdin não trava o harness.
- Qualquer violação do protocolo (linha que não é JSON, id inesperado, linha acima de 4 MiB,
  EOF, timeout) fecha o cliente, porque depois disso o estado do canal não é confiável. Um erro
  JSON-RPC numa requisição válida não fecha: é só uma recusa.
- Requisições do servidor ao cliente são recusadas com `method not found`; notificações são
  ignoradas.
- As mensagens de erro são curtas e genéricas: sem stderr (descartado), sem corpo cru e sem
  caminhos.
"""

import contextlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import IO, Any, Final

from harness.host.mcp.config import ServidorMcp

VERSAO_PROTOCOLO: Final = "2025-11-25"
VERSOES_ACEITAS: Final = frozenset({"2025-11-25", "2025-06-18", "2025-03-26"})
# Tamanho máximo de uma linha (incluindo o \n) que o servidor pode enviar: 4 MiB.
LIMITE_LINHA: Final = 4 * 1024 * 1024
# Teto de páginas de tools/list, para um servidor que devolve cursores sem fim.
LIMITE_PAGINAS: Final = 100
# Variáveis do harness que o servidor recebe. Todo o resto do ambiente fica de fora.
_VARIAVEIS_DO_HARNESS: Final = ("PATH", "HOME")
_ESPERA_ENCERRAMENTO_S: Final = 2.0
_METODO_INEXISTENTE: Final = -32601
_LINHA: Final = "linha"
_EOF: Final = "eof"
_GRANDE: Final = "grande"


class ErroMcp(Exception):
    """Falha de um servidor MCP. A mensagem é curta e genérica, pensada para o modelo."""


@dataclass(frozen=True)
class DescritorFerramenta:
    """Uma ferramenta anunciada por `tools/list`. Nome vazio quando o servidor não mandou texto."""

    nome: str
    descricao: str
    esquema: dict[str, Any] | None


class ClienteMcp:
    """Cliente de um servidor MCP stdio: inicia o processo, faz o handshake e atende às chamadas.

    Construir o cliente já inicia o servidor e faz o `initialize`. Se isso falhar, levanta ErroMcp
    e o processo não fica vivo. Use como context manager ou chame `fechar()`.
    """

    def __init__(self, servidor: ServidorMcp) -> None:
        self._servidor = servidor
        self._lock = threading.Lock()
        self._fila: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._proximo_id = 1
        self._fechado = False
        self._processo, self._entrada, self._saida = self._abrir_processo()
        leitor = threading.Thread(target=self._ler, args=(self._saida, self._fila), daemon=True)
        leitor.start()
        try:
            self._handshake()
        except BaseException:
            self.fechar()
            raise

    def __enter__(self) -> "ClienteMcp":
        return self

    def __exit__(self, *_: object) -> None:
        self.fechar()

    @property
    def pid(self) -> int:
        """PID do processo do servidor (usado pelos testes para conferir o encerramento)."""
        return self._processo.pid

    # --- API pública ----------------------------------------------------------------------

    def listar_ferramentas(self) -> list[DescritorFerramenta]:
        """Todas as ferramentas do servidor, seguindo a paginação por `nextCursor`."""
        descritores: list[DescritorFerramenta] = []
        cursor: str | None = None
        for _ in range(LIMITE_PAGINAS):
            params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
            resultado = self._requisitar("tools/list", params, self._servidor.timeout_chamada_s)
            itens = resultado.get("tools")
            if not isinstance(itens, list):
                raise self._falhar("respondeu a tools/list sem a lista de ferramentas")
            for item in itens:
                if not isinstance(item, dict):
                    raise self._falhar("respondeu a tools/list com um item que não é objeto")
                descritores.append(_descritor(item))
            proximo = resultado.get("nextCursor")
            if proximo is None or proximo == "":
                return descritores
            if not isinstance(proximo, str) or proximo == cursor:
                raise self._falhar("repetiu o cursor da paginação de tools/list")
            cursor = proximo
        raise self._falhar("pagina a lista de ferramentas além do limite")

    def chamar(self, nome: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        """Executa `tools/call` e devolve o `result` cru (com `content` e, às vezes, `isError`)."""
        return self._requisitar(
            "tools/call",
            {"name": nome, "arguments": argumentos},
            self._servidor.timeout_chamada_s,
        )

    def fechar(self) -> None:
        """Encerra o servidor. Idempotente e nunca levanta exceção.

        Envia SIGTERM ao grupo do processo, espera um pouco e manda SIGKILL se ainda estiver vivo.
        O sinal vai antes do `wait`, enquanto o processo ainda não foi colhido, para que o id do
        grupo continue válido.
        """
        if self._fechado:
            return
        self._fechado = True
        with contextlib.suppress(Exception):
            self._sinalizar_grupo(signal.SIGTERM)
            try:
                self._processo.wait(timeout=_ESPERA_ENCERRAMENTO_S)
            except subprocess.TimeoutExpired:
                self._sinalizar_grupo(signal.SIGKILL)
                self._processo.wait(timeout=_ESPERA_ENCERRAMENTO_S)
        self._fechar_fluxos()

    # --- handshake e requisições ----------------------------------------------------------

    def _handshake(self) -> None:
        timeout = self._servidor.timeout_inicio_s
        resposta = self._requisitar(
            "initialize",
            {
                "protocolVersion": VERSAO_PROTOCOLO,
                "capabilities": {},
                "clientInfo": {"name": "harness", "version": "0.0.1"},
            },
            timeout,
        )
        versao = resposta.get("protocolVersion")
        if not isinstance(versao, str) or versao not in VERSOES_ACEITAS:
            raise self._falhar("pediu uma versão de protocolo que o harness não fala")
        with self._lock:
            self._enviar(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                timeout,
            )

    def _requisitar(self, metodo: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self._lock:
            if self._fechado:
                raise ErroMcp(f"o servidor MCP '{self._servidor.nome}' não está mais disponível")
            id_ = self._proximo_id
            self._proximo_id += 1
            self._enviar({"jsonrpc": "2.0", "id": id_, "method": metodo, "params": params}, timeout)
            return self._aguardar(id_, timeout)

    def _enviar(self, mensagem: dict[str, Any], timeout: float) -> None:
        nome = self._servidor.nome
        try:
            dados = json.dumps(mensagem, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            raise ErroMcp(
                f"os argumentos não podem ser enviados ao servidor MCP '{nome}'"
            ) from None
        dados += b"\n"
        erros: list[OSError | ValueError] = []

        def escrever() -> None:
            try:
                self._entrada.write(dados)
                self._entrada.flush()
            except (OSError, ValueError) as erro:
                erros.append(erro)

        escritor = threading.Thread(target=escrever, daemon=True)
        escritor.start()
        escritor.join(timeout)
        if escritor.is_alive():
            raise self._falhar("não respondeu a tempo")
        if erros:
            raise self._falhar("não aceitou a mensagem")

    def _aguardar(self, id_: int, timeout: float) -> dict[str, Any]:
        """Lê linhas até a resposta com o `id_` esperado. Ignora notificações."""
        nome = self._servidor.nome
        prazo = time.monotonic() + timeout
        while True:
            try:
                tipo, dados = self._fila.get(timeout=max(0.0, prazo - time.monotonic()))
            except queue.Empty:
                raise self._falhar("não respondeu a tempo") from None
            if tipo == _EOF:
                raise self._falhar("encerrou antes de responder")
            if tipo == _GRANDE:
                raise self._falhar(f"enviou uma linha acima de {LIMITE_LINHA} bytes")
            try:
                mensagem = json.loads(dados)
            except ValueError:
                raise self._falhar("enviou uma linha que não é JSON") from None
            if not isinstance(mensagem, dict) or mensagem.get("jsonrpc") != "2.0":
                raise self._falhar("enviou uma mensagem fora do JSON-RPC")
            if "method" in mensagem:
                if "id" in mensagem:
                    # Pedido do servidor ao cliente: o harness não oferece nenhum, então recusa.
                    recusa = {
                        "jsonrpc": "2.0",
                        "id": mensagem["id"],
                        "error": {"code": _METODO_INEXISTENTE, "message": "método não suportado"},
                    }
                    self._enviar(recusa, self._servidor.timeout_chamada_s)
                continue
            if not _id_confere(mensagem.get("id"), id_):
                raise self._falhar("respondeu com um id inesperado")
            if "error" in mensagem:
                # Recusa de uma requisição válida: não fecha o cliente, só falha a chamada.
                raise ErroMcp(f"o servidor MCP '{nome}' recusou a requisição")
            resultado = mensagem.get("result")
            if not isinstance(resultado, dict):
                raise self._falhar("respondeu sem um resultado válido")
            return resultado

    def _falhar(self, motivo: str) -> ErroMcp:
        """Fecha o cliente, porque o canal não é mais confiável, e devolve o erro para levantar.

        `motivo` vem sem o prefixo: a mensagem final é "o servidor MCP 'nome' <motivo>".
        """
        self.fechar()
        return ErroMcp(f"o servidor MCP '{self._servidor.nome}' {motivo}")

    # --- processo e fluxos ----------------------------------------------------------------

    def _abrir_processo(
        self,
    ) -> tuple[subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        ambiente = {nome: os.environ[nome] for nome in _VARIAVEIS_DO_HARNESS if nome in os.environ}
        ambiente.update(self._servidor.env)
        try:
            processo = subprocess.Popen(
                [self._servidor.comando, *self._servidor.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=ambiente,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise ErroMcp(
                f"não foi possível iniciar o servidor MCP '{self._servidor.nome}'"
            ) from None
        assert processo.stdin is not None and processo.stdout is not None
        return processo, processo.stdin, processo.stdout

    def _ler(self, fluxo: IO[bytes], fila: "queue.Queue[tuple[str, bytes]]") -> None:
        """Lê uma linha por vez e a põe na fila. Para no EOF, acima do limite ou em erro."""
        while True:
            try:
                linha = fluxo.readline(LIMITE_LINHA)
            except (OSError, ValueError):
                fila.put((_EOF, b""))
                return
            if not linha:
                fila.put((_EOF, b""))
                return
            if not linha.endswith(b"\n"):
                # Sem \n: ou a linha estourou o limite (o readline parou nele) ou o servidor saiu.
                fila.put((_GRANDE if len(linha) >= LIMITE_LINHA else _EOF, b""))
                return
            fila.put((_LINHA, linha))

    def _sinalizar_grupo(self, sinal: int) -> None:
        with contextlib.suppress(OSError):
            os.killpg(self._processo.pid, sinal)

    def _fechar_fluxos(self) -> None:
        """Fecha stdin e stdout numa thread, para um filho com o pipe aberto não travar o fim."""

        def fechar() -> None:
            for fluxo in (self._entrada, self._saida):
                with contextlib.suppress(Exception):
                    fluxo.close()

        fechador = threading.Thread(target=fechar, daemon=True)
        fechador.start()
        fechador.join(_ESPERA_ENCERRAMENTO_S)


def _descritor(item: dict[str, Any]) -> DescritorFerramenta:
    nome = item.get("name")
    descricao = item.get("description")
    esquema = item.get("inputSchema")
    return DescritorFerramenta(
        nome=nome if isinstance(nome, str) else "",
        descricao=descricao if isinstance(descricao, str) else "",
        esquema=esquema if isinstance(esquema, dict) else None,
    )


def _id_confere(recebido: object, esperado: int) -> bool:
    """Igualdade de id que não aceita `true` como 1 (bool é subclasse de int)."""
    return type(recebido) is int and recebido == esperado
