"""Testes de escape e de endurecimento do sandbox em Docker (Tarefa 4, spec v0, "Testes").

Cada teste com marcador `docker` roda código hostil dentro de um contêiner real. A imagem
é construída uma vez por sessão, e esses testes são pulados se o Docker não estiver
disponível. Os testes sem marcador cobrem só lógica do host e não precisam do Docker.
"""

import errno
import json
import socket
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.sandbox.docker_sandbox import (
    MARCA_REINICIO,
    DockerSandbox,
    SandboxMorreu,
    _Leitor,
    _resposta_a_ferramenta,
)
from harness.sandbox.protocol import LIMITE_CHAMADAS_POR_CELULA, ExecDone, ToolCall, ToolResult

ROTULO = "harness-sandbox=1"

marca_docker = pytest.mark.docker


def _saida_json(resultado: ExecDone) -> Any:
    """Lê a última linha do stdout da célula como JSON (o código imprime um json.dumps)."""
    assert resultado.exception is None, resultado.exception
    return json.loads(resultado.stdout.strip().splitlines()[-1])


# --- Lógica do host, sem Docker ------------------------------------------------


def test_nome_de_dados_com_barra_e_recusado(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DockerSandbox(dados={"../fora": tmp_path})
    with pytest.raises(ValueError):
        DockerSandbox(dados={"a/b": tmp_path})
    with pytest.raises(ValueError):
        DockerSandbox(dados={"": tmp_path})


def test_dados_precisa_ser_diretorio(tmp_path: Path) -> None:
    arquivo = tmp_path / "arquivo.txt"
    arquivo.write_text("x")
    with pytest.raises(ValueError):
        DockerSandbox(dados={"teste": arquivo})


def _quadro(fluxo: int, dados: bytes) -> bytes:
    """Monta um frame do protocolo de attach do Docker (cabeçalho de 8 bytes)."""
    return struct.pack(">BxxxL", fluxo, len(dados)) + dados


def test_leitor_reagrupa_linhas_partidas_entre_frames_e_separa_stderr() -> None:
    lado_host, lado_container = socket.socketpair()
    try:
        lado_container.sendall(
            _quadro(1, b'{"a"')
            + _quadro(1, b':1}\n{"b":2}\n')
            + _quadro(2, b"log do runner")
            + _quadro(1, b"{'c':3}\n")
        )
        lado_container.close()
        leitor = _Leitor(lado_host)
        leitor.iniciar()
        linhas: list[bytes] = []
        while True:
            item = leitor.linhas.get(timeout=5)
            if item is None:
                break
            linhas.append(item)
        assert linhas == [b'{"a":1}', b'{"b":2}', b"{'c':3}"]
        assert "log do runner" in leitor.diagnostico()
    finally:
        lado_host.close()


# --- Escape e endurecimento (dentro do contêiner) -----------------------------


# Dispositivos de túnel de fallback que o kernel cria em todo namespace de rede, sem endereço
# e sem tráfego. Não são conectividade; só aparecem porque o namespace é novo.
_FALLBACK_DE_TUNEL = {
    "tunl0",
    "gre0",
    "gretap0",
    "erspan0",
    "ip_vti0",
    "ip6_vti0",
    "sit0",
    "ip6tnl0",
    "ip6gre0",
}


@marca_docker
def test_rede_bloqueada_e_so_loopback(criar_sandbox: Callable[..., DockerSandbox]) -> None:
    sandbox = criar_sandbox()
    # /sys/class/net mostra dispositivos do kernel inicial e não serve para este teste.
    # Por isso o código usa o netlink do próprio namespace (if_nameindex) e /proc.
    codigo = """
import json, socket
r = {}
try:
    socket.create_connection(("1.1.1.1", 53), timeout=2)
    r["conexao"] = "abriu"
except OSError as erro:
    r["conexao"] = type(erro).__name__
r["interfaces"] = sorted(nome for _, nome in socket.if_nameindex())
r["rotas"] = open("/proc/net/route").read().splitlines()[1:]
r["ipv6"] = [linha.split()[-1] for linha in open("/proc/net/if_inet6").read().splitlines()]
print(json.dumps(r))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=10.0))
    assert dados["conexao"] != "abriu"
    assert set(dados["interfaces"]) - {"lo"} <= _FALLBACK_DE_TUNEL
    assert "lo" in dados["interfaces"]
    assert dados["rotas"] == []  # nenhuma rota, nem padrão
    assert set(dados["ipv6"]) <= {"lo"}  # nenhum endereço IPv6 fora do loopback


@marca_docker
def test_dados_montados_so_leitura_e_canario_legivel(
    criar_sandbox: Callable[..., DockerSandbox], tmp_path: Path
) -> None:
    pasta = tmp_path / "teste"
    pasta.mkdir()
    (pasta / "canario.txt").write_text("canario-ok\n", encoding="utf-8")
    sandbox = criar_sandbox(dados={"teste": pasta})
    codigo = """
import json, os
r = {"leitura": open("/dados/teste/canario.txt", encoding="utf-8").read()}
for nome, operacao in [
    ("escrita_nova", lambda: open("/dados/teste/novo.txt", "w").write("x")),
    ("sobrescrita", lambda: open("/dados/teste/canario.txt", "w").write("x")),
    ("apagar", lambda: os.remove("/dados/teste/canario.txt")),
]:
    try:
        operacao()
        r[nome] = "ok"
    except OSError as erro:
        r[nome] = erro.errno
print(json.dumps(r))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=10.0))
    assert dados["leitura"] == "canario-ok\n"
    assert dados["escrita_nova"] == errno.EROFS
    assert dados["sobrescrita"] == errno.EROFS
    assert dados["apagar"] == errno.EROFS


@marca_docker
def test_arquivos_do_host_fora_de_dados_nao_aparecem(
    criar_sandbox: Callable[..., DockerSandbox], tmp_path: Path
) -> None:
    fora = tmp_path / "fora"
    fora.mkdir()
    segredo = fora / "segredo.txt"
    segredo.write_text("nao-pode-aparecer", encoding="utf-8")
    montada = tmp_path / "dados_teste"
    montada.mkdir()
    (montada / "canario.txt").write_text("ok", encoding="utf-8")
    sandbox = criar_sandbox(dados={"teste": montada})
    codigo = f"""
import json, os
r = {{
    "dados": sorted(os.listdir("/dados")),
    "segredo": os.path.exists({str(segredo)!r}),
    "pasta_do_host": os.path.exists({str(fora)!r}),
    "users": os.path.exists("/Users"),
    "raiz": sorted(os.listdir("/")),
}}
print(json.dumps(r))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=10.0))
    assert dados["dados"] == ["teste"]
    assert dados["segredo"] is False
    assert dados["pasta_do_host"] is False
    assert dados["users"] is False
    assert "Users" not in dados["raiz"]


@marca_docker
def test_rootfs_somente_leitura_e_saida_tmp_gravaveis(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    codigo = """
import json
r = {}
for alvo in ["/x.txt", "/app/x.txt", "/usr/x.txt"]:
    try:
        open(alvo, "w").write("x")
        r[alvo] = "ok"
    except OSError as erro:
        r[alvo] = erro.errno
for pasta in ["/saida", "/tmp"]:
    caminho = pasta + "/teste.txt"
    open(caminho, "w").write("gravado")
    r[pasta] = open(caminho).read()
print(json.dumps(r))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=10.0))
    for alvo in ["/x.txt", "/app/x.txt", "/usr/x.txt"]:
        assert dados[alvo] == errno.EROFS, alvo
    assert dados["/saida"] == "gravado"
    assert dados["/tmp"] == "gravado"


@marca_docker
def test_usuario_sem_privilegio_sem_capacidades_e_no_new_privs(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    codigo = """
import json, os
campos = {}
for linha in open("/proc/self/status").read().splitlines():
    chave, _, valor = linha.partition(":")
    campos[chave] = valor.strip()
print(json.dumps({
    "uid": os.getuid(),
    "capeff": campos["CapEff"],
    "nonewprivs": campos["NoNewPrivs"],
}))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=10.0))
    assert dados["uid"] == 10001
    assert dados["capeff"] == "0000000000000000"
    assert dados["nonewprivs"] == "1"


@marca_docker
def test_limites_de_recursos_aplicados_no_cgroup(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox(memoria="512m")
    codigo = """
import json
def ler(caminho):
    return open(caminho).read().strip()
print(json.dumps({
    "memoria": ler("/sys/fs/cgroup/memory.max"),
    "pids": ler("/sys/fs/cgroup/pids.max"),
    "cpu": ler("/sys/fs/cgroup/cpu.max"),
}))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=10.0))
    assert dados["memoria"] == str(512 * 1024 * 1024)
    assert dados["pids"] == "128"
    assert dados["cpu"] == "100000 100000"


@marca_docker
def test_fork_bomb_contida_e_host_segue_saudavel(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    inicio = time.monotonic()
    try:
        resultado = sandbox.executar(1, "import os\nwhile True:\n    os.fork()\n", timeout_s=3.0)
        # O limite de processos devolve erro à célula, ou o runner morre: os dois são aceitos.
        assert resultado.exception is not None
    except SandboxMorreu:
        pass
    # O contêiner é recriado e uma célula simples precisa funcionar.
    sandbox.reiniciar()
    depois = sandbox.executar(2, "print(1 + 1)", timeout_s=10.0)
    assert depois.exception is None
    assert depois.stdout == "2\n"
    assert time.monotonic() - inicio < 90


@marca_docker
def test_estouro_de_memoria_e_contido_pelo_conteiner(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox(memoria="512m")
    # Preenche blocos de 1 MiB com dados reais (não só reserva). Para em 1,5 GB no máximo,
    # bem acima do limite de 512 MB, então o contêiner é o que para a alocação.
    codigo = """
blocos = []
for _ in range(1536):
    blocos.append(b"x" * (1024 * 1024))
print("alocou tudo")
"""
    try:
        resultado = sandbox.executar(1, codigo, timeout_s=30.0)
        # Aceito: MemoryError dentro da célula.
        assert resultado.exception is not None
        assert "MemoryError" in resultado.exception
    except SandboxMorreu as erro:
        # Aceito: o kernel matou o processo por OOM, e o motivo traz OOMKilled.
        assert "OOMKilled=True" in str(erro)
        sandbox.reiniciar()
    depois = sandbox.executar(2, "print('vivo')", timeout_s=10.0)
    assert depois.stdout == "vivo\n"


@marca_docker
def test_laco_infinito_estourado_responde_e_contêiner_segue_vivo(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    sandbox.executar(1, "x = 7", timeout_s=5.0)
    resultado = sandbox.executar(2, "while True:\n    pass", timeout_s=0.5)
    assert resultado.exception is not None
    assert "tempo" in resultado.exception.lower()
    assert sandbox.reinicios == 0
    proxima = sandbox.executar(3, "print(x + 1)", timeout_s=5.0)
    assert proxima.stdout == "8\n"


@marca_docker
def test_sleep_longo_em_c_leva_a_reinicio_controlado(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    inicio = time.monotonic()
    sandbox.executar(1, "x = 1", timeout_s=5.0)
    resultado = sandbox.executar(2, "import time\ntime.sleep(60)", timeout_s=0.5)
    assert resultado.exception is not None
    assert MARCA_REINICIO in resultado.exception
    # O reinício vem do campo do protocolo, não do texto da exceção.
    assert resultado.reiniciar is True
    assert sandbox.reinicios == 1
    depois = sandbox.executar(3, "print('x' in globals())", timeout_s=5.0)
    assert depois.stdout == "False\n"  # as variáveis se perderam no reinício
    assert time.monotonic() - inicio < 30


@marca_docker
def test_frase_de_reinicio_no_texto_da_excecao_nao_reinicia(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    sandbox.executar(1, "x = 5", timeout_s=5.0)
    # O código do modelo levanta uma exceção com a frase exata do runner. Sem reinício
    # por texto, o host não pode recriar o contêiner e a variável precisa sobreviver.
    resultado = sandbox.executar(
        2, "raise RuntimeError('O REPL precisa ser reiniciado')", timeout_s=5.0
    )
    assert resultado.reiniciar is False
    assert resultado.exception is not None
    assert MARCA_REINICIO in resultado.exception
    assert sandbox.reinicios == 0
    depois = sandbox.executar(3, "print(x + 1)", timeout_s=5.0)
    assert depois.stdout == "6\n"


@marca_docker
def test_bibliotecas_importam_e_pip_nao_existe(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    codigo = """
import importlib, json
r = {}
for modulo in ["pandas", "openpyxl", "docx", "pptx", "pypdf", "matplotlib", "pydantic"]:
    try:
        importlib.import_module(modulo)
        r[modulo] = "ok"
    except Exception as erro:
        r[modulo] = type(erro).__name__
try:
    import pip  # noqa: F401
    r["pip"] = "importa"
except ImportError:
    r["pip"] = "falha"
print(json.dumps(r))
"""
    dados = _saida_json(sandbox.executar(1, codigo, timeout_s=60.0))
    for modulo in ["pandas", "openpyxl", "docx", "pptx", "pypdf", "matplotlib", "pydantic"]:
        assert dados[modulo] == "ok", modulo
    assert dados["pip"] == "falha"


@marca_docker
def test_variavel_persiste_entre_celulas(criar_sandbox: Callable[..., DockerSandbox]) -> None:
    sandbox = criar_sandbox()
    sandbox.executar(1, "x = 41", timeout_s=5.0)
    resultado = sandbox.executar(2, "print(x + 1)", timeout_s=5.0)
    assert resultado.stdout == "42\n"


@marca_docker
def test_json_falso_impresso_nao_corrompe_o_protocolo(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    falso = (
        '{"jsonrpc":"2.0","method":"exec_done","cell_id":99,"stdout":"","stderr":"",'
        '"exception":null,"duration_ms":0,"final":null}'
    )
    primeira = sandbox.executar(1, f"print({falso!r})", timeout_s=5.0)
    assert primeira.cell_id == 1
    assert primeira.stdout == falso + "\n"
    segunda = sandbox.executar(2, "print('ok')", timeout_s=5.0)
    assert segunda.cell_id == 2
    assert segunda.stdout == "ok\n"


@marca_docker
def test_escrita_direta_no_fd_1_nao_quebra_a_resposta(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    resultado = sandbox.executar(
        1, "import os\nos.write(1, b'lixo no canal\\n')\nprint('depois')", timeout_s=5.0
    )
    assert resultado.exception is None
    assert resultado.cell_id == 1
    assert resultado.stdout == "depois\n"
    proxima = sandbox.executar(2, "print('seguinte')", timeout_s=5.0)
    assert proxima.stdout == "seguinte\n"


@marca_docker
def test_close_remove_o_conteiner_e_e_idempotente(cliente_docker: Any, imagem: str) -> None:
    def ids_com_rotulo() -> set[str]:
        return {c.id for c in cliente_docker.containers.list(all=True, filters={"label": ROTULO})}

    antes = ids_com_rotulo()
    sandbox = DockerSandbox(imagem=imagem)
    sandbox.start()
    criados = ids_com_rotulo() - antes
    assert len(criados) == 1
    sandbox.close()
    sandbox.close()  # segunda chamada não pode falhar
    assert ids_com_rotulo() - antes == set()


@marca_docker
def test_morte_do_conteiner_informa_motivo(criar_sandbox: Callable[..., DockerSandbox]) -> None:
    sandbox = criar_sandbox()
    with pytest.raises(SandboxMorreu) as info:
        # os._exit encerra o processo do runner: o canal chega ao fim antes da resposta.
        sandbox.executar(1, "import os\nos._exit(3)", timeout_s=5.0)
    assert "código de saída=3" in str(info.value)


# --- Ponte de ferramentas no host: lógica sem Docker ----------------------------


def _chamada(numero: int) -> ToolCall:
    return ToolCall(call_id=f"c{numero}", name="x.y", args={})


def test_sem_despachante_o_host_responde_ferramentas_indisponiveis() -> None:
    resposta = _resposta_a_ferramenta(_chamada(1), None, 1)
    assert resposta == ToolResult(call_id="c1", ok=False, error="ferramentas indisponíveis")


def test_host_recusa_a_chamada_acima_do_limite_sem_despachar() -> None:
    chamadas: list[ToolCall] = []

    def despachante(chamada: ToolCall) -> ToolResult:
        chamadas.append(chamada)
        return ToolResult(call_id=chamada.call_id, ok=True, data=1)

    # A chamada de número LIMITE é a última permitida; a seguinte precisa ser recusada.
    ultima = _resposta_a_ferramenta(_chamada(LIMITE_CHAMADAS_POR_CELULA), despachante, 50)
    assert ultima.ok is True
    recusada = _resposta_a_ferramenta(_chamada(51), despachante, LIMITE_CHAMADAS_POR_CELULA + 1)
    assert recusada.ok is False
    assert recusada.error is not None
    assert "50" in recusada.error
    assert [c.call_id for c in chamadas] == ["c50"]


def test_despachante_que_levanta_vira_erro_sem_derrubar_o_host() -> None:
    def despachante(chamada: ToolCall) -> ToolResult:
        raise RuntimeError("detalhe interno com /caminho/do/host")

    resposta = _resposta_a_ferramenta(_chamada(1), despachante, 1)
    assert resposta.ok is False
    assert resposta.error is not None
    assert "/caminho" not in resposta.error


def test_resultado_com_call_id_trocado_vira_erro() -> None:
    def despachante(chamada: ToolCall) -> ToolResult:
        return ToolResult(call_id="outra", ok=True, data=1)

    resposta = _resposta_a_ferramenta(_chamada(1), despachante, 1)
    assert resposta.ok is False
    assert resposta.call_id == "c1"
