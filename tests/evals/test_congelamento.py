"""Testes do conjunto congelado (T17c): o arquivo versionado é o que o código gera hoje.

Se pergunta ou gabarito mudarem, o teste de igualdade falha. A mudança exige uma versão nova
(`congelar --versao v2`) e a troca de `VERSAO_CONJUNTO`, e não uma edição do arquivo v1.
"""

from pathlib import Path

import pytest

from harness.evals.casos import Caso, casos_de_json, casos_para_json, validar_conjunto
from harness.evals.congelamento import (
    ErroDeCongelamento,
    caminho_do_conjunto,
    congelar,
    ler_congelado,
)
from harness.evals.conjunto import VERSAO_CONJUNTO, conjunto_completo
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.procedencia import RAIZ_DO_REPOSITORIO

ARQUIVO_CONGELADO: Path = RAIZ_DO_REPOSITORIO / "evals" / "conjuntos" / "v2-semente42.json"


@pytest.fixture(scope="module")
def fatos(tmp_path_factory: pytest.TempPathFactory) -> FatosVerdadeiros:
    return gerar_dados(tmp_path_factory.mktemp("congelamento") / "dados", 42)


def test_conjunto_gerado_pelo_codigo_e_igual_ao_arquivo_versionado(
    fatos: FatosVerdadeiros,
) -> None:
    gerado = casos_para_json(conjunto_completo(fatos)) + "\n"
    versionado = ARQUIVO_CONGELADO.read_text(encoding="utf-8")

    assert VERSAO_CONJUNTO == "v2"
    assert gerado == versionado, (
        "O conjunto de casos mudou em relação ao arquivo congelado evals/conjuntos/"
        "v2-semente42.json. Mudar pergunta, gabarito ou perfil exige uma versão nova: rode "
        '`python -m harness.evals congelar --versao v3`, troque VERSAO_CONJUNTO para "v3" e '
        "mantenha o arquivo v2 intacto. Não edite o arquivo congelado à mão."
    )


def test_arquivo_versionado_carrega_como_lista_de_casos_valida() -> None:
    casos = ler_congelado(VERSAO_CONJUNTO, 42, RAIZ_DO_REPOSITORIO)

    assert len(casos) == 35
    assert sum(1 for caso in casos if caso.situacao.ativo) == 20
    validar_conjunto(casos)
    assert all(isinstance(caso, Caso) for caso in casos)


def test_congelar_grava_o_mesmo_conteudo_em_pasta_temporaria(
    tmp_path: Path, fatos: FatosVerdadeiros
) -> None:
    caminho = congelar("v2", 42, tmp_path)

    assert caminho == tmp_path / "evals" / "conjuntos" / "v2-semente42.json"
    assert caminho.read_text(encoding="utf-8") == ARQUIVO_CONGELADO.read_text(encoding="utf-8")
    assert caminho.read_text(encoding="utf-8").endswith("\n")
    assert [c.id for c in ler_congelado("v2", 42, tmp_path)] == [
        c.id for c in conjunto_completo(fatos)
    ]


def test_congelar_recusa_sobrescrever(tmp_path: Path) -> None:
    congelar("v1", 42, tmp_path)

    with pytest.raises(ErroDeCongelamento, match="não sobrescrevo"):
        congelar("v1", 42, tmp_path)


def test_ler_congelado_sem_arquivo_da_erro_claro(tmp_path: Path) -> None:
    with pytest.raises(ErroDeCongelamento, match="não encontrado"):
        ler_congelado("v9", 42, tmp_path)


def test_ler_congelado_com_arquivo_invalido_da_erro_claro(tmp_path: Path) -> None:
    caminho = caminho_do_conjunto("v1", 42, tmp_path)
    caminho.parent.mkdir(parents=True)
    caminho.write_text('[{"id": "errado"}]', encoding="utf-8")

    with pytest.raises(ErroDeCongelamento, match="inválido"):
        ler_congelado("v1", 42, tmp_path)


@pytest.mark.parametrize("versao", ["1", "v", "V1", "v1/../x", "versao-1", "v1 "])
def test_versao_fora_do_formato_e_recusada_antes_de_tocar_no_disco(
    tmp_path: Path, versao: str
) -> None:
    with pytest.raises(ErroDeCongelamento, match="versão inválida"):
        congelar(versao, 42, tmp_path)
    assert not (tmp_path / "evals").exists()


def test_caminho_do_conjunto_segue_o_padrao(tmp_path: Path) -> None:
    assert caminho_do_conjunto("v3", 7, tmp_path) == (
        tmp_path / "evals" / "conjuntos" / "v3-semente7.json"
    )


def test_texto_congelado_le_de_volta_sem_perda(fatos: FatosVerdadeiros) -> None:
    casos = conjunto_completo(fatos)

    assert casos_de_json(casos_para_json(casos)) == casos
