import io
import tarfile
from unittest.mock import MagicMock

import pytest
from docker.errors import DockerException

from harness.sandbox.docker_sandbox import (
    DockerSandbox,
    ResultadoExecucao,
    SandboxErro,
    _extrair_arquivo_unico,
)


def _tar(*membros: tuple[str, bytes | None, str]) -> list[bytes]:
    """Cada membro: (nome, conteúdo ou None, tipo 'arq'|'dir'|'link')."""
    bruto = io.BytesIO()
    with tarfile.open(fileobj=bruto, mode="w") as tar:
        for nome, conteudo, tipo in membros:
            info = tarfile.TarInfo(nome)
            if tipo == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif tipo == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                dados = conteudo or b""
                info.size = len(dados)
                tar.addfile(info, io.BytesIO(dados))
    valor = bruto.getvalue()
    return [valor[i : i + 700] for i in range(0, len(valor), 700)]


def test_le_um_arquivo_regular() -> None:
    assert (
        _extrair_arquivo_unico(_tar(("a.csv", b"x,y\n1,2\n", "arq")), "a.csv", 1000)
        == b"x,y\n1,2\n"
    )


@pytest.mark.parametrize(
    "membros",
    [
        (("a.csv", None, "dir"),),
        (("a.csv", None, "link"),),
        (("a.csv", b"1", "arq"), ("b.csv", b"2", "arq")),
        (("outro.csv", b"1", "arq"),),
    ],
)
def test_recusa_o_que_nao_e_um_unico_arquivo_regular(membros) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SandboxErro):
        _extrair_arquivo_unico(_tar(*membros), "a.csv", 1000)


def test_recusa_arquivo_acima_do_limite() -> None:
    with pytest.raises(SandboxErro):
        _extrair_arquivo_unico(_tar(("a.csv", b"x" * 2000, "arq")), "a.csv", 1000)


def test_resultado_de_execucao_tem_espera_zero_por_padrao() -> None:
    assert ResultadoExecucao.__dataclass_fields__["espera_aprovacao_s"].default == 0.0


def test_recusa_fluxo_tar_acima_da_folga() -> None:
    fluxo = [b"x" * (600 * 1024), b"x" * (600 * 1024)]
    with pytest.raises(SandboxErro, match="acima do limite"):
        _extrair_arquivo_unico(fluxo, "a.csv", 100)


def test_ler_saida_sem_conteiner_levanta_sandbox_erro() -> None:
    sb = DockerSandbox()
    with pytest.raises(SandboxErro):
        sb.ler_saida("a.csv", 1000)


def test_ler_saida_falha_api_levanta_mensagem_esperada() -> None:
    sb = DockerSandbox()
    sb._conteiner = MagicMock()
    sb._conteiner.get_archive.side_effect = DockerException("not found")
    with pytest.raises(SandboxErro, match="arquivo de saída não encontrado ou ilegível"):
        sb.ler_saida("a.csv", 1000)


def test_ler_saida_sucesso() -> None:
    sb = DockerSandbox()
    sb._conteiner = MagicMock()
    sb._conteiner.get_archive.return_value = (_tar(("a.csv", b"ok\n", "arq")), {})
    assert sb.ler_saida("a.csv", 1000) == b"ok\n"
    sb._conteiner.get_archive.assert_called_once_with("/saida/a.csv")
