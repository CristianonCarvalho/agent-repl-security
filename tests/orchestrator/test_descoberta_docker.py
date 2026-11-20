"""Descoberta sob demanda ponta a ponta com Docker (Tarefa 10, marcador docker).

Um contêiner real roda o REPL. O LLM falso roteirizado chama `search_tools` e `describe_tool`,
as observações trazem os resultados, e a trace grava as chamadas `descoberta.*` como
`chamada_ferramenta`. Sem Docker, os testes são pulados pelas fixtures de tests/conftest.py.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.registry import Registro
from harness.host.tools import registrar_todas
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import MotivoTermino, Orquestrador
from harness.sandbox.docker_sandbox import ROTULO, DockerSandbox
from harness.telemetry.trace import TraceJsonl, ler_trace

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
    sandbox.close()


def _containeres_do_harness(cliente: Any) -> list[Any]:
    return list(cliente.containers.list(all=True, filters={"label": ROTULO}))


def test_search_tools_e_describe_tool_no_repl_real_chegam_as_observacoes(
    raiz: Path,
    sandbox_vendas: DockerSandbox,
    cliente_docker: Any,
    tmp_path: Path,
) -> None:
    registro = Registro()
    registrar_todas(registro, raiz)
    roteiro = [
        "Procuro a ferramenta certa.\n```python\n"
        "achadas = search_tools('listar arquivos', limit=3)\n"
        "print([item['nome'] for item in achadas])\n```",
        "Leio os argumentos antes de chamar.\n```python\n"
        "assinatura = describe_tool('catalogo.listar')\n"
        "print(assinatura['exemplo'])\n"
        "print([p['nome'] for p in assinatura['parametros']])\n```",
        "Tenho o que preciso.\n```python\n"
        "FINAL('Use catalogo.listar', ['vendas/pedidos.csv'])\n```",
    ]
    trace = TraceJsonl(tmp_path / "traces", session_id="descoberta")
    orquestrador = Orquestrador(
        LLMFalso(roteiro),
        sandbox_vendas,
        registro,
        perfil_por_nome("Vendas"),
        eventos=trace,
    )

    resultado = orquestrador.executar_pedido("Quais arquivos eu consigo listar?")

    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.resposta == "Use catalogo.listar"
    # As observações trazem o que o host devolveu: o nome da ferramenta e a assinatura.
    assert "catalogo.listar" in resultado.passos[0].observacao
    assert 'tools.catalogo.listar(departamento="vendas")' in resultado.passos[1].observacao
    assert "departamento" in resultado.passos[1].observacao
    assert resultado.passos[0].ferramentas == {"descoberta.buscar": 1}
    assert resultado.passos[1].ferramentas == {"descoberta.descrever": 1}
    # A trace registra as chamadas de descoberta como chamada_ferramenta, com sucesso.
    chamadas = [
        linha["dados"]
        for linha in ler_trace(trace.caminho)
        if linha["tipo"] == "chamada_ferramenta"
    ]
    assert [c["nome"] for c in chamadas] == ["descoberta.buscar", "descoberta.descrever"]
    assert all(c["ok"] is True for c in chamadas)
    assert chamadas[0]["args"] == {"query": "listar arquivos", "limit": 3}
    # Fechado pelo orquestrador: nenhum contêiner do harness sobra.
    assert _containeres_do_harness(cliente_docker) == []
