"""Ferramenta MCP ponta a ponta com Docker (Tarefa 12, marcador docker).

Um contêiner real roda o REPL. O LLM falso roteirizado chama `tools.falso.eco(...)`, que atravessa
a ponte até o servidor MCP falso no host. A observação traz o resultado vindo do servidor, e a
trace grava a chamada como `chamada_ferramenta`. Sem Docker, o teste é pulado pelas fixtures.
"""

import contextlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.host.mcp.adaptador import registrar_servidores_mcp
from harness.host.mcp.config import ConfigMcp
from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.registry import Registro
from harness.host.tools import registrar_todas
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import MotivoTermino, Orquestrador
from harness.sandbox.docker_sandbox import ROTULO, DockerSandbox
from harness.telemetry.trace import TraceJsonl, ler_trace
from tests.host.mcp.auxiliares import SERVIDOR_FALSO, ler_pid, processo_morto

pytestmark = pytest.mark.docker


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido\n1\n", encoding="utf-8")
    # O perfil Vendas monta também comercial: a pasta precisa existir.
    (raiz / "comercial").mkdir()
    return raiz


@pytest.fixture
def sandbox_vendas(raiz: Path, imagem: str) -> Iterator[DockerSandbox]:
    sandbox = DockerSandbox(
        imagem=imagem, dados=montagens_do_perfil(perfil_por_nome("Vendas"), raiz)
    )
    sandbox.start()
    yield sandbox
    with contextlib.suppress(Exception):
        sandbox.close()


def _containeres_do_harness(cliente: Any) -> list[Any]:
    return list(cliente.containers.list(all=True, filters={"label": ROTULO}))


def test_ferramenta_mcp_no_repl_real_chega_a_observacao(
    raiz: Path,
    sandbox_vendas: DockerSandbox,
    cliente_docker: Any,
    tmp_path: Path,
) -> None:
    pid = tmp_path / "pid"
    config = ConfigMcp.model_validate(
        {
            "servidores": {
                "falso": {
                    "comando": sys.executable,
                    "args": [str(SERVIDOR_FALSO), "normal", str(pid)],
                    "leitura": ["eco"],
                }
            }
        }
    )
    registro = Registro()
    registrar_todas(registro, raiz)
    roteiro = [
        "Chamo a ferramenta do servidor.\n```python\n"
        "resposta = tools.falso.eco(texto='ola mcp')\n"
        "print(resposta)\n```",
        "Tenho o resultado.\n```python\nFINAL('Eco feito', ['falso.eco'])\n```",
    ]
    trace = TraceJsonl(tmp_path / "traces", session_id="mcp")
    perfil = perfil_por_nome("Vendas")

    with registrar_servidores_mcp(registro, config):
        resultado = Orquestrador(
            LLMFalso(roteiro), sandbox_vendas, registro, perfil, eventos=trace
        ).executar_pedido("Ecoa um texto pela ferramenta do servidor.")

    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.resposta == "Eco feito"
    # A observação traz o que o servidor MCP devolveu, dentro do envelope não confiável.
    assert resultado.passos[0].ferramentas == {"falso.eco": 1}
    assert '{"texto": "ola mcp"}' in resultado.passos[0].observacao
    chamadas = [
        linha["dados"]
        for linha in ler_trace(trace.caminho)
        if linha["tipo"] == "chamada_ferramenta"
    ]
    assert [c["nome"] for c in chamadas] == ["falso.eco"]
    assert chamadas[0]["ok"] is True
    assert chamadas[0]["args"] == {"texto": "ola mcp"}
    # O servidor foi fechado pelo gerente, e nenhum contêiner do harness sobra.
    assert processo_morto(ler_pid(pid))
    assert _containeres_do_harness(cliente_docker) == []
