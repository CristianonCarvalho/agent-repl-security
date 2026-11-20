"""Teste ponta a ponta do contexto com contêiner real (marcador docker).

Um LLM falso lê um contexto de cerca de 500 KB por fatias, dentro do contêiner, e termina com
FINAL. O teste confere que o modelo viu só a prévia e que a leitura por código funcionou.
"""

from collections.abc import Callable

import pytest

from harness.host.perfil import perfil_por_nome
from harness.host.registry import Registro
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import MotivoTermino, Orquestrador
from harness.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.docker


def test_modelo_le_o_contexto_por_fatias_dentro_do_conteiner(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    contexto = "\n".join(f"pedido {i}: valor {i * 10}" for i in range(20000))
    roteiro = [
        "Vou ver o tipo e o tamanho.\n```python\ninfo = context_info()\n"
        "print(info['tipo'], info['tamanho'])\n```",
        "Leio só o fim.\n```python\nprint(repr(context[-40:]))\n```",
        "Conto as linhas sem imprimir o conteúdo.\n```python\n"
        "linhas = context.splitlines()\n"
        "FINAL(f'{len(linhas)} linhas', ['contexto do pedido'])\n```",
    ]
    modelo = LLMFalso(roteiro)
    sandbox = criar_sandbox()
    orquestrador = Orquestrador(modelo, sandbox, Registro(), perfil_por_nome("Vendas"))

    resultado = orquestrador.executar_pedido("Quantas linhas tem o contexto?", contexto=contexto)

    assert resultado.motivo is MotivoTermino.FINAL
    assert resultado.completo is True
    assert resultado.resposta == "20000 linhas"
    # Primeira célula: tipo e tamanho exatos, lidos dentro do contêiner.
    assert f"texto {len(contexto)}" in resultado.passos[0].observacao
    # Segunda célula: fatia do fim, que o modelo não tinha visto.
    assert "pedido 19999: valor 199990" in resultado.passos[1].observacao
    # Antes de ler o fim, o modelo não conhecia o trecho; só a observação da fatia o trouxe.
    for mensagem in modelo.chamadas[0]:
        assert "pedido 19999" not in mensagem.content
    assert "pedido 19999" not in modelo.chamadas[1][-1].content
    assert "pedido 19999" in modelo.chamadas[2][-1].content


def test_context_info_sem_carga_no_conteiner_avisa(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    resultado = sandbox.executar(1, "print(context_info())", timeout_s=5.0)
    assert resultado.exception is not None
    assert "não há contexto carregado" in resultado.exception
