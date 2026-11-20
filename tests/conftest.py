"""Fixtures compartilhadas pelos testes com Docker (marcador `docker`).

A imagem `harness-sandbox:dev` é construída uma vez por sessão a partir da raiz do
repositório. Sem Docker, os testes que pedem estas fixtures são pulados.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import docker
import pytest

from harness.sandbox.docker_sandbox import DockerSandbox

RAIZ = Path(__file__).resolve().parents[1]
IMAGEM = "harness-sandbox:dev"


@pytest.fixture(scope="session")
def cliente_docker() -> Any:
    try:
        cliente = docker.from_env()
        cliente.ping()
    except Exception as erro:  # Docker ausente, parado ou sem permissão
        pytest.skip(f"Docker indisponível: {erro}")
    return cliente


@pytest.fixture(scope="session")
def imagem(cliente_docker: Any) -> str:
    # Constrói a imagem uma vez por sessão. O cache de camadas do Docker deixa as próximas rápidas.
    try:
        cliente_docker.images.build(
            path=str(RAIZ),
            dockerfile="harness/sandbox/Dockerfile",
            tag=IMAGEM,
            rm=True,
            forcerm=True,
        )
    except docker.errors.BuildError as erro:
        pytest.fail(f"falha ao construir a imagem do sandbox: {erro}")
    return IMAGEM


@pytest.fixture
def criar_sandbox(imagem: str) -> Iterator[Callable[..., DockerSandbox]]:
    """Cria sandboxes para o teste e garante que todos sejam fechados no fim."""
    criados: list[DockerSandbox] = []

    def criar(**kwargs: Any) -> DockerSandbox:
        sandbox = DockerSandbox(imagem=imagem, **kwargs)
        sandbox.start()
        criados.append(sandbox)
        return sandbox

    yield criar
    for sandbox in criados:
        sandbox.close()
