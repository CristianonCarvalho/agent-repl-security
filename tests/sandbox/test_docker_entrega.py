"""Integração ponta a ponta de `entrega.publicar` e aprovação com Docker (marcador `docker`)."""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.host.aprovacao import (
    Aprovador,
    PedidoDeAprovacao,
    RespostaDeAprovacao,
    sempre_aprova,
    sempre_recusa,
)
from harness.host.perfil import montagens_do_perfil, perfil_por_nome
from harness.host.registry import Registro
from harness.host.tools import entrega, registrar_todas
from harness.sandbox.docker_sandbox import DockerSandbox, ResultadoExecucao, SandboxErro

pytestmark = pytest.mark.docker

CONTEUDO_CSV = "item,valor\nreceita,1000\n"


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    dados = tmp_path / "dados"
    for dep in ("financeiro", "vendas", "comercial"):
        (dados / dep).mkdir(parents=True)
    (dados / "financeiro" / "base.csv").write_text("a\n1\n", encoding="utf-8")
    return dados


def _preparar(
    raiz: Path,
    pasta_entregas: Path,
    criar_sandbox: Callable[..., DockerSandbox],
    aprovador: Aprovador,
    prazo_aprovacao_s: float = 30.0,
) -> tuple[DockerSandbox, Registro, Any]:
    perfil = perfil_por_nome("Financeiro").com_concessoes(["entrega.publicar"])
    sandbox = criar_sandbox(dados=montagens_do_perfil(perfil, raiz))
    registro = Registro(aprovador=aprovador, prazo_aprovacao_s=prazo_aprovacao_s)
    registrar_todas(registro, raiz)
    entrega.registrar(
        registro, lambda nome, limite: sandbox.ler_saida(nome, limite), pasta_entregas
    )
    return sandbox, registro, perfil


def _executar_entrega(
    raiz: Path,
    pasta_entregas: Path,
    criar_sandbox: Callable[..., DockerSandbox],
    aprovador: Aprovador,
    codigo: str,
    timeout_s: float = 30.0,
    prazo_aprovacao_s: float = 30.0,
) -> ResultadoExecucao:
    sandbox, registro, perfil = _preparar(
        raiz, pasta_entregas, criar_sandbox, aprovador, prazo_aprovacao_s=prazo_aprovacao_s
    )
    return sandbox.executar_detalhado(
        1, codigo, timeout_s, despachante=registro.despachante(perfil)
    )


def test_entrega_aprovada_grava_arquivo_com_mesmo_conteudo(
    raiz: Path, tmp_path: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    pasta_entregas = tmp_path / "entregas"
    codigo = f"""
import json
with open("/saida/relatorio.csv", "w", encoding="utf-8") as f:
    f.write({CONTEUDO_CSV!r})
res = tools.entrega.publicar(caminho="/saida/relatorio.csv", destinatario="financeiro")
print(json.dumps(res))
"""
    resultado = _executar_entrega(raiz, pasta_entregas, criar_sandbox, sempre_aprova(), codigo)
    assert resultado.done.exception is None
    saida = json.loads(resultado.done.stdout.strip().splitlines()[-1])
    assert saida["entregue"] is True

    arquivos = list((pasta_entregas / "financeiro").glob("*-relatorio.csv"))
    assert len(arquivos) == 1
    assert arquivos[0].read_text(encoding="utf-8") == CONTEUDO_CSV


def test_entrega_recusada_nao_grava_e_levanta_tool_error(
    raiz: Path, tmp_path: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    pasta_entregas = tmp_path / "entregas"
    codigo = f"""
import json
with open("/saida/relatorio.csv", "w", encoding="utf-8") as f:
    f.write({CONTEUDO_CSV!r})
try:
    tools.entrega.publicar(caminho="/saida/relatorio.csv", destinatario="financeiro")
    print(json.dumps({{"erro": None}}))
except ToolError as e:
    print(json.dumps({{"erro": type(e).__name__, "msg": str(e)}}))
"""
    resultado = _executar_entrega(raiz, pasta_entregas, criar_sandbox, sempre_recusa(), codigo)
    assert resultado.done.exception is None
    saida = json.loads(resultado.done.stdout.strip().splitlines()[-1])
    assert saida["erro"] == "ToolError"
    assert not (pasta_entregas / "financeiro").exists()


def test_aprovacao_mais_lenta_que_prazo_da_celula_credita_espera(
    raiz: Path, tmp_path: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    pasta_entregas = tmp_path / "entregas"

    def aprovador_lento(pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao:
        time.sleep(1.5)
        return RespostaDeAprovacao.APROVADO

    codigo = f"""
import json
with open("/saida/relatorio.csv", "w", encoding="utf-8") as f:
    f.write({CONTEUDO_CSV!r})
res = tools.entrega.publicar(caminho="/saida/relatorio.csv", destinatario="financeiro")
print(json.dumps(res))
"""
    resultado = _executar_entrega(
        raiz,
        pasta_entregas,
        criar_sandbox,
        aprovador_lento,
        codigo,
        timeout_s=1.0,
        prazo_aprovacao_s=5.0,
    )
    assert resultado.done.exception is None
    assert resultado.espera_aprovacao_s >= 1.4
    arquivos = list((pasta_entregas / "financeiro").glob("*-relatorio.csv"))
    assert len(arquivos) == 1
    assert arquivos[0].read_text(encoding="utf-8") == CONTEUDO_CSV


def test_ler_saida_recusa_symlink_diretorio_e_arquivo_acima_do_limite(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    perfil = perfil_por_nome("Financeiro")
    sandbox = criar_sandbox(dados=montagens_do_perfil(perfil, raiz))
    codigo = """
import os
os.symlink("/etc/passwd", "/saida/l")
os.mkdir("/saida/d")
with open("/saida/grande.txt", "wb") as f:
    f.write(b"x" * 200)
"""
    resultado = sandbox.executar_detalhado(1, codigo, 30.0)
    assert resultado.done.exception is None

    with pytest.raises(SandboxErro):
        sandbox.ler_saida("l", 1024)
    with pytest.raises(SandboxErro):
        sandbox.ler_saida("d", 1024)
    with pytest.raises(SandboxErro):
        sandbox.ler_saida("grande.txt", 100)
