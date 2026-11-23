"""Testes da procedência (T17c): campos novos, hash por caso, git e linhas de base.

Nenhum teste depende do estado real do repositório. O git só aparece em um repositório
temporário criado na pasta do teste, e a versão real de `obter_git` nunca levanta exceção.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao, casos_para_json
from harness.evals.conjunto import VERSAO_CONJUNTO, casos_ativos
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.executor import ConfigExecucao, orcamento_usado
from harness.evals.procedencia import (
    ErroDeLinhaDeBase,
    caminho_da_linha_de_base,
    escrever_linha_de_base,
    obter_git,
    sanear_relatorio,
    validar_nome_da_linha,
)
from harness.evals.relatorio import (
    Classificacao,
    ErroDeRelatorio,
    Relatorio,
    ResumoCaso,
    RodadaCaso,
    agregar,
    hash_do_caso,
    relatorio_de_json,
    relatorio_para_json,
)

RAIZ_REPO: Path = Path(__file__).resolve().parents[2]
RELATORIO_ANTIGO: Path = (
    RAIZ_REPO / "docs/relatorios/dados/avaliacao-20261123-091302-semente42.json"
)
CHAVE_FALSA: str = "chave-falsa-procedencia-777bbb"


@pytest.fixture(scope="module")
def fatos(tmp_path_factory: pytest.TempPathFactory) -> FatosVerdadeiros:
    return gerar_dados(tmp_path_factory.mktemp("procedencia") / "dados", 42)


def _caso(caso_id: str = "consulta-01", pedido: str = "Qual é o valor? Cite a fonte.") -> Caso:
    return Caso(
        id=caso_id,
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido=pedido,
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )


def _rodada(numero: int, aprovada: bool) -> RodadaCaso:
    veredito = Veredito(
        caso_id="consulta-01",
        aprovado=aprovada,
        regras=(ResultadoRegra(descricao="regra", passou=aprovada, detalhe="ok"),),
    )
    classificacao = Classificacao.APROVADA if aprovada else Classificacao.REPROVADA
    return RodadaCaso(numero=numero, classificacao=classificacao, veredito=veredito, iteracoes=2)


def _relatorio() -> Relatorio:
    rodadas = [_rodada(1, True), _rodada(2, True), _rodada(3, False)]
    return agregar(
        [_caso()],
        {"consulta-01": rodadas},
        semente=42,
        rodadas_por_caso=3,
        modelo="modelo-de-teste",
        provedores=("provedor-a",),
        orcamento={"max_iteracoes": 15, "max_segundos": 300.0},
    )


# --- campos novos e compatibilidade -----------------------------------------------------------


def test_relatorio_novo_carrega_o_orcamento_e_o_hash_do_caso() -> None:
    relatorio = _relatorio()

    assert relatorio.orcamento == {"max_iteracoes": 15, "max_segundos": 300.0}
    assert relatorio.casos[0].hash_caso == hash_do_caso(_caso())
    assert relatorio.commit is None
    assert relatorio.versao_conjunto is None


def test_ida_e_volta_json_nao_perde_os_campos_de_procedencia() -> None:
    relatorio = _relatorio().model_copy(
        update={
            "versao_conjunto": VERSAO_CONJUNTO,
            "commit": "a" * 40,
            "commit_sujo": True,
            "iniciado_em": "2026-11-23T10:00:00+00:00",
            "linha_de_base": "base-x",
        }
    )

    de_volta = relatorio_de_json(relatorio_para_json(relatorio))

    assert de_volta == relatorio
    assert de_volta.commit_sujo is True
    assert de_volta.linha_de_base == "base-x"


def test_relatorio_antigo_sem_os_campos_novos_continua_carregando() -> None:
    relatorio = relatorio_de_json(RELATORIO_ANTIGO.read_text(encoding="utf-8"))

    assert relatorio.semente == 42
    assert relatorio.versao_conjunto is None
    assert relatorio.commit is None
    assert relatorio.commit_sujo is None
    assert relatorio.orcamento is None
    assert relatorio.iniciado_em is None
    assert relatorio.linha_de_base is None
    assert all(caso.hash_caso is None for caso in relatorio.casos)


# --- hash por caso ----------------------------------------------------------------------------


def test_hash_do_caso_e_estavel_e_muda_quando_o_caso_muda(fatos: FatosVerdadeiros) -> None:
    caso = casos_ativos(fatos)[0]
    igual = caso.model_copy()
    mudado = caso.model_copy(update={"pedido": caso.pedido + " Responda em uma linha."})

    assert hash_do_caso(caso) == hash_do_caso(igual)
    assert hash_do_caso(caso) != hash_do_caso(mudado)
    assert len(hash_do_caso(caso)) == 64


def test_hash_do_caso_e_o_sha256_do_json_canonico_do_caso() -> None:
    import hashlib

    caso = _caso()
    esperado = hashlib.sha256(casos_para_json([caso]).encode("utf-8")).hexdigest()

    assert hash_do_caso(caso) == esperado


def test_resumo_por_caso_recebe_o_hash_no_agregar() -> None:
    caso = _caso()
    mudado = _caso(pedido="Outra pergunta? Cite a fonte.")
    relatorio = agregar([caso], {caso.id: [_rodada(1, True)]}, semente=42, rodadas_por_caso=1)
    outro = agregar([mudado], {mudado.id: [_rodada(1, True)]}, semente=42, rodadas_por_caso=1)

    assert isinstance(relatorio.casos[0], ResumoCaso)
    assert relatorio.casos[0].hash_caso != outro.casos[0].hash_caso


def test_orcamento_usado_traz_os_padroes_quando_nao_ha_configuracao() -> None:
    usado = orcamento_usado(ConfigExecucao())

    assert usado == {
        "max_iteracoes": 15,
        "max_tokens": 200_000,
        "max_segundos": 300.0,
        "timeout_celula_s": 30.0,
        "max_invalidos_seguidos": 3,
    }


# --- git -------------------------------------------------------------------------------------


def test_obter_git_fora_de_repositorio_devolve_none_sem_levantar(tmp_path: Path) -> None:
    assert obter_git(tmp_path) == (None, None)


def test_obter_git_em_pasta_inexistente_devolve_none_sem_levantar(tmp_path: Path) -> None:
    assert obter_git(tmp_path / "nao-existe") == (None, None)


@pytest.mark.skipif(shutil.which("git") is None, reason="git não instalado")
def test_obter_git_num_repositorio_temporario_limpo_e_sujo(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=teste",
                "-c",
                "user.email=teste@example.invalid",
                *args,
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    git("commit", "-q", "--allow-empty", "-m", "inicial")
    commit = git("rev-parse", "HEAD")

    assert obter_git(tmp_path) == (commit, False)
    (tmp_path / "alterado.txt").write_text("x", encoding="utf-8")
    assert obter_git(tmp_path) == (commit, True)


# --- nomes e gravação da linha de base ---------------------------------------------------------


@pytest.mark.parametrize("nome", ["base-1", "modelo.v2_x", "0a", "x"])
def test_nomes_validos_passam(nome: str) -> None:
    assert validar_nome_da_linha(nome) == nome


@pytest.mark.parametrize("nome", ["", "Base", "../fora", "com espaco", "-inicio", "a/b", "ç"])
def test_nomes_invalidos_sao_recusados(nome: str) -> None:
    with pytest.raises(ErroDeLinhaDeBase):
        validar_nome_da_linha(nome)


def test_caminho_da_linha_fica_em_evals_linhas_de_base(tmp_path: Path) -> None:
    assert caminho_da_linha_de_base("base-1", tmp_path) == (
        tmp_path / "evals" / "linhas-de-base" / "base-1.json"
    )


def test_linha_de_base_e_sanitizada_e_carrega_o_nome(tmp_path: Path) -> None:
    relatorio = _relatorio()
    rodadas = tuple(
        rodada.model_copy(update={"caminho_trace": "/Users/alguem/traces/x.jsonl"})
        for rodada in relatorio.casos[0].rodadas
    )
    com_trace = relatorio.model_copy(
        update={"casos": (relatorio.casos[0].model_copy(update={"rodadas": rodadas}),)}
    )

    gravado = escrever_linha_de_base(com_trace, "base-1", tmp_path)
    texto = gravado.read_text(encoding="utf-8")
    de_volta = relatorio_de_json(texto)

    assert gravado == tmp_path / "evals" / "linhas-de-base" / "base-1.json"
    assert de_volta.linha_de_base == "base-1"
    assert all(r.caminho_trace is None for c in de_volta.casos for r in c.rodadas)
    assert "/Users/alguem" not in texto
    # O original não muda: o sanear devolve uma cópia.
    assert com_trace.casos[0].rodadas[0].caminho_trace == "/Users/alguem/traces/x.jsonl"
    assert sanear_relatorio(com_trace, "base-1").linha_de_base == "base-1"


def test_linha_de_base_nunca_sobrescreve(tmp_path: Path) -> None:
    escrever_linha_de_base(_relatorio(), "base-1", tmp_path)

    with pytest.raises(ErroDeLinhaDeBase, match="já existe"):
        escrever_linha_de_base(_relatorio(), "base-1", tmp_path)


def test_segredo_no_texto_impede_a_gravacao_sem_citar_o_valor(tmp_path: Path) -> None:
    com_segredo = _relatorio().model_copy(update={"modelo": f"modelo {CHAVE_FALSA}"})

    with pytest.raises(ErroDeLinhaDeBase) as erro:
        escrever_linha_de_base(com_segredo, "base-1", tmp_path, segredos=[CHAVE_FALSA])

    assert CHAVE_FALSA not in str(erro.value)
    assert not caminho_da_linha_de_base("base-1", tmp_path).exists()


def test_falha_de_io_ao_gravar_vira_erro_de_relatorio(tmp_path: Path) -> None:
    bloqueio = tmp_path / "evals"
    bloqueio.write_text("arquivo no lugar da pasta", encoding="utf-8")

    with pytest.raises(ErroDeRelatorio):
        escrever_linha_de_base(_relatorio(), "base-1", tmp_path)
