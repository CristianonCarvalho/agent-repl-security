"""Testes da configuração MCP: formato válido, formato inválido e mensagens sem valores."""

import json
from pathlib import Path
from typing import Any

import pytest

from harness.host.mcp.config import ConfigMcp, ErroConfigMcp, carregar_config_mcp


def _escrever(caminho: Path, dados: Any) -> Path:
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    return caminho


def test_formato_valido_usa_a_chave_como_nome_e_preenche_padroes(tmp_path: Path) -> None:
    caminho = _escrever(
        tmp_path / "mcp.json",
        {
            "servidores": {
                "eco": {
                    "comando": "uv",
                    "args": ["run", "servidor.py"],
                    "env": {"EXEMPLO": "1"},
                    "leitura": ["eco"],
                }
            }
        },
    )

    config = carregar_config_mcp(caminho)

    servidor = config.servidores["eco"]
    assert servidor.nome == "eco"
    assert servidor.comando == "uv"
    assert servidor.args == ["run", "servidor.py"]
    assert servidor.env == {"EXEMPLO": "1"}
    assert servidor.leitura == ["eco"]
    assert servidor.timeout_chamada_s == 30.0
    assert servidor.timeout_inicio_s == 15.0


def test_sem_servidores_a_configuracao_e_vazia(tmp_path: Path) -> None:
    config = carregar_config_mcp(_escrever(tmp_path / "mcp.json", {"servidores": {}}))
    assert config == ConfigMcp()


def test_nome_com_digito_e_aceito_pelo_formato_da_configuracao() -> None:
    # O registro é mais estrito (ver adaptador.py); a configuração segue o padrão da spec.
    config = ConfigMcp.model_validate({"servidores": {"eco2": {"comando": "x"}}})
    assert "eco2" in config.servidores


@pytest.mark.parametrize(
    "dados",
    [
        {"servidores": {"Eco": {"comando": "x"}}},
        {"servidores": {"2eco": {"comando": "x"}}},
        {"servidores": {"eco-x": {"comando": "x"}}},
        {"servidores": {"catalogo": {"comando": "x"}}},
        {"servidores": {"descoberta": {"comando": "x"}}},
        {"servidores": {"eco": {"comando": "x", "nome": "outro"}}},
        {"servidores": {"eco": {"comando": "x", "zz": 1}}},
        {"servidores": {"eco": {"args": []}}},
        {"servidores": {"eco": {"comando": ""}}},
        {"servidores": {"eco": {"comando": "x", "env": {"A": 1}}}},
        {"servidores": {"eco": {"comando": "x", "timeout_chamada_s": 0}}},
        {"servidores": {"eco": {"comando": "x", "timeout_inicio_s": -1}}},
        {"servidores": {"eco": {"comando": "x", "leitura": "eco"}}},
        {"servidores": {"eco": "texto"}},
        {"servidores": []},
        [],
    ],
)
def test_formato_invalido_levanta_erro_de_configuracao(tmp_path: Path, dados: Any) -> None:
    with pytest.raises(ErroConfigMcp):
        carregar_config_mcp(_escrever(tmp_path / "mcp.json", dados))


def test_arquivo_ausente_tem_mensagem_propria(tmp_path: Path) -> None:
    with pytest.raises(ErroConfigMcp, match="não encontrado"):
        carregar_config_mcp(tmp_path / "nao_existe.json")


def test_json_invalido_cita_a_linha_sem_o_conteudo(tmp_path: Path) -> None:
    caminho = tmp_path / "mcp.json"
    caminho.write_text('{"servidores": {\n "eco": ', encoding="utf-8")
    with pytest.raises(ErroConfigMcp, match="JSON inválido") as erro:
        carregar_config_mcp(caminho)
    assert "linha" in str(erro.value)


def test_arquivo_que_nao_e_utf8_nao_pode_ser_lido(tmp_path: Path) -> None:
    caminho = tmp_path / "mcp.json"
    caminho.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ErroConfigMcp, match="não pode ser lido"):
        carregar_config_mcp(caminho)


def test_mensagem_de_erro_nunca_repete_o_valor_do_arquivo(tmp_path: Path) -> None:
    # `args` com texto em vez de lista: o pydantic repetiria o valor, e ele não pode sair.
    segredo = "SEGREDO-DE-TESTE-123"
    dados = {"servidores": {"eco": {"comando": "x", "args": segredo, "env": {"K": segredo}}}}
    with pytest.raises(ErroConfigMcp) as erro:
        carregar_config_mcp(_escrever(tmp_path / "mcp.json", dados))
    assert segredo not in str(erro.value)
    assert "args" in str(erro.value)


def test_nome_reservado_aparece_na_mensagem(tmp_path: Path) -> None:
    with pytest.raises(ErroConfigMcp, match="catalogo"):
        carregar_config_mcp(
            _escrever(tmp_path / "mcp.json", {"servidores": {"catalogo": {"comando": "x"}}})
        )
