"""Testes da configuração do OpenRouter e do leitor de `.env` (Tarefa 9). Sem rede."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.llm.config import ConfigModelo, carregar_env, config_de_ambiente

# --- ConfigModelo ---------------------------------------------------------------------------


def test_slug_e_obrigatorio_sem_valor_padrao() -> None:
    with pytest.raises(ValidationError):
        ConfigModelo()  # type: ignore[call-arg]


def test_slug_vazio_e_recusado() -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="")


def test_valores_padrao() -> None:
    config = ConfigModelo(slug="deepseek/x")
    assert config.base_url == "https://openrouter.ai/api/v1"
    assert config.temperatura == 0.0
    assert config.seed is None
    assert config.fallback_provedor is False
    assert config.timeout_s == 60
    assert config.max_tentativas == 3
    assert config.max_tokens_saida is None


@pytest.mark.parametrize("temperatura", [0.0, 0.2, 1.0, 2.0])
def test_temperatura_dentro_da_faixa_e_aceita(temperatura: float) -> None:
    assert ConfigModelo(slug="x", temperatura=temperatura).temperatura == temperatura


@pytest.mark.parametrize("temperatura", [-0.1, 2.01, float("nan")])
def test_temperatura_fora_da_faixa_e_recusada(temperatura: float) -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="x", temperatura=temperatura)


def test_max_tentativas_minimo_e_um() -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="x", max_tentativas=0)


def test_timeout_precisa_ser_positivo() -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="x", timeout_s=0)


def test_max_tokens_saida_precisa_ser_positivo_quando_definido() -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="x", max_tokens_saida=0)


def test_configuracao_e_imutavel() -> None:
    config = ConfigModelo(slug="x")
    with pytest.raises(ValidationError):
        config.slug = "outro"


# --- config_de_ambiente ---------------------------------------------------------------------


def test_slug_do_ambiente_vira_slug_da_config() -> None:
    config = config_de_ambiente({"OPENROUTER_MODELO": "  deepseek/deepseek-v4.1-flash  "})
    assert config.slug == "deepseek/deepseek-v4.1-flash"
    assert config.temperatura == 0.0
    assert config.fallback_provedor is False


def test_sem_slug_levanta_erro_claro_citando_a_variavel() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_MODELO"):
        config_de_ambiente({})


def test_slug_so_com_espacos_conta_como_ausente() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_MODELO"):
        config_de_ambiente({"OPENROUTER_MODELO": "   "})


def test_opcionais_temperatura_seed_e_fallback_sao_lidos() -> None:
    config = config_de_ambiente(
        {
            "OPENROUTER_MODELO": "x",
            "OPENROUTER_TEMPERATURA": "0.1",
            "OPENROUTER_SEED": "42",
            "OPENROUTER_FALLBACK": "sim",
        }
    )
    assert config.temperatura == 0.1
    assert config.seed == 42
    assert config.fallback_provedor is True


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [("1", True), ("true", True), ("SIM", True), ("0", False), ("false", False), ("não", False)],
)
def test_fallback_aceita_formas_comuns_de_booleano(bruto: str, esperado: bool) -> None:
    config = config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_FALLBACK": bruto})
    assert config.fallback_provedor is esperado


def test_variaveis_opcionais_vazias_contam_como_ausentes() -> None:
    config = config_de_ambiente(
        {
            "OPENROUTER_MODELO": "x",
            "OPENROUTER_TEMPERATURA": "",
            "OPENROUTER_SEED": "  ",
            "OPENROUTER_FALLBACK": "",
        }
    )
    assert config == ConfigModelo(slug="x")


def test_temperatura_invalida_citada_pelo_nome_sem_expor_o_valor() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_TEMPERATURA deve ser um número") as erro:
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_TEMPERATURA": "quente"})
    assert "quente" not in str(erro.value)


def test_seed_invalida_levanta_erro_claro() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_SEED deve ser um inteiro"):
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_SEED": "1.5"})


def test_fallback_invalido_levanta_erro_claro() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_FALLBACK"):
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_FALLBACK": "talvez"})


def test_temperatura_fora_da_faixa_no_ambiente_e_recusada() -> None:
    with pytest.raises(ValidationError):
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_TEMPERATURA": "3"})


# --- carregar_env ---------------------------------------------------------------------------


def test_arquivo_ausente_devolve_dicionario_vazio(tmp_path: Path) -> None:
    assert carregar_env(tmp_path / "nao_existe.env") == {}


def test_diretorio_no_lugar_do_arquivo_devolve_dicionario_vazio(tmp_path: Path) -> None:
    assert carregar_env(tmp_path) == {}


def test_le_pares_ignora_comentarios_e_linhas_vazias(tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    arquivo.write_text(
        "# comentário\n\nOPENROUTER_MODELO=deepseek/x\n   \n# outro\nOUTRA=valor\n",
        encoding="utf-8",
    )
    assert carregar_env(arquivo) == {"OPENROUTER_MODELO": "deepseek/x", "OUTRA": "valor"}


def test_tira_aspas_simples_e_duplas(tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    arquivo.write_text("A=\"duplas\"\nB='simples'\nC=sem aspas\n", encoding="utf-8")
    assert carregar_env(arquivo) == {"A": "duplas", "B": "simples", "C": "sem aspas"}


def test_valor_pode_conter_sinal_de_igual_e_aceita_export(tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    arquivo.write_text("export URL=https://x.test/?a=1&b=2\n", encoding="utf-8")
    assert carregar_env(arquivo) == {"URL": "https://x.test/?a=1&b=2"}


def test_linha_sem_igual_ou_sem_chave_e_ignorada(tmp_path: Path) -> None:
    arquivo = tmp_path / ".env"
    arquivo.write_text("SEM_IGUAL\n=sem_chave\nOK=1\n", encoding="utf-8")
    assert carregar_env(arquivo) == {"OK": "1"}
