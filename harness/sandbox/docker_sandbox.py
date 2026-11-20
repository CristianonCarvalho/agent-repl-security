"""Cliente do sandbox no host: cria e controla o contêiner do REPL (spec v0, D4 e D12).

O host cria um contêiner Docker endurecido a partir da imagem `harness-sandbox:dev`, envia
células pelo canal de stdin e stdout (modo `--stdio` do runner) e lê as respostas
`exec_done`. O stderr do contêiner são logs do runner: ficam num buffer de diagnóstico.

Ponte de ferramentas (Tarefa 5): enquanto espera o `exec_done`, o host atende cada
`tool_call` pelo despachante recebido em `executar` e responde com `tool_result`. As
ferramentas executam aqui, no host; o contêiner só troca mensagens.

Endurecimento aplicado na criação do contêiner: sem rede, sistema de arquivos somente
leitura, tmpfs efêmeros para /tmp e /saida, sem capacidades Linux, usuário 10001,
`no-new-privileges`, sem swap, limites de processos, CPU e memória, e só os diretórios de
`dados` montados em modo somente leitura. O host nunca monta o socket do Docker e nunca
usa `privileged`.

Heurísticas do host (decisões deste módulo, não da spec):
- Reinício controlado: quando o runner não consegue interromper a célula, o `exec_done`
  vem com `reiniciar` verdadeiro. O host decide por esse campo do protocolo, nunca pelo
  texto da exceção, que pode conter qualquer coisa, inclusive o código do modelo. Ao
  receber `reiniciar` verdadeiro, mata e recria o contêiner (`reiniciar()`) e conta o
  reinício em `reinicios`. A célula é devolvida mesmo assim, com a exceção (que cita a
  frase `MARCA_REINICIO` para o modelo), e o modelo fica sabendo que as variáveis se perderam.
- Morte do contêiner: o canal chegar ao fim antes da resposta é tratado como morte. O
  motivo vem de `container.attrs["State"]` (OOMKilled, ExitCode, Status) e do diagnóstico.
- Sem resposta no prazo: `timeout_s` + MARGEM_S. Passado esse prazo, o contêiner é morto.
- Contexto: `carregar_contexto` guarda o valor no host e o reenvia em todo reinício, para que
  `context` sobreviva ao REPL novo. O teto (LIMITE_CONTEXTO_BYTES) é conferido antes do envio.
  Um valor fora do teto nunca chega ao contêiner, e a mensagem de erro não traz o conteúdo.
"""

import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final

import docker
from docker.errors import DockerException
from docker.types import Mount
from docker.utils.socket import STDERR, STDOUT, frames_iter

from harness.sandbox.contexto import ValorContexto, bytes_do_contexto, descrever_contexto
from harness.sandbox.protocol import (
    LIMITE_CHAMADAS_POR_CELULA,
    Contexto,
    ContextoOk,
    Exec,
    ExecDone,
    Mensagem,
    ProtocolError,
    ToolCall,
    ToolResult,
    decode,
    encode,
)

IMAGEM_PADRAO: Final = "harness-sandbox:dev"
# Rótulo que marca os contêineres criados por este código. Só eles podem ser removidos.
ROTULO: Final = "harness-sandbox"
# uid e gid do usuário sem privilégio dentro da imagem (ver o Dockerfile).
USUARIO: Final = "10001:10001"
PIDS_MAX: Final = 128
# Margem sobre o timeout da célula: o runner já aplica o limite; o host só espera mais isso.
MARGEM_S: Final = 3.0
# Uma linha do protocolo acima deste tamanho é tratada como violação e o contêiner é descartado.
LIMITE_LINHA: Final = 16 * 1024 * 1024
# Teto do contexto do pedido, medido como JSON em bytes (ver `bytes_do_contexto`). Acima disso
# o carregamento é recusado antes de qualquer envio ao contêiner.
LIMITE_CONTEXTO_BYTES: Final = 2 * 1024 * 1024
# Prazo para o runner confirmar a carga do contexto (o JSON de 2 MiB chega em milissegundos).
PRAZO_CONTEXTO_S: Final = 30.0
# Quanto do stderr do runner fica guardado para diagnóstico.
TAMANHO_DIAGNOSTICO: Final = 16 * 1024
# Frase que o runner põe no texto da exceção quando não consegue interromper a célula
# (runner.py, run_cell). É texto para o modelo; o host não decide reinício por ela.
MARCA_REINICIO: Final = "O REPL precisa ser reiniciado"
_NOME_DADOS: Final = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ResultadoExecucao:
    """Uma célula executada: o `exec_done` e a contagem de ferramentas chamadas nela.

    `ferramentas` mapeia o nome de cada ferramenta ao número de chamadas recebidas nesta
    célula. `reiniciado` é verdadeiro quando o contêiner foi recriado depois da célula (o
    REPL perdeu as variáveis, exceto `context`, que o host restaura). A instância é congelada
    (o campo não pode ser trocado); o dict em si fica como o chamador o recebeu.
    """

    done: ExecDone
    ferramentas: dict[str, int]
    reiniciado: bool = False


class SandboxErro(Exception):
    """Falha do sandbox que impede continuar com o contêiner atual."""


class SandboxMorreu(SandboxErro):
    """O contêiner terminou antes de responder (OOM, saída do processo ou canal fechado)."""


class SandboxTimeout(SandboxErro):
    """O contêiner não respondeu dentro do prazo e foi morto pelo host."""


class _Leitor:
    """Thread que lê os frames do canal do contêiner.

    Frames de stdout trazem o NDJSON do protocolo: viram linhas na fila `linhas`. Frames de
    stderr são logs do runner e vão para o buffer de diagnóstico, com tamanho limitado.
    Quando o canal chega ao fim, põe `None` na fila.
    """

    def __init__(self, canal: Any) -> None:
        self._canal = canal
        self.linhas: queue.Queue[bytes | None] = queue.Queue()
        self.estourou_linha = False
        self._diagnostico = bytearray()
        self._trava = threading.Lock()
        self._thread = threading.Thread(target=self._ler, name="leitor-sandbox", daemon=True)

    def iniciar(self) -> None:
        self._thread.start()

    def diagnostico(self) -> str:
        """Últimos bytes do stderr do runner, já decodificados."""
        with self._trava:
            return bytes(self._diagnostico).decode("utf-8", errors="replace")

    def _anotar(self, dados: bytes) -> None:
        with self._trava:
            self._diagnostico.extend(dados)
            excesso = len(self._diagnostico) - TAMANHO_DIAGNOSTICO
            if excesso > 0:
                del self._diagnostico[:excesso]

    def _ler(self) -> None:
        pendente = bytearray()
        try:
            for fluxo, dados in frames_iter(self._canal, tty=False):
                if fluxo == STDERR:
                    self._anotar(dados)
                elif fluxo == STDOUT:
                    pendente.extend(dados)
                    while (pos := pendente.find(b"\n")) >= 0:
                        self.linhas.put(bytes(pendente[:pos]))
                        del pendente[: pos + 1]
                    if len(pendente) > LIMITE_LINHA:
                        self.estourou_linha = True
                        break
        except (OSError, ValueError):
            # Canal fechado pelo host ou contêiner que sumiu: é o mesmo que chegar ao fim.
            pass
        finally:
            self.linhas.put(None)


class DockerSandbox:
    """Contêiner do REPL controlado pelo host. Use como context manager ou chame close()."""

    def __init__(
        self,
        imagem: str = IMAGEM_PADRAO,
        dados: dict[str, Path] | None = None,
        memoria: str = "512m",
        tmp_mb: int = 64,
        saida_mb: int = 64,
    ) -> None:
        self._imagem = imagem
        self._memoria = memoria
        self._tmp_mb = tmp_mb
        self._saida_mb = saida_mb
        self._montagens = _preparar_dados(dados or {})
        self._cliente: Any = None
        self._conteiner: Any = None
        self._canal: Any = None
        self._leitor: _Leitor | None = None
        self._fechado = False
        # Contexto do pedido guardado no host: é reenviado ao REPL a cada reinício.
        self._contexto: ValorContexto | None = None
        # Quantas vezes o contêiner foi recriado por reiniciar() (incluindo o controlado).
        self.reinicios = 0

    # --- ciclo de vida ----------------------------------------------------------

    def start(self) -> None:
        """Cria e inicia o contêiner. Só pode ser chamado uma vez, antes de qualquer célula."""
        if self._fechado:
            raise SandboxErro("o sandbox já foi fechado")
        if self._conteiner is not None:
            raise SandboxErro("o sandbox já foi iniciado")
        self._criar_e_iniciar()

    def reiniciar(self) -> None:
        """Mata e recria o contêiner. As variáveis do REPL se perdem, menos `context`.

        Se um contexto foi carregado, ele é reenviado ao REPL novo antes de a função voltar.
        """
        if self._fechado:
            raise SandboxErro("o sandbox já foi fechado")
        self._descartar()
        self._criar_e_iniciar()
        self.reinicios += 1
        if self._contexto is not None:
            self._enviar_contexto(self._contexto)

    def carregar_contexto(self, valor: ValorContexto) -> None:
        """Envia o contexto do pedido ao REPL, como `context`, e guarda o valor no host.

        O valor é dado, nunca código. Se o JSON canônico passar de LIMITE_CONTEXTO_BYTES, a
        carga é recusada antes do envio e a mensagem de erro não traz o conteúdo. Um segundo
        carregamento substitui o primeiro, no REPL e no host. Falhas de envio, morte do
        contêiner e prazo seguem o mesmo tratamento das células.
        """
        if self._fechado:
            raise SandboxErro("o sandbox já foi fechado")
        if self._conteiner is None or self._leitor is None:
            raise SandboxErro("o sandbox não está ativo: chame start() ou reiniciar()")
        tamanho = bytes_do_contexto(valor)
        if tamanho > LIMITE_CONTEXTO_BYTES:
            raise SandboxErro(
                f"o contexto tem {tamanho} bytes e passa do limite de {LIMITE_CONTEXTO_BYTES} "
                "bytes; ele não foi carregado"
            )
        self._enviar_contexto(valor)
        self._contexto = valor

    def close(self) -> None:
        """Para e remove o contêiner. Chamadas repetidas não fazem nada."""
        if self._conteiner is not None:
            try:
                self._conteiner.stop(timeout=2)
            except (DockerException, OSError):
                pass  # já parado ou sumido: a remoção abaixo resolve
        self._descartar()
        self._fechado = True

    def __enter__(self) -> "DockerSandbox":
        self.start()
        return self

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        valor: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- execução ---------------------------------------------------------------

    def executar(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ExecDone:
        """Envia uma célula e devolve o `exec_done`. Mesma assinatura de sempre.

        Não conta as ferramentas chamadas; para isso use `executar_detalhado`.
        """
        return self.executar_detalhado(cell_id, code, timeout_s, despachante).done

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        """Envia uma célula, espera o `exec_done` com o mesmo `cell_id` e conta as ferramentas.

        Cada `tool_call` recebido enquanto a célula roda é entregue a `despachante`, e a
        resposta volta ao contêiner como `tool_result`. Sem despachante, a chamada recebe
        erro "ferramentas indisponíveis". O prazo `timeout_s` + MARGEM_S vale durante as
        chamadas também.

        A contagem `ferramentas` inclui todas as chamadas que chegaram nesta célula, inclusive
        as recusadas pelo teto de LIMITE_CHAMADAS_POR_CELULA (que não vão ao despachante).

        Levanta SandboxMorreu se o contêiner terminar antes da resposta e SandboxTimeout se
        ele não responder dentro de `timeout_s` + MARGEM_S (nesse caso o contêiner é morto).
        Se o runner pedir reinício, o contêiner é recriado antes de devolver a célula.
        """
        if self._conteiner is None or self._leitor is None:
            raise SandboxErro("o sandbox não está ativo: chame start() ou reiniciar()")
        quadro = encode(Exec(cell_id=cell_id, code=code, timeout_s=timeout_s))
        self._enviar(quadro.encode("utf-8"))
        prazo = time.monotonic() + timeout_s + MARGEM_S
        leitor = self._leitor
        chamadas_feitas = 0
        ferramentas: dict[str, int] = {}
        while True:
            restante = prazo - time.monotonic()
            if restante <= 0:
                self._descartar()
                raise SandboxTimeout(
                    f"sem resposta da célula {cell_id} em {timeout_s + MARGEM_S:g} s; "
                    "o contêiner foi morto"
                )
            try:
                linha = leitor.linhas.get(timeout=restante)
            except queue.Empty:
                continue
            if linha is None:
                raise self._consumir_morte()
            msg = self._decodificar(linha)
            if isinstance(msg, ToolCall):
                chamadas_feitas += 1
                ferramentas[msg.name] = ferramentas.get(msg.name, 0) + 1
                resposta = _resposta_a_ferramenta(msg, despachante, chamadas_feitas)
                self._enviar(encode(resposta).encode("utf-8"))
                continue
            if isinstance(msg, ExecDone):
                if msg.cell_id != cell_id:
                    # Resposta de célula anterior: não é a que esperamos, então é descartada.
                    continue
                if msg.reiniciar:
                    self.reiniciar()
                    return ResultadoExecucao(done=msg, ferramentas=ferramentas, reiniciado=True)
                return ResultadoExecucao(done=msg, ferramentas=ferramentas)
            self._descartar()
            raise SandboxErro(
                f"mensagem '{msg.method}' inesperada do sandbox nesta etapa; contêiner descartado"
            )

    def _enviar_contexto(self, valor: ValorContexto) -> None:
        """Envia `contexto`, espera o `contexto_ok` e confere tipo e tamanho com o host."""
        self._enviar(encode(Contexto(valor=valor)).encode("utf-8"))
        confirmacao = self._aguardar_contexto_ok()
        descricao = descrever_contexto(valor)
        if (confirmacao.tipo, confirmacao.tamanho) != (descricao.tipo, descricao.tamanho):
            self._descartar()
            raise SandboxErro(
                "o REPL confirmou um contexto diferente do enviado; contêiner descartado"
            )

    def _aguardar_contexto_ok(self) -> ContextoOk:
        """Lê a próxima mensagem do canal, que precisa ser o `contexto_ok`, dentro do prazo."""
        leitor = self._leitor
        if leitor is None:
            raise SandboxErro("o sandbox não está ativo: chame start() ou reiniciar()")
        prazo = time.monotonic() + PRAZO_CONTEXTO_S
        while True:
            restante = prazo - time.monotonic()
            if restante <= 0:
                self._descartar()
                raise SandboxTimeout(
                    f"sem confirmação do contexto em {PRAZO_CONTEXTO_S:g} s; o contêiner foi morto"
                )
            try:
                linha = leitor.linhas.get(timeout=restante)
            except queue.Empty:
                continue
            if linha is None:
                raise self._consumir_morte()
            msg = self._decodificar(linha)
            if isinstance(msg, ContextoOk):
                return msg
            self._descartar()
            raise SandboxErro(
                f"mensagem '{msg.method}' inesperada ao carregar o contexto; contêiner descartado"
            )

    def _decodificar(self, linha: bytes) -> Mensagem:
        try:
            return decode(linha.decode("utf-8"))
        except (UnicodeDecodeError, ProtocolError) as erro:
            self._descartar()
            raise SandboxErro(
                f"linha inválida do protocolo; contêiner descartado: {erro}"
            ) from erro

    def _enviar(self, quadro: bytes) -> None:
        try:
            self._canal.sendall(quadro)
        except OSError as erro:
            raise self._consumir_morte() from erro

    # --- internos ---------------------------------------------------------------

    def _criar_e_iniciar(self) -> None:
        if self._cliente is None:
            self._cliente = docker.from_env()
        montagens: list[Mount] = list(self._montagens)
        try:
            conteiner = self._cliente.containers.create(
                self._imagem,
                detach=True,
                stdin_open=True,
                init=True,
                network_mode="none",
                read_only=True,
                tmpfs={
                    "/tmp": f"rw,noexec,nosuid,nodev,size={self._tmp_mb}m,mode=1777",
                    "/saida": (
                        f"rw,noexec,nosuid,nodev,size={self._saida_mb}m,"
                        "uid=10001,gid=10001,mode=0700"
                    ),
                },
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user=USUARIO,
                privileged=False,
                mem_limit=self._memoria,
                memswap_limit=self._memoria,
                pids_limit=PIDS_MAX,
                nano_cpus=1_000_000_000,
                mounts=montagens,
                labels={ROTULO: "1"},
            )
            self._conteiner = conteiner
            conteiner.start()
            # stdout e stderr são pedidos para receber protocolo e logs; o stdin, para as células.
            canal = conteiner.attach_socket(
                params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
            )
        except DockerException:
            self._descartar()
            raise
        # O SDK entrega um SocketIO só de leitura; a escrita (stdin) usa o socket por baixo.
        self._canal = canal._sock
        self._leitor = _Leitor(self._canal)
        self._leitor.iniciar()

    def _consumir_morte(self) -> SandboxMorreu:
        """Coleta o motivo da morte, descarta o contêiner e devolve a exceção a levantar."""
        motivo = self._motivo_morte()
        self._descartar()
        return SandboxMorreu(motivo)

    def _motivo_morte(self) -> str:
        if self._leitor is not None and self._leitor.estourou_linha:
            return "linha do protocolo acima do limite; contêiner descartado"
        estado: dict[str, Any] = {}
        conteiner = self._conteiner
        if conteiner is not None:
            try:
                conteiner.wait(timeout=5)
            except (DockerException, OSError):
                pass  # ainda rodando ou sumido: o estado abaixo mostra o que houver
            try:
                conteiner.reload()
                estado = dict(conteiner.attrs.get("State", {}))
            except (DockerException, OSError):
                pass
        detalhes = (
            f"status={estado.get('Status')}, código de saída={estado.get('ExitCode')}, "
            f"OOMKilled={estado.get('OOMKilled')}"
        )
        if estado.get("Error"):
            detalhes += f", erro={estado['Error']}"
        motivo = f"o contêiner do sandbox terminou antes de responder ({detalhes})"
        diagnostico = self._leitor.diagnostico().strip() if self._leitor is not None else ""
        if diagnostico:
            motivo += f"; diagnóstico: {diagnostico[-500:]}"
        return motivo

    def _descartar(self) -> None:
        """Remove o contêiner e os recursos do canal. Seguro para ser chamado repetidamente."""
        conteiner, canal = self._conteiner, self._canal
        self._conteiner = None
        self._canal = None
        self._leitor = None
        if canal is not None:
            try:
                canal.close()
            except OSError:
                pass
        if conteiner is not None:
            try:
                conteiner.remove(force=True)
            except (DockerException, OSError):
                pass  # já removido


def _resposta_a_ferramenta(
    chamada: ToolCall,
    despachante: Callable[[ToolCall], ToolResult] | None,
    numero: int,
) -> ToolResult:
    """Decide a resposta do host a uma `tool_call` da célula em curso.

    `numero` é a posição da chamada dentro da célula (a partir de 1). O teto de
    LIMITE_CHAMADAS_POR_CELULA é reforçado aqui, no host, além do sandbox. A chamada
    recusada nunca chega ao despachante. Falhas do despachante viram erro sem detalhes.
    """
    if numero > LIMITE_CHAMADAS_POR_CELULA:
        return ToolResult(
            call_id=chamada.call_id,
            ok=False,
            error=(
                f"limite de {LIMITE_CHAMADAS_POR_CELULA} chamadas de ferramenta por célula "
                "excedido; a chamada foi recusada pelo host"
            ),
        )
    if despachante is None:
        return ToolResult(call_id=chamada.call_id, ok=False, error="ferramentas indisponíveis")
    try:
        resultado = despachante(chamada)
    except Exception:
        # O despachante não deveria levantar; se levantar, o detalhe fica no host.
        return ToolResult(
            call_id=chamada.call_id,
            ok=False,
            error="falha interna do host ao atender a ferramenta",
        )
    if resultado.call_id != chamada.call_id:
        return ToolResult(
            call_id=chamada.call_id,
            ok=False,
            error="falha interna do host: resposta de outra chamada",
        )
    return resultado


def _preparar_dados(dados: dict[str, Path]) -> list[Mount]:
    """Valida os diretórios de `dados` e monta cada um em /dados/<nome>, somente leitura."""
    montagens: list[Mount] = []
    for nome, caminho in dados.items():
        if not _NOME_DADOS.fullmatch(nome):
            raise ValueError(f"nome de dados inválido: {nome!r} (use letras, dígitos, _ ou -)")
        origem = caminho.resolve()
        if not origem.is_dir():
            raise ValueError(f"dados {nome!r} não é um diretório: {origem}")
        montagens.append(
            Mount(target=f"/dados/{nome}", source=str(origem), type="bind", read_only=True)
        )
    return montagens
