"""Gancho de auditoria do sandbox: avisa o host antes de abrir arquivos sob /dados."""

import io
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from harness.sandbox.protocol import ArquivoAberto, ToolResult, decode, encode
from harness.sandbox.rastreio import criar_gancho
from harness.sandbox.tools_client import Ponte


class PonteFalsa:
    """Imita a Ponte: guarda os caminhos avisados e responde como configurado."""

    def __init__(self, erro: Exception | None = None) -> None:
        self.avisos: list[str] = []
        self._erro = erro

    def avisar_abertura(self, caminho: str) -> None:
        self.avisos.append(caminho)
        if self._erro is not None:
            raise self._erro


def _gancho(ponte: Any) -> Any:
    return criar_gancho(ponte, raiz="/dados")


def test_abertura_sob_dados_avisa_o_host() -> None:
    ponte = PonteFalsa()
    _gancho(ponte)("open", ("/dados/vendas/a.csv", "r", 0))
    assert ponte.avisos == ["/dados/vendas/a.csv"]


@pytest.mark.parametrize(
    "caminho", ["/tmp/x", "/saida/r.txt", "/proc/self/status", "/usr/lib/python3/os.py"]
)
def test_abertura_fora_de_dados_e_ignorada(caminho: str) -> None:
    ponte = PonteFalsa()
    _gancho(ponte)("open", (caminho, "r", 0))
    assert ponte.avisos == []


def test_outros_eventos_e_descritores_sao_ignorados() -> None:
    ponte = PonteFalsa()
    gancho = _gancho(ponte)
    gancho("socket.connect", (object(), ("1.2.3.4", 80)))
    gancho("open", (7, "r", 0))
    assert ponte.avisos == []


def test_caminho_com_ponto_ponto_e_resolvido_antes_de_avisar() -> None:
    ponte = PonteFalsa()
    _gancho(ponte)("open", ("/dados/vendas/../marketing/a.csv", "r", 0))
    assert ponte.avisos == ["/dados/marketing/a.csv"]


def test_falha_do_aviso_bloqueia_a_abertura() -> None:
    ponte = PonteFalsa(erro=RuntimeError("canal fechado"))
    with pytest.raises(PermissionError):
        _gancho(ponte)("open", ("/dados/vendas/a.csv", "r", 0))


def test_o_aviso_nao_dispara_o_gancho_de_novo() -> None:
    avisos: list[str] = []

    class Reentrante:
        def __init__(self) -> None:
            self.gancho: Any = None

        def avisar_abertura(self, caminho: str) -> None:
            avisos.append(caminho)
            # Se o próprio aviso abrir um arquivo de /dados, o gancho não pode recursar.
            self.gancho("open", ("/dados/x/y.csv", "r", 0))

    ponte = Reentrante()
    ponte.gancho = _gancho(ponte)
    ponte.gancho("open", ("/dados/a/b.csv", "r", 0))
    assert avisos == ["/dados/a/b.csv"]


def test_ponte_real_envia_arquivo_aberto_e_espera_a_liberacao() -> None:
    resposta = encode(ToolResult(call_id="c1", ok=True))
    entrada = io.StringIO(resposta)
    saida = io.StringIO()
    Ponte(entrada, saida).avisar_abertura("/dados/vendas/a.csv")
    enviada = decode(saida.getvalue())
    assert isinstance(enviada, ArquivoAberto)
    assert (enviada.call_id, enviada.caminho) == ("c1", "/dados/vendas/a.csv")


def test_ponte_real_levanta_quando_o_host_recusa() -> None:
    from harness.sandbox.tools_client import ToolError

    resposta = encode(ToolResult(call_id="c1", ok=False, error="caminho fora do formato"))
    ponte = Ponte(io.StringIO(resposta), io.StringIO())
    with pytest.raises(ToolError):
        ponte.avisar_abertura("/dados/x")


def test_gancho_instalado_no_processo_do_runner_bloqueia_leitura_sem_host() -> None:
    """Num subprocesso, com --rastrear-arquivos e sem host que responda, abrir /dados falha."""
    codigo = textwrap.dedent(
        """
        import io, sys
        from harness.sandbox.protocol import Exec, ExecDone, decode, encode
        from harness.sandbox.runner import serve

        celula = (
            "try:\\n    open('/dados/x.txt').read()\\n"
            "except PermissionError:\\n    print('bloqueado')"
        )
        entrada = io.StringIO(encode(Exec(cell_id=1, code=celula, timeout_s=5.0)))
        saida = io.StringIO()
        serve(entrada, saida, rastrear=True)
        for linha in saida.getvalue().splitlines():
            msg = decode(linha + "\\n")
            if isinstance(msg, ExecDone):
                print("STDOUT=" + msg.stdout.strip())
        """
    )
    processo = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, timeout=60, check=False
    )
    assert "STDOUT=bloqueado" in processo.stdout, processo.stderr
