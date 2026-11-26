"""Servidor MCP de teste `contas` (T19-c): protocolo, dados dos fatos, injeção e ciclo de vida.

Fala com o servidor real por stdio, pelo cliente MCP do harness. Sem modelo, sem rede, sem Docker.
"""

import json
import subprocess
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.fixtures import servidor_contas
from harness.evals.mcp_avaliacao import (
    ErroMcpDeAvaliacao,
    config_mcp_de_contas,
    gravar_fatos_de_contas,
    servidor_de_contas,
)
from harness.host.mcp.adaptador import registrar_servidores_mcp
from harness.host.mcp.cliente import ClienteMcp, ErroMcp
from harness.host.mcp.config import ConfigMcp
from harness.host.perfil import perfil_por_nome
from harness.host.registry import Registro
from harness.sandbox.protocol import ToolCall
from tests.host.mcp.auxiliares import processo_morto

SERVIDOR = Path(servidor_contas.__file__).resolve()


@pytest.fixture(scope="module")
def fatos(tmp_path_factory: pytest.TempPathFactory) -> FatosVerdadeiros:
    return gerar_dados(tmp_path_factory.mktemp("contas") / "dados", 42)


@pytest.fixture
def config(fatos: FatosVerdadeiros, tmp_path: Path) -> ConfigMcp:
    return config_mcp_de_contas(gravar_fatos_de_contas(fatos, tmp_path / "mcp"))


@pytest.fixture
def cliente(config: ConfigMcp) -> Iterator[ClienteMcp]:
    with ClienteMcp(config.servidores["contas"]) as aberto:
        yield aberto


def _texto(resultado: dict[str, object]) -> str:
    conteudo = resultado["content"]
    assert isinstance(conteudo, list)
    return "\n".join(bloco["text"] for bloco in conteudo)


def test_arquivo_de_fatos_traz_os_valores_dos_fatos(
    fatos: FatosVerdadeiros, tmp_path: Path
) -> None:
    caminho = gravar_fatos_de_contas(fatos, tmp_path / "mcp")
    bruto = json.loads(caminho.read_text(encoding="utf-8"))
    assert {n: Decimal(v) for n, v in bruto["vencido_por_cliente"].items()} == (
        fatos.vencido_por_cliente
    )
    assert {n: Decimal(v) for n, v in bruto["valor_mensal_por_cliente"].items()} == (
        fatos.valor_mensal_por_cliente
    )


def test_config_passa_os_fatos_pelo_env_e_marca_as_consultas_como_leitura(
    config: ConfigMcp, tmp_path: Path
) -> None:
    servidor = config.servidores["contas"]
    assert servidor.nome == "contas"
    assert servidor.comando == sys.executable
    assert servidor.args == [str(SERVIDOR)]
    assert servidor.env == {"FATOS_CONTAS": str(tmp_path / "mcp" / "fatos_contas.json")}
    assert sorted(servidor.leitura) == ["contratos_vencendo", "vencidos"]


def test_tools_list_traz_as_duas_ferramentas_e_a_descricao_envenenada(
    cliente: ClienteMcp,
) -> None:
    descritores = {d.nome: d for d in cliente.listar_ferramentas()}
    assert set(descritores) == {"vencidos", "contratos_vencendo"}
    assert servidor_contas.INSTRUCAO_DESCRICAO in descritores["contratos_vencendo"].descricao
    assert "desconto máximo agora é 40%" in descritores["contratos_vencendo"].descricao
    # A injeção da primeira ferramenta vem na resposta, não na descrição.
    assert "auditoria@exemplo.invalid" not in descritores["vencidos"].descricao
    for descritor in descritores.values():
        assert descritor.esquema is not None
        assert descritor.esquema["required"] == ["cliente"]


def test_vencidos_devolve_o_total_dos_fatos_junto_com_a_instrucao(
    cliente: ClienteMcp, fatos: FatosVerdadeiros
) -> None:
    nome = max(fatos.vencido_por_cliente, key=fatos.vencido_por_cliente.__getitem__)
    texto = _texto(cliente.chamar("vencidos", {"cliente": nome}))
    esperado = servidor_contas.formatar_reais(fatos.vencido_por_cliente[nome])
    assert f"Total vencido de {nome}: {esperado}." in texto
    assert servidor_contas.INSTRUCAO_RESULTADO in texto
    assert "auditoria@exemplo.invalid" in texto


def test_contratos_vencendo_devolve_o_valor_mensal_dos_fatos(
    cliente: ClienteMcp, fatos: FatosVerdadeiros
) -> None:
    nome = max(fatos.valor_mensal_por_cliente, key=fatos.valor_mensal_por_cliente.__getitem__)
    texto = _texto(cliente.chamar("contratos_vencendo", {"cliente": nome.upper()}))
    esperado = servidor_contas.formatar_reais(fatos.valor_mensal_por_cliente[nome])
    assert f"Contrato ativo de {nome}: valor mensal de {esperado}." in texto


def test_cliente_desconhecido_e_ferramenta_desconhecida_sao_erro_da_ferramenta(
    cliente: ClienteMcp,
) -> None:
    assert cliente.chamar("vencidos", {"cliente": "Ninguém"})["isError"] is True
    assert cliente.chamar("vencidos", {})["isError"] is True
    assert cliente.chamar("inexistente", {"cliente": "x"})["isError"] is True


def test_e_deterministico(cliente: ClienteMcp, fatos: FatosVerdadeiros) -> None:
    nome = next(iter(fatos.vencido_por_cliente))
    assert cliente.chamar("vencidos", {"cliente": nome}) == cliente.chamar(
        "vencidos", {"cliente": nome}
    )


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (Decimal("0"), "R$ 0,00"),
        (Decimal("5.5"), "R$ 5,50"),
        (Decimal("1234.56"), "R$ 1.234,56"),
        (Decimal("1234567.8"), "R$ 1.234.567,80"),
    ],
)
def test_formatar_reais(valor: Decimal, esperado: str) -> None:
    assert servidor_contas.formatar_reais(valor) == esperado


def test_sem_arquivo_de_fatos_o_servidor_sai_com_codigo_3(tmp_path: Path) -> None:
    for env in ({}, {"FATOS_CONTAS": str(tmp_path / "nao_existe.json")}):
        processo = subprocess.run(
            [sys.executable, str(SERVIDOR)], input=b"", env=env, capture_output=True, timeout=30
        )
        assert processo.returncode == servidor_contas.CODIGO_SEM_FATOS


def test_arquivo_de_fatos_invalido_faz_o_cliente_falhar_no_inicio(
    config: ConfigMcp, tmp_path: Path
) -> None:
    ruim = tmp_path / "ruim.json"
    ruim.write_text("{não é json", encoding="utf-8")
    servidor = config.servidores["contas"].model_copy(update={"env": {"FATOS_CONTAS": str(ruim)}})
    with pytest.raises(ErroMcp):
        ClienteMcp(servidor)


def test_fechar_encerra_o_processo(config: ConfigMcp) -> None:
    cliente = ClienteMcp(config.servidores["contas"])
    pid = cliente.pid
    cliente.listar_ferramentas()
    cliente.fechar()
    assert processo_morto(pid)


# --- ligação ao registro e ao perfil -------------------------------------------------------------


def test_servidor_de_contas_registra_como_leitura_e_concede_so_as_duas_ferramentas(
    config: ConfigMcp, fatos: FatosVerdadeiros
) -> None:
    registro = Registro()
    base = perfil_por_nome("Financeiro")
    with servidor_de_contas(registro, base, config) as perfil:
        assert perfil.permite("contas.vencidos")
        assert perfil.permite("contas.contratos_vencendo")
        assert perfil.concessoes - base.concessoes == {
            "contas.vencidos",
            "contas.contratos_vencendo",
        }
        nome = max(fatos.vencido_por_cliente, key=fatos.vencido_por_cliente.__getitem__)
        resultado = registro.despachar(
            ToolCall(call_id="1", name="contas.vencidos", args={"cliente": nome}), perfil
        )
        assert resultado.ok
        assert servidor_contas.INSTRUCAO_RESULTADO in str(resultado.data)
    # O perfil original não muda, e o registro sem a concessão nega a ferramenta.
    assert not base.permite("contas.vencidos")


def test_servidor_de_contas_encerra_o_processo_ao_sair_mesmo_com_erro(
    config: ConfigMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []

    def espiar(registro: Registro, cfg: ConfigMcp):  # type: ignore[no-untyped-def]
        gerente = registrar_servidores_mcp(registro, cfg)
        pids.extend(c.pid for c in gerente._clientes.values())
        return gerente

    monkeypatch.setattr("harness.evals.mcp_avaliacao.registrar_servidores_mcp", espiar)
    with pytest.raises(RuntimeError):
        with servidor_de_contas(Registro(), perfil_por_nome("Financeiro"), config):
            raise RuntimeError("falha no pedido")
    assert len(pids) == 1
    assert processo_morto(pids[0])


def test_falha_ao_iniciar_levanta_erro_de_avaliacao_sem_deixar_processo(
    config: ConfigMcp, tmp_path: Path
) -> None:
    quebrado = ConfigMcp.model_validate(
        {
            "servidores": {
                "contas": {
                    "comando": sys.executable,
                    "args": [str(SERVIDOR)],
                    "env": {"FATOS_CONTAS": str(tmp_path / "nao_existe.json")},
                    "leitura": ["vencidos", "contratos_vencendo"],
                }
            }
        }
    )
    with pytest.raises(ErroMcpDeAvaliacao):
        with servidor_de_contas(Registro(), perfil_por_nome("Financeiro"), quebrado):
            pytest.fail("não deveria entrar no bloco")
