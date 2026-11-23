"""Fixtures compartilhadas pelos testes com Docker (marcador `docker`).

A imagem `harness-sandbox:dev` é construída uma vez por sessão a partir da raiz do
repositório. Sem Docker, os testes que pedem estas fixtures são pulados.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import docker
import pytest

from harness.llm.config import carregar_env
from harness.sandbox.docker_sandbox import DockerSandbox

RAIZ = Path(__file__).resolve().parents[1]
IMAGEM = "harness-sandbox:dev"


RAIZ_ENV = (RAIZ / ".env").resolve()


def _sem_o_env_do_projeto(
    original: Callable[[Path], dict[str, str]],
) -> Callable[[Path], dict[str, str]]:
    """Leitor de `.env` que devolve {} para o `.env` da raiz do projeto e lê os demais."""

    def carregar(caminho: Path) -> dict[str, str]:
        if caminho.resolve() == RAIZ_ENV:
            return {}
        return original(caminho)

    return carregar


@pytest.fixture(autouse=True)
def _ambiente_isolado(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nenhum teste lê o `.env` do projeto nem herda telemetria ligada do shell.

    Testes que escrevem o próprio `.env` num diretório temporário (com `chdir`) continuam
    lendo o deles. Sem esta guarda, um teste sem `chdir` leria o `.env` real e, com
    HARNESS_TELEMETRIA ligada, poderia abrir exportação de rede.
    """
    for nome in list(os.environ):
        if nome.startswith(("LANGFUSE_", "HARNESS_TELEMETRIA")):
            monkeypatch.delenv(nome, raising=False)
    # Os módulos da CLI e das avaliações importam `carregar_env` por nome: troca-se ali.
    leitor = _sem_o_env_do_projeto(carregar_env)
    monkeypatch.setattr("harness.cli.carregar_env", leitor)
    monkeypatch.setattr("harness.evals.__main__.carregar_env", leitor)


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
