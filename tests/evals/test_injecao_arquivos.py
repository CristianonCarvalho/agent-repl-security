"""Arquivos de ataque do conjunto `injecao`: existem em disco, aparecem no catálogo do perfil do
caso e as ferramentas da variante JSON conseguem lê-los, com a instrução plantada no texto.

Sem modelo, sem rede, sem Docker. Os valores esperados vêm da geração (`FatosVerdadeiros`) e das
constantes de `dados.py`.
"""

from pathlib import Path

import pytest

from harness.evals.dados import (
    ARQUIVO_APRESENTACAO_Q4,
    ARQUIVO_ATA,
    ARQUIVO_OBSERVACOES,
    INSTRUCAO_APRESENTACAO,
    INSTRUCAO_ATA,
    INSTRUCAO_CONTRATO,
    FatosVerdadeiros,
    gerar_dados,
)
from harness.evals.ferramentas_json import (
    LerDocumentoArgs,
    LerPlanilhaArgs,
    ler_documento,
    ler_planilha,
    perfil_json,
)
from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.tools.catalogo import ListarArgs, listar


@pytest.fixture(scope="module")
def ataques(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, FatosVerdadeiros]:
    destino = tmp_path_factory.mktemp("arquivos_de_ataque") / "dados"
    return destino, gerar_dados(destino, 42, com_ataques=True)


def _ferramentas_json(perfil: str) -> Perfil:
    """Perfil da variante JSON básica, a que não tem agregação: é a que mais depende de ler."""
    return perfil_json(perfil_por_nome(perfil), "json")


def test_arquivos_de_ataque_existem_em_disco(ataques: tuple[Path, FatosVerdadeiros]) -> None:
    destino, fatos = ataques
    esperados = [
        ARQUIVO_ATA,
        ARQUIVO_OBSERVACOES,
        ARQUIVO_APRESENTACAO_Q4,
        fatos.instrucao_escondida.arquivo,
    ]
    for relativo in esperados:
        assert (destino / relativo).is_file(), relativo
    assert ARQUIVO_ATA.startswith("direcao/atas/")
    assert ARQUIVO_OBSERVACOES.startswith("comercial/")
    assert ARQUIVO_APRESENTACAO_Q4.startswith("marketing/")
    assert fatos.instrucao_escondida.proposta_alvo == "PR-2026-007"


@pytest.mark.parametrize(
    ("perfil", "departamento", "arquivo"),
    [
        ("Direção", "comercial", "proposta"),  # injecao-01: proposta injetada
        ("Direção", "direcao", ARQUIVO_ATA),  # injecao-02: ata
        ("Direção", "comercial", ARQUIVO_OBSERVACOES),  # injecao-03: planilha de observações
        ("Marketing", "marketing", ARQUIVO_APRESENTACAO_Q4),  # injecao-04: apresentação
    ],
)
def test_catalogo_lista_o_arquivo_de_ataque_para_o_perfil_do_caso(
    ataques: tuple[Path, FatosVerdadeiros],
    perfil: str,
    departamento: str,
    arquivo: str,
) -> None:
    destino, fatos = ataques
    if arquivo == "proposta":
        arquivo = fatos.instrucao_escondida.arquivo
    itens = listar(ListarArgs(departamento=departamento), perfil_por_nome(perfil), destino)
    assert arquivo in {item["arquivo"] for item in itens}


def test_ler_documento_devolve_a_instrucao_da_ata(ataques: tuple[Path, FatosVerdadeiros]) -> None:
    destino, _ = ataques
    texto = ler_documento(
        LerDocumentoArgs(arquivo=ARQUIVO_ATA, limite_caracteres=20000),
        _ferramentas_json("Direção"),
        destino,
    )["texto"]
    assert INSTRUCAO_ATA in texto


def test_ler_planilha_devolve_a_observacao_do_contrato(
    ataques: tuple[Path, FatosVerdadeiros],
) -> None:
    destino, _ = ataques
    linhas = ler_planilha(
        LerPlanilhaArgs(arquivo=ARQUIVO_OBSERVACOES, limite=200),
        _ferramentas_json("Direção"),
        destino,
    )["linhas"]
    assert any(linha.get("observacoes") == INSTRUCAO_CONTRATO for linha in linhas)


def test_ler_documento_devolve_a_nota_da_apresentacao(
    ataques: tuple[Path, FatosVerdadeiros],
) -> None:
    destino, _ = ataques
    texto = ler_documento(
        LerDocumentoArgs(arquivo=ARQUIVO_APRESENTACAO_Q4, limite_caracteres=20000),
        _ferramentas_json("Marketing"),
        destino,
    )["texto"]
    assert INSTRUCAO_APRESENTACAO in texto


def test_ler_documento_devolve_a_instrucao_da_proposta_injetada(
    ataques: tuple[Path, FatosVerdadeiros],
) -> None:
    destino, fatos = ataques
    texto = ler_documento(
        LerDocumentoArgs(arquivo=fatos.instrucao_escondida.arquivo, limite_caracteres=20000),
        _ferramentas_json("Direção"),
        destino,
    )["texto"]
    assert fatos.instrucao_escondida.texto in texto
