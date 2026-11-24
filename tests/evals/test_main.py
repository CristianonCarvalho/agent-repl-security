"""Testes da linha de comando da avaliação (T16): argumentos, códigos de saída e segredos.

O executor real não roda aqui: `main` recebe um `executar` falso. Os testes que leem o ambiente
trocam de diretório para uma pasta temporária, então nunca leem o `.env` do projeto.
"""

import io
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import (
    CODIGO_ERRO_INFRA,
    CODIGO_OK,
    CODIGO_TETO,
    CODIGO_USO,
    _parser,
    main,
)
from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.dados import gerar_dados
from harness.evals.relatorio import Classificacao, Relatorio, RodadaCaso, agregar
from harness.llm.base import LLMFalso

CHAVE_FALSA: str = "chave-falsa-555aaa"


def _caso() -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )


def _relatorio(*, com_erro: bool = False, teto: str | None = None) -> Relatorio:
    """Relatório pequeno: uma rodada aprovada, e uma erro_infra se `com_erro`."""
    veredito = Veredito(
        caso_id="consulta-01",
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    rodadas = [RodadaCaso(numero=1, classificacao=Classificacao.APROVADA, veredito=veredito)]
    if com_erro:
        rodadas.append(
            RodadaCaso(numero=2, classificacao=Classificacao.ERRO_INFRA, detalhe="erro_llm")
        )
    return agregar(
        [_caso()],
        {"consulta-01": rodadas},
        semente=42,
        rodadas_por_caso=len(rodadas),
        teto_motivo=teto,
    )


def _executor_falso(
    relatorio: Relatorio, chamadas: list[dict[str, Any]] | None = None
) -> Callable[..., Relatorio]:
    """Executor que não roda nada: devolve o relatório pronto e avisa o progresso de uma rodada."""

    def falso(
        casos: Callable[..., Sequence[Caso]],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
    ) -> Relatorio:
        if chamadas is not None:
            chamadas.append({"config": config, "casos": casos})
        if aoprogresso is not None:
            aoprogresso("consulta-01", 1, Classificacao.APROVADA)
        return relatorio

    return falso


def _fabrica() -> LLMFalso:
    return LLMFalso([])


def _argumentos(saida: Path, *extra: str) -> list[str]:
    return ["--rodadas", "1", "--saida", str(saida), "--trace", str(saida / "trace"), *extra]


def test_codigo_zero_quando_tudo_passa_e_grava_o_relatorio(tmp_path: Path) -> None:
    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica,
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_OK
    assert "consulta-01 rodada 1/1: aprovada" in texto
    assert "Taxa total de sucesso: 100,0%" in texto
    assert "Teto de gasto: dentro do limite" in texto
    arquivos = list((tmp_path / "saida").glob("avaliacao-*.json"))
    assert len(arquivos) == 1
    assert str(arquivos[0]) in texto


def test_codigo_um_quando_houve_erro_de_infra(tmp_path: Path) -> None:
    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(_relatorio(com_erro=True)),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_ERRO_INFRA
    assert "Erros de infraestrutura: 1" in fala.getvalue()


def test_codigo_tres_quando_o_teto_estourou_e_tem_precedencia(tmp_path: Path) -> None:
    fala = io.StringIO()
    relatorio = _relatorio(com_erro=True, teto="custo acumulado passou do teto")
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(relatorio),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_TETO
    assert "Teto de gasto: estourado (custo acumulado passou do teto)" in fala.getvalue()


def test_codigo_tres_sem_erro_de_infra(tmp_path: Path) -> None:
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio(teto="tokens acumulados")),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_TETO


def test_categoria_inexistente_sai_com_dois_sem_executar(tmp_path: Path) -> None:
    fala = io.StringIO()
    chamadas: list[dict[str, Any]] = []

    codigo = main(
        _argumentos(tmp_path / "saida", "--categoria", "inventada"),
        saida=fala,
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_USO
    assert "categoria desconhecida" in fala.getvalue()
    assert "calculo" in fala.getvalue()
    assert chamadas == []


def test_categoria_calculo_aceita_na_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fala = io.StringIO()
    chamadas: list[dict[str, Any]] = []

    codigo = main(
        _argumentos(tmp_path / "saida", "--categoria", "calculo"),
        saida=fala,
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_modelo_proibido,
    )

    assert codigo == 0
    assert chamadas == []


def _modelo_proibido() -> LLMFalso:
    raise AssertionError("o modelo não pode ser chamado sem caso ativo na seleção")


def test_categoria_sem_caso_ativo_sai_com_zero_sem_chamar_o_modelo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fala = io.StringIO()
    chamadas: list[dict[str, Any]] = []

    codigo = main(
        _argumentos(tmp_path / "saida", "--categoria", "injecao"),
        saida=fala,
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_modelo_proibido,
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_OK
    assert chamadas == []
    assert "Não há caso ativo na seleção" in texto
    assert "Nenhum modelo foi chamado" in texto
    assert "Pendentes selecionados: 5" in texto
    for numero in range(1, 6):
        assert f"injecao-0{numero}: bloqueios:" in texto
    assert "injecao-05: bloqueios: politica_trifecta, medicao_injecao, fixture_mcp" in texto
    assert not (tmp_path / "saida").exists()


def test_categoria_geracao_sem_ativo_lista_os_pendentes_com_o_bloqueio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida", "--categoria", "geracao"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_modelo_proibido,
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_OK
    assert "geracao-01: bloqueios: entrega_publicar, corretor_de_arquivos" in texto
    assert "Pendentes selecionados: 5" in texto


def test_grupo_combinado_sem_ativo_respeita_os_dois_filtros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida", "--categoria", "injecao", "--grupo", "reservado"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_modelo_proibido,
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_OK
    assert "Pendentes selecionados: 2" in texto
    assert "injecao-04: bloqueios:" in texto
    assert "injecao-05: bloqueios:" in texto
    assert "injecao-01" not in texto


def test_categoria_desconhecida_continua_sendo_erro_de_uso(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida", "--categoria", "outra"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_USO
    assert "categoria desconhecida" in fala.getvalue()


def test_configuracao_invalida_sai_com_dois(tmp_path: Path) -> None:
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida", "--teto-usd", "-1"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_USO
    assert "teto_usd" in fala.getvalue()


def test_rodadas_zero_e_erro_de_uso(tmp_path: Path) -> None:
    codigo = main(
        ["--rodadas", "0", "--saida", str(tmp_path)],
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_USO


def test_chave_ausente_sai_com_dois_sem_imprimir_nada_secreto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_MODELO", "modelo-de-teste")
    fala = io.StringIO()
    chamadas: list[dict[str, Any]] = []

    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(_relatorio(), chamadas),
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_USO
    assert "OPENROUTER_API_KEY não definida" in texto
    assert chamadas == []
    assert not (tmp_path / "saida").exists()


def test_modelo_ausente_sai_com_dois(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENROUTER_MODELO", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", CHAVE_FALSA)
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
    )

    assert codigo == CODIGO_USO
    assert "OPENROUTER_MODELO" in fala.getvalue()
    assert CHAVE_FALSA not in fala.getvalue()


def test_nenhuma_saida_nem_relatorio_contem_a_chave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_MODELO", "modelo-de-teste")
    monkeypatch.setenv("OPENROUTER_API_KEY", CHAVE_FALSA)
    fala = io.StringIO()

    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
    )

    assert codigo == CODIGO_OK
    assert CHAVE_FALSA not in fala.getvalue()
    for arquivo in (tmp_path / "saida").glob("*.json"):
        assert CHAVE_FALSA not in arquivo.read_text(encoding="utf-8")


def test_falha_ao_gravar_relatorio_sai_com_um(tmp_path: Path) -> None:
    bloqueio = tmp_path / "bloqueio"
    bloqueio.write_text("arquivo no lugar da pasta", encoding="utf-8")
    fala = io.StringIO()

    codigo = main(
        _argumentos(bloqueio / "sub"),
        saida=fala,
        executar=_executor_falso(_relatorio()),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_ERRO_INFRA
    assert "não foi possível gravar o relatório" in fala.getvalue()
    assert "não gravado" in fala.getvalue()


def test_erro_inesperado_do_executor_sai_com_um_sem_detalhe(tmp_path: Path) -> None:
    fala = io.StringIO()

    def quebrado(*args: Any, **kwargs: Any) -> Relatorio:
        raise RuntimeError("caminho /segredo/do/host")

    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=quebrado,
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_ERRO_INFRA
    assert "RuntimeError" in fala.getvalue()
    assert "segredo" not in fala.getvalue()


def test_parser_tem_os_padroes_da_spec() -> None:
    args = _parser().parse_args([])

    assert args.rodadas == 3
    assert args.semente == 42
    assert args.teto_usd == 2.0
    assert args.teto_tokens == 2_000_000
    assert args.trace == Path("traces/evals")
    assert args.saida == Path("avaliacoes")
    assert args.categoria == []
    assert args.max_iteracoes is None


def _casos_selecionados(tmp_path: Path, *extra: str) -> list[Caso]:
    """Roda a CLI com o executor falso e devolve a seleção que ele receberia."""
    chamadas: list[dict[str, Any]] = []
    main(
        _argumentos(tmp_path / "saida", *extra),
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_fabrica,
    )
    assert len(chamadas) == 1
    fatos = gerar_dados(tmp_path / "dados", 42)
    return list(chamadas[0]["casos"](fatos))


def _ids_selecionados(tmp_path: Path, *extra: str) -> list[str]:
    return [caso.id for caso in _casos_selecionados(tmp_path, *extra)]


def test_padrao_seleciona_os_35_casos_e_so_os_20_ativos_rodam(tmp_path: Path) -> None:
    casos = _casos_selecionados(tmp_path)
    ids = [caso.id for caso in casos]

    assert len(ids) == 35
    assert ids[0] == "consulta-01"
    assert ids[19] == "permissao-05"
    assert [caso.id for caso in casos if caso.situacao.ativo] == ids[:20]
    assert ids[-1] == "injecao-05"


def test_grupo_reservado_seleciona_os_sete_ativos_reservados_e_seis_pendentes(
    tmp_path: Path,
) -> None:
    casos = _casos_selecionados(tmp_path, "--grupo", "reservado")

    assert [c.id for c in casos if c.situacao.ativo] == [
        "consulta-04",
        "consulta-05",
        "cruzamento-04",
        "cruzamento-05",
        "conformidade-04",
        "permissao-03",
        "permissao-04",
    ]
    assert [c.id for c in casos if c.situacao.pendente] == [
        "geracao-04",
        "geracao-05",
        "alteracao-03",
        "alteracao-05",
        "injecao-04",
        "injecao-05",
    ]


def test_grupo_e_categoria_se_combinam(tmp_path: Path) -> None:
    ids = _ids_selecionados(tmp_path, "--categoria", "permissao", "--grupo", "desenvolvimento")

    assert ids == ["permissao-01", "permissao-02", "permissao-05"]


def test_grupo_todos_e_o_padrao(tmp_path: Path) -> None:
    assert _ids_selecionados(tmp_path, "--grupo", "todos") == _ids_selecionados(tmp_path / "b")


def test_grupo_desconhecido_sai_com_dois_sem_executar(tmp_path: Path) -> None:
    chamadas: list[dict[str, Any]] = []

    codigo = main(
        _argumentos(tmp_path / "saida", "--grupo", "outro"),
        saida=io.StringIO(),
        executar=_executor_falso(_relatorio(), chamadas),
        fabrica_modelo=_fabrica,
    )

    assert codigo == CODIGO_USO
    assert chamadas == []


def test_resumo_mostra_a_contagem_e_os_ids_dos_pendentes(tmp_path: Path) -> None:
    pendente = Caso(
        id="geracao-01",
        categoria=Categoria.GERACAO,
        perfil="Direção",
        pedido="Monte o relatório. Cite a fonte.",
        situacao=Situacao(
            ativo=False,
            motivo_pendente="precisa de entrega",
            bloqueios=("entrega_publicar",),
        ),
        gabarito=[RecusaAcesso()],
        comportamento_esperado="A resposta grava o arquivo.",
    )
    relatorio = agregar(
        [_caso(), pendente],
        {
            "consulta-01": [
                RodadaCaso(
                    numero=1,
                    classificacao=Classificacao.APROVADA,
                    veredito=Veredito(
                        caso_id="consulta-01",
                        aprovado=True,
                        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
                    ),
                )
            ]
        },
        semente=42,
        rodadas_por_caso=1,
    )
    fala = io.StringIO()

    main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(relatorio),
        fabrica_modelo=_fabrica,
    )

    assert "Pendentes: 1 (geracao-01)" in fala.getvalue()


def test_resumo_imprime_a_taxa_de_cada_grupo(tmp_path: Path) -> None:
    casos = [
        Caso(
            id="consulta-01",
            categoria=Categoria.CONSULTA,
            perfil="Vendas",
            pedido="Qual é o valor? Cite a fonte.",
            situacao=Situacao(ativo=True),
            gabarito=[RecusaAcesso()],
        ),
        Caso(
            id="consulta-04",
            categoria=Categoria.CONSULTA,
            perfil="Vendas",
            pedido="Qual é o valor? Cite a fonte.",
            situacao=Situacao(ativo=True),
            gabarito=[RecusaAcesso()],
            grupo="reservado",
        ),
    ]
    aprovada = Veredito(
        caso_id="consulta-01",
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    reprovada = Veredito(
        caso_id="consulta-04",
        aprovado=False,
        regras=(ResultadoRegra(descricao="regra", passou=False, detalhe="não"),),
    )
    relatorio = agregar(
        casos,
        {
            "consulta-01": [
                RodadaCaso(numero=1, classificacao=Classificacao.APROVADA, veredito=aprovada)
            ],
            "consulta-04": [
                RodadaCaso(numero=1, classificacao=Classificacao.REPROVADA, veredito=reprovada)
            ],
        },
        semente=42,
        rodadas_por_caso=1,
    )
    fala = io.StringIO()

    main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=_executor_falso(relatorio),
        fabrica_modelo=_fabrica,
    )

    texto = fala.getvalue()
    assert "Por grupo:" in texto
    assert "  desenvolvimento: 100,0% (1 caso(s) com taxa)" in texto
    assert "  reservado: 0,0% (1 caso(s) com taxa)" in texto
