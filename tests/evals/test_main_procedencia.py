"""Testes da CLI da T17c: `comparar`, `congelar` e `--salvar-linha-de-base`.

Nada aqui fala com a rede nem com o modelo. A execução e a fábrica do modelo são falsas, e as
validações da linha de base param antes de tocar nelas: a `_explode` falha o teste se for chamada.
O git é trocado por uma função injetada, e o `.env` nunca é lido (a pasta é temporária).
"""

import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.evals import __main__ as cli
from harness.evals.__main__ import (
    CODIGO_ERRO_INFRA,
    CODIGO_OK,
    CODIGO_REGRESSAO,
    CODIGO_USO,
    main,
)
from harness.evals.casos import Categoria
from harness.evals.conjunto import VERSAO_CONJUNTO
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.procedencia import RAIZ_DO_REPOSITORIO
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    ResumoCaso,
    RodadaCaso,
    relatorio_para_json,
)

CHAVE_FALSA: str = "chave-falsa-cli-procedencia-333ccc"
COMMIT: str = "c" * 40


def _explode(*_: Any, **__: Any) -> Any:
    raise AssertionError("não deveria ser chamado: a validação devia ter parado antes")


@pytest.fixture(autouse=True)
def _ambiente_limpo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pasta temporária como cwd, sem variáveis de telemetria nem chave vindas do shell."""
    monkeypatch.chdir(tmp_path)
    for nome in list(os.environ):
        if nome.startswith(("LANGFUSE", "OTEL", "OPENROUTER")):
            monkeypatch.delenv(nome, raising=False)
    monkeypatch.setenv("OPENROUTER_MODELO", "modelo-de-teste")


def _resumo(caso_id: str, taxa: float, *, grupo: str = "desenvolvimento") -> ResumoCaso:
    veredito = Veredito(
        caso_id=caso_id,
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    rodadas = tuple(
        RodadaCaso(numero=i + 1, classificacao=Classificacao.APROVADA, veredito=veredito)
        for i in range(3)
    )
    return ResumoCaso(
        caso_id=caso_id,
        categoria=Categoria.CONSULTA,
        situacao="ativo",
        rodadas=rodadas,
        taxa=taxa,
        desvio=0.0,
        grupo=grupo,  # type: ignore[arg-type]
        hash_caso=f"hash-{caso_id}",
    )


def _relatorio(taxa: float = 1.0, *, semente: int = 42, desvio: float | None = None) -> Relatorio:
    casos = tuple(_resumo(f"consulta-{i:02d}", taxa) for i in range(1, 4))
    return Relatorio(
        semente=semente,
        rodadas_por_caso=3,
        hash_casos="0" * 64,
        casos=casos,
        taxas_por_rodada=(taxa, taxa, taxa),
        desvio_entre_rodadas=desvio,
        versao_conjunto=VERSAO_CONJUNTO,
    )


def _gravar(caminho: Path, relatorio: Relatorio) -> Path:
    caminho.write_text(relatorio_para_json(relatorio), encoding="utf-8")
    return caminho


def _obter(
    commit: str | None, sujo: bool | None
) -> Callable[[Path], tuple[str | None, bool | None]]:
    def falso(raiz: Path) -> tuple[str | None, bool | None]:
        return (commit, sujo)

    return falso


# --- comparar ----------------------------------------------------------------------------------


def test_comparar_ok_sai_com_zero(tmp_path: Path) -> None:
    base = _gravar(tmp_path / "base.json", _relatorio())
    novo = _gravar(tmp_path / "novo.json", _relatorio())
    fala = io.StringIO()

    codigo = main(["comparar", str(base), str(novo)], saida=fala)

    assert codigo == CODIGO_OK
    assert "Veredito: OK" in fala.getvalue()


def test_comparar_regressao_sai_com_um(tmp_path: Path) -> None:
    base = _gravar(tmp_path / "base.json", _relatorio(1.0))
    novo = _gravar(tmp_path / "novo.json", _relatorio(0.5))
    fala = io.StringIO()

    codigo = main(["comparar", str(base), str(novo)], saida=fala)

    assert codigo == CODIGO_REGRESSAO
    assert "REGRESSÃO PROVÁVEL" in fala.getvalue()


def test_comparar_semente_diferente_sai_com_dois(tmp_path: Path) -> None:
    base = _gravar(tmp_path / "base.json", _relatorio(semente=42))
    novo = _gravar(tmp_path / "novo.json", _relatorio(semente=7))
    fala = io.StringIO()

    codigo = main(["comparar", str(base), str(novo)], saida=fala)

    assert codigo == CODIGO_USO
    assert "INCOMPARÁVEL" in fala.getvalue()


def test_comparar_formato_json_sai_json_valido(tmp_path: Path) -> None:
    base = _gravar(tmp_path / "base.json", _relatorio())
    novo = _gravar(tmp_path / "novo.json", _relatorio())
    fala = io.StringIO()

    codigo = main(["comparar", str(base), str(novo), "--formato", "json"], saida=fala)

    assert codigo == CODIGO_OK
    assert fala.getvalue().lstrip().startswith("{")
    assert '"veredito": "ok"' in fala.getvalue()


def test_comparar_arquivo_ausente_sai_com_dois(tmp_path: Path) -> None:
    novo = _gravar(tmp_path / "novo.json", _relatorio())
    fala = io.StringIO()

    codigo = main(["comparar", str(tmp_path / "nao-existe.json"), str(novo)], saida=fala)

    assert codigo == CODIGO_USO
    assert "arquivo não encontrado" in fala.getvalue()


def test_comparar_json_invalido_sai_com_dois_sem_detalhe_do_conteudo(tmp_path: Path) -> None:
    ruim = tmp_path / "ruim.json"
    ruim.write_text('{"semente": "x", "segredo": "' + CHAVE_FALSA, encoding="utf-8")
    novo = _gravar(tmp_path / "novo.json", _relatorio())
    fala = io.StringIO()

    codigo = main(["comparar", str(ruim), str(novo)], saida=fala)

    assert codigo == CODIGO_USO
    assert "não é um relatório de avaliação válido" in fala.getvalue()
    assert CHAVE_FALSA not in fala.getvalue()


def test_comparar_nao_le_o_ambiente_nem_chama_o_modelo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_ambiente_do_processo", _explode)
    monkeypatch.setattr(cli, "_fabrica_de_modelo", _explode)
    base = _gravar(tmp_path / "base.json", _relatorio())
    novo = _gravar(tmp_path / "novo.json", _relatorio())

    assert main(["comparar", str(base), str(novo)], saida=io.StringIO()) == CODIGO_OK


# --- congelar ----------------------------------------------------------------------------------


def test_congelar_cria_o_arquivo_e_recusa_sobrescrever(tmp_path: Path) -> None:
    fala = io.StringIO()
    primeira = main(["congelar", "--versao", "v9"], saida=fala, raiz=tmp_path)
    segunda = io.StringIO()
    repetida = main(["congelar", "--versao", "v9"], saida=segunda, raiz=tmp_path)

    assert primeira == CODIGO_OK
    assert "evals/conjuntos/v9-semente42.json" in fala.getvalue()
    assert (tmp_path / "evals" / "conjuntos" / "v9-semente42.json").is_file()
    assert repetida == CODIGO_USO
    assert "não sobrescrevo" in segunda.getvalue()


def test_congelar_versao_invalida_sai_com_dois(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(["congelar", "--versao", "beta"], saida=fala, raiz=tmp_path)

    assert codigo == CODIGO_USO
    assert "versão inválida" in fala.getvalue()
    assert not (tmp_path / "evals").exists()


def test_congelar_sem_versao_e_erro_de_uso(tmp_path: Path) -> None:
    assert main(["congelar"], saida=io.StringIO(), raiz=tmp_path) == CODIGO_USO


# --- --salvar-linha-de-base: validações antes de qualquer execução -----------------------------


def _argumentos_linha(*extra: str, nome: str = "base-teste") -> list[str]:
    return [
        "--rodadas",
        "3",
        "--saida",
        "avaliacoes",
        "--trace",
        "traces",
        "--salvar-linha-de-base",
        nome,
        *extra,
    ]


def _sem_execucao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_ambiente_do_processo", _explode)


@pytest.mark.parametrize("nome", ["Base", "../fora", "com espaco", "a/b"])
def test_nome_invalido_falha_antes_de_rodar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nome: str
) -> None:
    _sem_execucao(monkeypatch)
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(nome=nome),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_USO
    assert "nome de linha de base inválido" in fala.getvalue()
    assert not (tmp_path / "evals").exists()


def test_categoria_nao_e_aceita_com_linha_de_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sem_execucao(monkeypatch)
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha("--categoria", "consulta"),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_USO
    assert "conjunto completo" in fala.getvalue()


def test_grupo_nao_e_aceito_com_linha_de_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sem_execucao(monkeypatch)

    codigo = main(
        _argumentos_linha("--grupo", "reservado"),
        saida=io.StringIO(),
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_USO


def test_menos_de_tres_rodadas_falha_antes_de_rodar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sem_execucao(monkeypatch)
    fala = io.StringIO()
    argumentos = ["--rodadas", "2", "--salvar-linha-de-base", "base-teste"]

    codigo = main(
        argumentos,
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_USO
    assert "pelo menos 3 rodadas" in fala.getvalue()


def test_arquivo_ja_existente_falha_antes_de_rodar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sem_execucao(monkeypatch)
    existente = tmp_path / "evals" / "linhas-de-base" / "base-teste.json"
    existente.parent.mkdir(parents=True)
    existente.write_text("{}", encoding="utf-8")
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_USO
    assert "já existe" in fala.getvalue()
    assert existente.read_text(encoding="utf-8") == "{}"


def test_arvore_suja_bloqueia_e_nada_e_gravado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sem_execucao(monkeypatch)
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, True),
    )

    assert codigo == CODIGO_USO
    assert "alterações" in fala.getvalue()
    assert not (tmp_path / "evals" / "linhas-de-base").exists()


def test_sem_git_bloqueia_a_linha_de_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sem_execucao(monkeypatch)
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(None, None),
    )

    assert codigo == CODIGO_USO
    assert "não foi possível confirmar a árvore do git" in fala.getvalue()


# --- --salvar-linha-de-base: caminho feliz -----------------------------------------------------


def _executor_que_devolve(relatorio: Relatorio) -> Callable[..., Relatorio]:
    def executor(
        casos: Callable[..., Any],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
        **_: Any,
    ) -> Relatorio:
        return relatorio

    return executor


def _relatorio_com_trace_e_chave_ausente() -> Relatorio:
    base = _relatorio()
    casos = tuple(
        caso.model_copy(
            update={
                "rodadas": tuple(
                    rodada.model_copy(update={"caminho_trace": "/Users/alguem/traces/t.jsonl"})
                    for rodada in caso.rodadas
                )
            }
        )
        for caso in base.casos
    )
    return base.model_copy(update={"casos": casos, "modelo": "modelo-de-teste"})


def test_caminho_feliz_grava_linha_sanitizada_com_procedencia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", CHAVE_FALSA)
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_executor_que_devolve(_relatorio_com_trace_e_chave_ausente()),
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    gravado = tmp_path / "evals" / "linhas-de-base" / "base-teste.json"
    texto = gravado.read_text(encoding="utf-8")
    assert codigo == CODIGO_OK
    assert "Linha de base gravada: evals/linhas-de-base/base-teste.json" in fala.getvalue()
    assert CHAVE_FALSA not in texto
    assert CHAVE_FALSA not in fala.getvalue()
    assert "/Users/alguem" not in texto
    assert '"caminho_trace": null' in texto
    assert '"linha_de_base": "base-teste"' in texto
    assert f'"commit": "{COMMIT}"' in texto
    assert '"commit_sujo": false' in texto
    assert f'"versao_conjunto": "{VERSAO_CONJUNTO}"' in texto
    assert '"max_iteracoes": 15' in texto
    assert '"iniciado_em": "' in texto


def test_arvore_suja_com_permitir_grava_e_registra_commit_sujo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha("--permitir-arvore-suja"),
        saida=fala,
        executar=_executor_que_devolve(_relatorio()),
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, True),
    )

    texto = (tmp_path / "evals" / "linhas-de-base" / "base-teste.json").read_text(encoding="utf-8")
    assert codigo == CODIGO_OK
    assert '"commit_sujo": true' in texto


def test_nao_sobrescreve_a_linha_gravada(tmp_path: Path) -> None:
    primeiro = main(
        _argumentos_linha(),
        saida=io.StringIO(),
        executar=_executor_que_devolve(_relatorio()),
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )
    fala = io.StringIO()
    segundo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_explode,
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert primeiro == CODIGO_OK
    assert segundo == CODIGO_USO
    assert "já existe" in fala.getvalue()


def test_execucao_com_erro_de_infra_nao_grava_linha_de_base(tmp_path: Path) -> None:
    relatorio = _relatorio().model_copy(update={"erros_infra": 1})
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_executor_que_devolve(relatorio),
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_ERRO_INFRA
    assert "linha de base não gravada" in fala.getvalue()
    assert not (tmp_path / "evals" / "linhas-de-base" / "base-teste.json").exists()


def test_chave_no_relatorio_impede_a_gravacao_sem_mostrar_a_chave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", CHAVE_FALSA)
    vazado = _relatorio().model_copy(update={"modelo": f"modelo {CHAVE_FALSA}"})
    fala = io.StringIO()

    codigo = main(
        _argumentos_linha(),
        saida=fala,
        executar=_executor_que_devolve(vazado),
        fabrica_modelo=_explode,
        raiz=tmp_path,
        obter_procedencia=_obter(COMMIT, False),
    )

    assert codigo == CODIGO_ERRO_INFRA
    assert CHAVE_FALSA not in fala.getvalue()
    assert not (tmp_path / "evals" / "linhas-de-base" / "base-teste.json").exists()


# --- modo de execução antigo continua igual ----------------------------------------------------


def test_modo_de_execucao_sem_linha_de_base_nao_exige_arvore_limpa(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(
        ["--rodadas", "1", "--saida", str(tmp_path / "saida"), "--trace", str(tmp_path / "t")],
        saida=fala,
        executar=_executor_que_devolve(_relatorio()),
        fabrica_modelo=lambda: _explode(),
        raiz=tmp_path,
        obter_procedencia=_obter(None, None),
    )

    assert codigo == CODIGO_OK
    assert not (tmp_path / "evals").exists()


def test_comparar_relatorio_antigo_do_repositorio_cai_para_id_com_aviso() -> None:
    antigo = RAIZ_DO_REPOSITORIO / "docs/relatorios/dados/avaliacao-20261123-091302-semente42.json"
    fala = io.StringIO()

    codigo = main(["comparar", str(antigo), str(antigo)], saida=fala)

    assert codigo == CODIGO_OK
    assert "sem hash por caso" in fala.getvalue()
