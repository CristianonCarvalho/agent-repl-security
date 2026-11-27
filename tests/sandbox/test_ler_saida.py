from unittest.mock import MagicMock

import pytest
from docker.errors import DockerException

from harness.sandbox.docker_sandbox import (
    _LEITOR_DE_SAIDA,
    DockerSandbox,
    ResultadoExecucao,
    SandboxErro,
    _interpretar_leitura,
)


def test_interpretar_leitura_codigo_zero_devolve_bytes() -> None:
    assert _interpretar_leitura(0, b"x,y\n1,2\n", 1000) == b"x,y\n1,2\n"


def test_interpretar_leitura_codigo_zero_acima_do_limite_levanta() -> None:
    with pytest.raises(SandboxErro, match="arquivo de saída acima do limite"):
        _interpretar_leitura(0, b"x" * 11, 10)


@pytest.mark.parametrize(
    ("codigo", "mensagem"),
    [
        (2, "arquivo de saída não encontrado"),
        (3, "o arquivo de saída não é um arquivo regular"),
        (4, "arquivo de saída acima do limite"),
        (9, "arquivo de saída ilegível"),
    ],
)
def test_interpretar_leitura_codigos_de_erro(codigo: int, mensagem: str) -> None:
    with pytest.raises(SandboxErro, match=mensagem):
        _interpretar_leitura(codigo, b"", 1000)


def test_resultado_de_execucao_tem_espera_zero_por_padrao() -> None:
    assert ResultadoExecucao.__dataclass_fields__["espera_aprovacao_s"].default == 0.0


def test_ler_saida_sem_conteiner_levanta_sandbox_erro() -> None:
    sb = DockerSandbox()
    with pytest.raises(SandboxErro):
        sb.ler_saida("a.csv", 1000)


@pytest.mark.parametrize("nome", ["../etc/passwd", "a/b.csv", ""])
def test_ler_saida_nome_invalido_levanta(nome: str) -> None:
    sb = DockerSandbox()
    sb._conteiner = MagicMock()
    with pytest.raises(SandboxErro, match="nome de arquivo de saída inválido"):
        sb.ler_saida(nome, 1000)
    sb._conteiner.exec_run.assert_not_called()


def test_ler_saida_falha_api_levanta_mensagem_esperada() -> None:
    sb = DockerSandbox()
    sb._conteiner = MagicMock()
    sb._conteiner.exec_run.side_effect = DockerException("exec failed")
    with pytest.raises(SandboxErro, match="arquivo de saída ilegível"):
        sb.ler_saida("a.csv", 1000)


def test_ler_saida_sucesso() -> None:
    sb = DockerSandbox()
    sb._conteiner = MagicMock()
    sb._conteiner.exec_run.return_value = MagicMock(exit_code=0, output=b"ok\n")
    assert sb.ler_saida("a.csv", 1000) == b"ok\n"
    sb._conteiner.exec_run.assert_called_once_with(
        ["python", "-c", _LEITOR_DE_SAIDA, "/saida/a.csv", "1000"],
        demux=False,
    )
