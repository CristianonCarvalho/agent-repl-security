"""Testes da configuração do OpenRouter e do leitor de `.env` (Tarefa 9). Sem rede."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.llm.config import (
    ConfigModelo,
    carregar_env,
    config_de_ambiente,
    provedor_de_ambiente,
)

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


# --- prazo_total_s e OPENROUTER_PRAZO_TOTAL -------------------------------------------------


def test_prazo_total_padrao_e_120_segundos() -> None:
    assert ConfigModelo(slug="x").prazo_total_s == 120.0


def test_prazo_total_igual_ao_timeout_e_aceito() -> None:
    assert ConfigModelo(slug="x", timeout_s=60.0, prazo_total_s=60.0).prazo_total_s == 60.0


def test_prazo_total_menor_que_timeout_falha_com_mensagem_clara() -> None:
    with pytest.raises(ValidationError, match="prazo_total_s não pode ser menor que timeout_s"):
        ConfigModelo(slug="x", timeout_s=150.0)


@pytest.mark.parametrize("prazo", [0, -1.0, float("inf"), float("nan")])
def test_prazo_total_precisa_ser_positivo_e_finito(prazo: float) -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="x", prazo_total_s=prazo)


def test_prazo_total_do_ambiente_e_lido() -> None:
    config = config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PRAZO_TOTAL": " 300 "})
    assert config.prazo_total_s == 300.0


def test_prazo_total_vazio_no_ambiente_conta_como_ausente() -> None:
    config = config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PRAZO_TOTAL": " "})
    assert config.prazo_total_s == 120.0


@pytest.mark.parametrize("bruto", ["rápido", "1.5.2"])
def test_prazo_total_invalido_cita_so_o_nome_da_variavel(bruto: str) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_PRAZO_TOTAL deve ser um número") as erro:
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PRAZO_TOTAL": bruto})
    assert bruto not in str(erro.value)


@pytest.mark.parametrize("bruto", ["0", "-5", "nan", "inf"])
def test_prazo_total_nao_positivo_ou_nao_finito_no_ambiente_e_recusado(bruto: str) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_PRAZO_TOTAL deve ser um número positivo"):
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PRAZO_TOTAL": bruto})


def test_prazo_total_menor_que_o_timeout_padrao_e_recusado_no_ambiente() -> None:
    with pytest.raises(ValidationError, match="prazo_total_s"):
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PRAZO_TOTAL": "30"})


# --- Provedor fixo (OPENROUTER_PROVEDOR) ----------------------------------------------------


def test_provedor_padrao_e_none() -> None:
    assert ConfigModelo(slug="x").provedor is None


def test_provedor_do_ambiente_e_lido() -> None:
    config = config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PROVEDOR": " Wafer "})
    assert config.provedor == "Wafer"


@pytest.mark.parametrize("bruto", ["", "   ", None])
def test_provedor_vazio_ou_ausente_vira_none(bruto: str | None) -> None:
    ambiente: dict[str, str] = {"OPENROUTER_MODELO": "x"}
    if bruto is not None:
        ambiente["OPENROUTER_PROVEDOR"] = bruto
    assert config_de_ambiente(ambiente).provedor is None
    assert provedor_de_ambiente(ambiente) is None


@pytest.mark.parametrize(
    "nome",
    ["Wafer", "Novita AI", "DeepInfra", "Together-2.0", "a_b", "x" * 64],
)
def test_nomes_de_provedor_validos_sao_aceitos(nome: str) -> None:
    assert ConfigModelo(slug="x", provedor=nome).provedor == nome


@pytest.mark.parametrize(
    "invalido",
    ["a/b", 'a"b', "-inicio", "x" * 65, "a;b", "Wa\nfer", "a'b", "a\\b"],
)
def test_nome_de_provedor_invalido_e_recusado_sem_ecoar_o_valor(invalido: str) -> None:
    with pytest.raises(ValueError, match="OPENROUTER_PROVEDOR inválido") as erro:
        config_de_ambiente({"OPENROUTER_MODELO": "x", "OPENROUTER_PROVEDOR": invalido})
    assert "OPENROUTER_PROVEDOR" in str(erro.value)
    with pytest.raises(ValueError, match="OPENROUTER_PROVEDOR inválido"):
        provedor_de_ambiente({"OPENROUTER_PROVEDOR": invalido})


def test_provedor_invalido_no_modelo_e_recusado() -> None:
    with pytest.raises(ValidationError):
        ConfigModelo(slug="x", provedor="a/b")


def test_repr_da_configuracao_nao_traz_chave() -> None:
    chave = "chave-falsa-de-teste-789"
    config = config_de_ambiente(
        {"OPENROUTER_MODELO": "x", "OPENROUTER_PROVEDOR": "Wafer", "OPENROUTER_API_KEY": chave}
    )
    assert chave not in repr(config)
    assert chave not in str(config)
