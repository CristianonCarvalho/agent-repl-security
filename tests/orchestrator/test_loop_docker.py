"""Testes ponta a ponta do loop com Docker (Tarefa 7, marcador docker).

Cada teste sobe um contêiner real com DockerSandbox, roda o Orquestrador com um LLM falso
roteirizado e um Registro com `catalogo.listar` sobre um diretório temporário com a forma de
/dados. Sem Docker, os testes são pulados pelas fixtures de tests/conftest.py.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.registry import Registro
from harness.host.tools import registrar_todas
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import MotivoTermino, Orquestrador, ResultadoPedido
from harness.sandbox.docker_sandbox import ROTULO, DockerSandbox

pytestmark = pytest.mark.docker


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    """Diretório com um arquivo por departamento, no formato de /dados."""
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido\n1\n", encoding="utf-8")
    (raiz / "comercial" / "propostas").mkdir(parents=True)
    (raiz / "comercial" / "propostas" / "p1.txt").write_text("proposta\n", encoding="utf-8")
    (raiz / "marketing").mkdir()
    (raiz / "marketing" / "campanhas.csv").write_text("campanha\n", encoding="utf-8")
    return raiz


@pytest.fixture
def sandbox_vendas(raiz: Path, imagem: str) -> Iterator[DockerSandbox]:
    """Contêiner iniciado com as montagens do perfil Vendas; fechado no fim do teste."""
    sandbox = DockerSandbox(
        imagem=imagem, dados=montagens_do_perfil(perfil_por_nome("Vendas"), raiz)
    )
    sandbox.start()
    yield sandbox
    sandbox.close()


class EspiaoSandbox:
    """Envolve o sandbox real e conta as células que chegaram até ele."""

    def __init__(self, real: DockerSandbox) -> None:
        self._real = real
        self.celulas = 0

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Any = None,
    ) -> Any:
        self.celulas += 1
        return self._real.executar_detalhado(cell_id, code, timeout_s, despachante)

    def reiniciar(self) -> None:
        self._real.reiniciar()

    def carregar_contexto(self, valor: Any) -> None:
        self._real.carregar_contexto(valor)

    def close(self) -> None:
        self._real.close()


def _registro(raiz: Path) -> Registro:
    registro = Registro()
    registrar_todas(registro, raiz)
    return registro


def _containeres_do_harness(cliente: Any) -> list[Any]:
    return list(cliente.containers.list(all=True, filters={"label": ROTULO}))


def _pedido(raiz: Path, sandbox: Any, roteiro: list[str]) -> ResultadoPedido:
    perfil = perfil_por_nome("Vendas")
    orquestrador = Orquestrador(LLMFalso(roteiro), sandbox, _registro(raiz), perfil)
    return orquestrador.executar_pedido("Quantos arquivos eu vejo?")


def test_pedido_completo_lista_arquivos_e_termina_com_final(
    raiz: Path, sandbox_vendas: DockerSandbox, cliente_docker: Any
) -> None:
    roteiro = [
        "Vou listar os arquivos.\n```python\narquivos = tools.catalogo.listar()\n"
        "print(len(arquivos))\n```",
        "Tenho a contagem.\n```python\ntotal = len(tools.catalogo.listar())\n"
        "FINAL(f'{total} arquivos visíveis', ['vendas/pedidos.csv'])\n```",
    ]
    resultado = _pedido(raiz, sandbox_vendas, roteiro)

    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.completo is True
    assert resultado.resposta == "2 arquivos visíveis"
    assert resultado.fontes == ("vendas/pedidos.csv",)
    assert resultado.passos[0].ferramentas == {"catalogo.listar": 1}
    assert resultado.passos[0].observacao.startswith("[cell 1]")
    # Fechado pelo orquestrador: nenhum contêiner do harness sobra.
    assert _containeres_do_harness(cliente_docker) == []


def test_codigo_com_import_proibido_e_barrado_sem_chegar_ao_conteiner(
    raiz: Path, sandbox_vendas: DockerSandbox
) -> None:
    espiao = EspiaoSandbox(sandbox_vendas)
    roteiro = ["```python\nimport subprocess\nsubprocess.run(['id'])\n```"] * 3
    perfil = perfil_por_nome("Vendas")
    orquestrador = Orquestrador(LLMFalso(roteiro), espiao, _registro(raiz), perfil)

    resultado = orquestrador.executar_pedido("Pedido")

    assert resultado.motivo is MotivoTermino.ERRO_IRRECUPERAVEL
    assert espiao.celulas == 0
    assert all(passo.cell_id is None for passo in resultado.passos)
    assert all(passo.problemas_estaticos for passo in resultado.passos)


def test_conteiner_e_removido_ao_fim_do_pedido_mesmo_com_erro(
    raiz: Path, sandbox_vendas: DockerSandbox, cliente_docker: Any
) -> None:
    resultado = _pedido(raiz, sandbox_vendas, ["só texto"] * 3)

    assert resultado.motivo is MotivoTermino.ERRO_IRRECUPERAVEL
    assert _containeres_do_harness(cliente_docker) == []


def test_tool_call_chega_ao_host_pelo_despachante_do_perfil(
    raiz: Path, sandbox_vendas: DockerSandbox
) -> None:
    # O perfil Vendas não vê marketing: a chamada é recusada no host, antes de qualquer leitura.
    roteiro = [
        "```python\ntry:\n    tools.catalogo.listar(departamento='marketing')\n"
        "    print('visto')\nexcept ToolError as erro:\n    print(erro)\n```",
        "```python\nFINAL('ok', ['vendas/pedidos.csv'])\n```",
    ]
    resultado = _pedido(raiz, sandbox_vendas, roteiro)

    assert resultado.motivo is MotivoTermino.FINAL
    assert "acesso negado" in resultado.passos[0].observacao
    assert "visto" not in resultado.passos[0].observacao
