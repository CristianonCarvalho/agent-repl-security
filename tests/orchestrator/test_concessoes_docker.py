"""Concessões de ferramentas ponta a ponta com Docker (D14, marcador docker).

Um contêiner real roda o REPL. O LLM falso chama, por `getattr` (que a verificação estática não
analisa), uma ferramenta negada, uma inexistente e uma concedida. A negada tem de responder
exatamente como a inexistente, e o conteúdo da negada não pode aparecer. Sem Docker, os testes
são pulados pelas fixtures de tests/conftest.py.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.host.tools import registrar_todas
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import MotivoTermino, Orquestrador
from harness.sandbox.docker_sandbox import ROTULO, DockerSandbox
from harness.telemetry.trace import TraceJsonl

pytestmark = pytest.mark.docker

# Chamada que a verificação estática não enxerga: `getattr` não é uma chamada literal `tools.g.n`.
CHAMADA_DINAMICA = "getattr(getattr(tools, '{grupo}'), '{nome}')()"


class _Vazio(BaseModel):
    """Modelo sem argumentos, para as ferramentas de teste."""


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


def _registro_com_ferramentas_de_teste(raiz: Path) -> Registro:
    registro = Registro()
    registrar_todas(registro, raiz)
    registro.ferramenta(nome="teste.aberta", nivel=Nivel.LEITURA, descricao="Aberta.", args=_Vazio)(
        lambda args, perfil: "conteudo liberado"
    )
    registro.ferramenta(nome="teste.oculta", nivel=Nivel.LEITURA, descricao="Oculta.", args=_Vazio)(
        lambda args, perfil: "SEGREDO da ferramenta negada"
    )
    return registro


def test_negada_responde_igual_a_inexistente_e_a_concedida_funciona(
    raiz: Path, sandbox_vendas: DockerSandbox, cliente_docker: Any, tmp_path: Path
) -> None:
    perfil = perfil_por_nome("Vendas").com_concessoes({"teste.aberta"})
    roteiro = [
        "Tento as três.\n```python\n"
        f"try:\n    {CHAMADA_DINAMICA.format(grupo='teste', nome='oculta')}\n"
        "except ToolError as erro:\n    print('negada:', erro)\n"
        f"try:\n    {CHAMADA_DINAMICA.format(grupo='teste', nome='nao_existe')}\n"
        "except ToolError as erro:\n    print('inexistente:', erro)\n"
        f"print('aberta:', {CHAMADA_DINAMICA.format(grupo='teste', nome='aberta')})\n```",
        "Tenho o que preciso.\n```python\nFINAL('ok', ['vendas/pedidos.csv'])\n```",
    ]
    trace = TraceJsonl(tmp_path / "traces", session_id="concessoes")
    orquestrador = Orquestrador(
        LLMFalso(roteiro),
        sandbox_vendas,
        _registro_com_ferramentas_de_teste(raiz),
        perfil,
        eventos=trace,
    )

    resultado = orquestrador.executar_pedido("Use as ferramentas de teste.")

    assert resultado.motivo is MotivoTermino.FINAL
    observacao = resultado.passos[0].observacao
    # A negada recebe a mesma mensagem que uma ferramenta inexistente, com o nome próprio.
    assert "negada: a ferramenta 'teste.oculta' não existe" in observacao
    assert "inexistente: a ferramenta 'teste.nao_existe' não existe" in observacao
    # O conteúdo da ferramenta negada nunca chega ao modelo, e a concedida funciona.
    assert "SEGREDO" not in observacao
    assert "aberta: conteudo liberado" in observacao
    # Fechado pelo orquestrador: nenhum contêiner do harness sobra.
    assert _containeres_do_harness(cliente_docker) == []
