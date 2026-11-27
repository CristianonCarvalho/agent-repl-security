"""Rastreio de arquivos e política da trifecta ponta a ponta, com Docker (marcador `docker`)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from harness.host.perfil import Perfil, montagens_do_perfil, perfil_por_nome
from harness.host.politica import RastreadorDeArquivos
from harness.host.registry import Nivel, Registro
from harness.host.tools import registrar_todas
from harness.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.docker


class Vazio(BaseModel):
    model_config = ConfigDict(extra="forbid")


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "comercial").mkdir(parents=True)
    (raiz / "vendas" / "interno.csv").write_text("a\n1\n", encoding="utf-8")
    (raiz / "vendas" / "externo.txt").write_text("texto de fora\n", encoding="utf-8")
    (raiz / "manifesto.json").write_text('{"vendas/externo.txt": "externo"}', encoding="utf-8")
    return raiz


def _registro(raiz: Path, politica: bool) -> Registro:
    registro = Registro(politica=politica)
    registrar_todas(registro, raiz)

    @registro.ferramenta(
        nome="teste.enviar", nivel=Nivel.LEITURA, descricao="envia", args=Vazio, canal_externo=True
    )
    def enviar(args: Vazio, perfil: Perfil) -> Any:
        return "enviado"

    return registro


def _executar(
    raiz: Path,
    criar_sandbox: Callable[..., DockerSandbox],
    codigo: str,
    politica: bool,
) -> dict[str, Any]:
    perfil = perfil_por_nome("Vendas").com_concessoes(frozenset({"teste.*"}))
    registro = _registro(raiz, politica)
    sandbox = criar_sandbox(dados=montagens_do_perfil(perfil, raiz), rastrear_arquivos=politica)
    rastreador = RastreadorDeArquivos(raiz, registro.exposicao) if politica else None
    opcoes: dict[str, Any] = {"ao_abrir": rastreador} if rastreador is not None else {}
    resultado = sandbox.executar_detalhado(
        1, codigo, 30.0, despachante=registro.despachante(perfil), **opcoes
    )
    return {
        "stdout": resultado.done.stdout,
        "excecao": resultado.done.exception,
        "viu_nao_confiavel": registro.exposicao.viu_nao_confiavel,
    }


CODIGO = """
import json
open('/dados/vendas/externo.txt').read()
try:
    tools.teste.enviar()
    print(json.dumps('enviou'))
except Exception as erro:
    print(json.dumps('recusou'))
"""


def test_com_a_politica_ligada_ler_externo_e_enviar_e_recusado(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    saida = _executar(raiz, criar_sandbox, CODIGO, politica=True)
    assert json.loads(saida["stdout"].strip().splitlines()[-1]) == "recusou"
    assert saida["viu_nao_confiavel"] is True


def test_com_a_politica_desligada_o_mesmo_codigo_envia(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    saida = _executar(raiz, criar_sandbox, CODIGO, politica=False)
    assert json.loads(saida["stdout"].strip().splitlines()[-1]) == "enviou"


def test_ler_so_arquivo_confiavel_nao_impede_o_envio(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    codigo = CODIGO.replace("externo.txt", "interno.csv")
    saida = _executar(raiz, criar_sandbox, codigo, politica=True)
    assert json.loads(saida["stdout"].strip().splitlines()[-1]) == "enviou"
    assert saida["viu_nao_confiavel"] is False
