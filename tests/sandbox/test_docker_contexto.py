"""Testes do contexto no sandbox Docker real (marcador `docker`): carga, reinício e teto.

As fixtures `criar_sandbox` e `imagem` vêm de tests/conftest.py. Sem Docker, os testes com o
marcador são pulados; os demais testam só o host.
"""

from collections.abc import Callable

import pytest

from harness.sandbox.docker_sandbox import (
    LIMITE_CONTEXTO_BYTES,
    DockerSandbox,
    SandboxErro,
    SandboxMorreu,
)

marca_docker = pytest.mark.docker


def test_carregar_sem_contêiner_ativo_levanta_erro_sem_docker() -> None:
    sandbox = DockerSandbox()
    with pytest.raises(SandboxErro, match="não está ativo"):
        sandbox.carregar_contexto("texto")


@marca_docker
def test_carrega_um_mebibyte_e_a_celula_so_imprime_o_tamanho(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    texto = "a" * (1024 * 1024)
    sandbox.carregar_contexto(texto)
    resultado = sandbox.executar(1, "print(len(context))", timeout_s=10.0)
    assert resultado.exception is None
    assert resultado.stdout == f"{len(texto)}\n"


@marca_docker
def test_reinicio_por_timeout_restaura_o_context(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    sandbox.carregar_contexto({"chave": "valor" * 10})
    sandbox.executar(1, "x = 1", timeout_s=5.0)
    reinicio = sandbox.executar_detalhado(2, "import time\ntime.sleep(60)", timeout_s=0.5)
    assert reinicio.reiniciado is True
    assert reinicio.done.reiniciar is True
    depois = sandbox.executar(
        3, "print(context['chave'][:5])\nprint('x' in globals())", timeout_s=5.0
    )
    # `context` voltou; as demais variáveis se perderam.
    assert depois.stdout == "valor\nFalse\n"
    assert sandbox.reinicios == 1


@marca_docker
def test_reinicio_por_morte_restaura_o_context_com_reiniciar_manual(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    sandbox.carregar_contexto([1, 2, 3])
    with pytest.raises(SandboxMorreu):
        sandbox.executar(1, "import os\nos._exit(3)", timeout_s=5.0)
    sandbox.reiniciar()
    resultado = sandbox.executar(2, "print(context)", timeout_s=5.0)
    assert resultado.stdout == "[1, 2, 3]\n"


@marca_docker
def test_segundo_carregamento_substitui_o_primeiro_inclusive_depois_de_reinicio(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    sandbox.carregar_contexto("um")
    sandbox.carregar_contexto([4, 5])
    assert sandbox.executar(1, "print(context)", timeout_s=5.0).stdout == "[4, 5]\n"
    sandbox.reiniciar()
    assert sandbox.executar(2, "print(context)", timeout_s=5.0).stdout == "[4, 5]\n"


@marca_docker
def test_acima_do_teto_levanta_sem_vazar_o_conteudo(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    segredo = "SEGREDO-DO-CONTEXTO-" + "x" * LIMITE_CONTEXTO_BYTES
    with pytest.raises(SandboxErro) as info:
        sandbox.carregar_contexto(segredo)
    mensagem = str(info.value)
    assert "SEGREDO" not in mensagem
    assert str(LIMITE_CONTEXTO_BYTES) in mensagem
    # O sandbox segue vivo e nenhum contexto foi carregado.
    vivo = sandbox.executar(1, "print('vivo')", timeout_s=5.0)
    assert vivo.stdout == "vivo\n"
    sem_contexto = sandbox.executar(2, "print(context)", timeout_s=5.0)
    assert sem_contexto.exception is not None
    assert "NameError" in sem_contexto.exception


@marca_docker
def test_execucao_comum_nao_muda_com_o_contexto_carregado(
    criar_sandbox: Callable[..., DockerSandbox],
) -> None:
    sandbox = criar_sandbox()
    sandbox.carregar_contexto("abc")
    resultado = sandbox.executar_detalhado(1, "print(len(context))", timeout_s=5.0)
    assert resultado.done.stdout == "3\n"
    assert resultado.ferramentas == {}
    assert resultado.reiniciado is False
