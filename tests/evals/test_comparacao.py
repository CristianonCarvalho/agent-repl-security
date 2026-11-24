"""Testes da comparação entre execuções (T17c). Relatórios sintéticos, montados à mão.

As taxas são frações (0 a 1) como no relatório real. Os desvios também. As contas esperadas estão
escritas nos testes: a margem é `max(piso, fator × desvio_base_pp)` e, nos recortes, é escalada
por `sqrt(N / n)`.
"""

import math
from collections.abc import Sequence
from typing import Any

import pytest

from harness.evals.casos import Categoria
from harness.evals.comparacao import (
    Comparacao,
    comparar,
    formatar_json,
    formatar_texto,
)
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import Classificacao, Relatorio, ResumoCaso, RodadaCaso

HASH_VAZIO: str = "0" * 64


def _rodada(numero: int, aprovada: bool) -> RodadaCaso:
    veredito = Veredito(
        caso_id="x",
        aprovado=aprovada,
        regras=(ResultadoRegra(descricao="regra", passou=aprovada, detalhe="ok"),),
    )
    classificacao = Classificacao.APROVADA if aprovada else Classificacao.REPROVADA
    return RodadaCaso(numero=numero, classificacao=classificacao, veredito=veredito, iteracoes=4)


def _caso(
    caso_id: str,
    taxa: float | None,
    *,
    categoria: Categoria = Categoria.CONSULTA,
    grupo: str = "desenvolvimento",
    rodadas: int = 3,
    hash_caso: str | None = None,
    situacao: str = "ativo",
) -> ResumoCaso:
    """Um caso com `rodadas` rodadas válidas e taxa dada. Hash padrão: `hash-<id>`."""
    if taxa is None:
        return ResumoCaso(
            caso_id=caso_id,
            categoria=categoria,
            situacao="pendente" if situacao == "pendente" else "ativo",
            taxa=None,
            grupo=grupo,  # type: ignore[arg-type]
            hash_caso=hash_caso if hash_caso is not None else f"hash-{caso_id}",
        )
    aprovadas = round(taxa * rodadas)
    tupla = tuple(_rodada(i + 1, aprovada=i < aprovadas) for i in range(rodadas))
    return ResumoCaso(
        caso_id=caso_id,
        categoria=categoria,
        situacao="ativo",
        rodadas=tupla,
        taxa=taxa,
        grupo=grupo,  # type: ignore[arg-type]
        hash_caso=hash_caso if hash_caso is not None else f"hash-{caso_id}",
    )


def _bloco(
    taxas: Sequence[float],
    *,
    prefixo: str = "consulta",
    categoria: Categoria = Categoria.CONSULTA,
    grupo: str = "desenvolvimento",
    rodadas: int = 3,
) -> list[ResumoCaso]:
    return [
        _caso(
            f"{prefixo}-{i + 1:02d}",
            taxa,
            categoria=categoria,
            grupo=grupo,
            rodadas=rodadas,
        )
        for i, taxa in enumerate(taxas)
    ]


def _relatorio(
    casos: Sequence[ResumoCaso],
    *,
    semente: int = 42,
    desvio: float | None = None,
    **campos: Any,
) -> Relatorio:
    return Relatorio(
        semente=semente,
        rodadas_por_caso=3,
        hash_casos=HASH_VAZIO,
        casos=tuple(casos),
        taxas_por_rodada=(None, None, None),
        desvio_entre_rodadas=desvio,
        **campos,
    )


def _dez(taxas_dev: Sequence[float], taxas_res: Sequence[float]) -> list[ResumoCaso]:
    """Seis casos de desenvolvimento (consulta) e quatro reservados (cruzamento)."""
    return [
        *_bloco(taxas_dev),
        *_bloco(
            taxas_res,
            prefixo="cruzamento",
            categoria=Categoria.CRUZAMENTO,
            grupo="reservado",
        ),
    ]


def _cenario(
    taxas_base_dev: Sequence[float],
    taxas_base_res: Sequence[float],
    taxas_novo_dev: Sequence[float],
    taxas_novo_res: Sequence[float],
    *,
    desvio_base: float | None = None,
    desvio_novo: float | None = None,
) -> tuple[Relatorio, Relatorio]:
    base = _relatorio(_dez(taxas_base_dev, taxas_base_res), desvio=desvio_base)
    novo = _relatorio(_dez(taxas_novo_dev, taxas_novo_res), desvio=desvio_novo)
    return base, novo


def _recorte(comparacao: Comparacao, nome: str) -> Any:
    for recorte in (*comparacao.categorias, *comparacao.grupos):
        if recorte.nome == nome:
            return recorte
    raise AssertionError(f"recorte {nome!r} ausente")


# --- veredito e regra de margem ----------------------------------------------------------------


def test_relatorios_identicos_dao_ok_e_dentro_do_ruido() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [1.0] * 4)

    comparacao = comparar(base, novo)

    assert comparacao.veredito == "ok"
    assert comparacao.total is not None
    assert comparacao.total.delta_pp == pytest.approx(0.0)
    assert comparacao.total.veredito == "dentro_do_ruido"
    assert comparacao.casos_comparados == 10


def test_queda_grande_no_total_e_regressao_provavel() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.5] * 6, [0.5] * 4)

    comparacao = comparar(base, novo)

    assert comparacao.veredito == "regressao_provavel"
    assert comparacao.total is not None
    assert comparacao.total.veredito == "regressao_provavel"
    assert comparacao.total.delta_pp == pytest.approx(-50.0)
    assert any("regressão provável" in motivo for motivo in comparacao.motivos)


def test_queda_so_no_grupo_reservado_e_regressao_mesmo_com_total_dentro() -> None:
    # Margem sem desvio: piso de 2 p.p. Total cai 1,6 p.p. (dentro). Reservado cai 4 p.p. e a
    # margem dele é 2 × sqrt(10/4) = 3,16 p.p. (regressão).
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [0.96] * 4)

    comparacao = comparar(base, novo)

    assert comparacao.total is not None
    assert comparacao.total.veredito == "dentro_do_ruido"
    reservado = _recorte(comparacao, "reservado")
    assert reservado.delta_pp == pytest.approx(-4.0)
    assert reservado.margem_pp == pytest.approx(2.0 * math.sqrt(10 / 4))
    assert reservado.veredito == "regressao_provavel"
    assert comparacao.veredito == "regressao_provavel"


def test_melhora_grande_dah_melhorou_e_ok() -> None:
    base, novo = _cenario([0.5] * 6, [0.5] * 4, [1.0] * 6, [1.0] * 4)

    comparacao = comparar(base, novo)

    assert comparacao.total is not None
    assert comparacao.total.veredito == "melhorou"
    assert comparacao.veredito == "ok"
    assert any("subiu" in motivo for motivo in comparacao.motivos)


def test_queda_pequena_dentro_da_margem_fica_no_ruido_e_ok() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.99] * 6, [0.99] * 4)

    comparacao = comparar(base, novo)

    assert comparacao.total is not None
    assert comparacao.total.delta_pp == pytest.approx(-1.0)
    assert comparacao.total.veredito == "dentro_do_ruido"
    assert comparacao.veredito == "ok"


def test_unidades_sao_pontos_percentuais() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.9] * 6, [0.9] * 4)

    comparacao = comparar(base, novo)

    assert comparacao.total is not None
    assert comparacao.total.base_pp == pytest.approx(100.0)
    assert comparacao.total.novo_pp == pytest.approx(90.0)
    assert comparacao.total.delta_pp == pytest.approx(-10.0)


def test_margem_usa_o_desvio_entre_rodadas_da_base() -> None:
    # Queda de 3 p.p. no total. Sem desvio, a margem é o piso (2): regressão.
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.97] * 6, [0.97] * 4)
    sem_desvio = comparar(base, novo)

    # Com desvio de 2 p.p. na base, a margem vira 2 × 2 = 4: a mesma queda fica no ruído.
    base_com, novo_com = _cenario([1.0] * 6, [1.0] * 4, [0.97] * 6, [0.97] * 4, desvio_base=0.02)
    com_desvio = comparar(base_com, novo_com)

    assert sem_desvio.total is not None and com_desvio.total is not None
    assert sem_desvio.total.margem_pp == pytest.approx(2.0)
    assert com_desvio.total.margem_pp == pytest.approx(4.0)
    assert sem_desvio.total.veredito == "regressao_provavel"
    assert com_desvio.total.veredito == "dentro_do_ruido"
    assert com_desvio.veredito == "ok"


def test_desvio_do_novo_nao_muda_a_margem() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.97] * 6, [0.97] * 4, desvio_novo=0.5)

    comparacao = comparar(base, novo)

    assert comparacao.total is not None
    assert comparacao.total.margem_pp == pytest.approx(2.0)


def test_sem_desvio_na_base_usa_so_o_piso_e_avisa() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [1.0] * 4)

    comparacao = comparar(base, novo, piso_pp=2.5)

    assert comparacao.desvio_base_pp is None
    assert comparacao.total is not None
    assert comparacao.total.margem_pp == pytest.approx(2.5)
    assert any("não tem desvio" in aviso for aviso in comparacao.avisos)


def test_recortes_pequenos_tem_margem_escalada_pelo_tamanho() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [1.0] * 4)

    comparacao = comparar(base, novo)

    assert _recorte(comparacao, "consulta").margem_pp == pytest.approx(2.0 * math.sqrt(10 / 6))
    assert _recorte(comparacao, "cruzamento").margem_pp == pytest.approx(2.0 * math.sqrt(10 / 4))
    assert _recorte(comparacao, "reservado").margem_pp == pytest.approx(2.0 * math.sqrt(10 / 4))
    assert _recorte(comparacao, "desenvolvimento").margem_pp == pytest.approx(
        2.0 * math.sqrt(10 / 6)
    )


def test_parametros_fora_da_faixa_levantam_erro() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [1.0] * 4)

    with pytest.raises(ValueError):
        comparar(base, novo, piso_pp=-1.0)
    with pytest.raises(ValueError):
        comparar(base, novo, fator_ruido=math.nan)
    with pytest.raises(ValueError):
        comparar(base, novo, limite_caso=0.0)


# --- comparabilidade ---------------------------------------------------------------------------


def test_sementes_diferentes_sao_incomparaveis() -> None:
    base = _relatorio(_bloco([1.0] * 3), semente=42)
    novo = _relatorio(_bloco([1.0] * 3), semente=7)

    comparacao = comparar(base, novo)

    assert comparacao.veredito == "incomparavel"
    assert comparacao.total is None
    assert "sementes diferentes" in comparacao.motivos[0]


def test_nenhum_caso_em_comum_e_incomparavel() -> None:
    base = _relatorio(_bloco([1.0] * 3, prefixo="consulta"))
    novo = _relatorio(_bloco([1.0] * 3, prefixo="permissao"))

    comparacao = comparar(base, novo)

    assert comparacao.veredito == "incomparavel"
    assert comparacao.total is None
    assert comparacao.so_na_base == ("consulta-01", "consulta-02", "consulta-03")
    assert comparacao.so_no_novo == ("permissao-01", "permissao-02", "permissao-03")


def test_interseção_parcial_por_hash_deixa_alterado_e_ausente_fora_da_media() -> None:
    base = _relatorio(
        [
            _caso("consulta-01", 1.0, hash_caso="ha"),
            _caso("consulta-02", 1.0, hash_caso="hb"),
            _caso("consulta-03", 0.0, hash_caso="hc"),
        ]
    )
    novo = _relatorio(
        [
            _caso("consulta-01", 1.0, hash_caso="ha"),
            _caso("consulta-02", 0.0, hash_caso="hb-mudou"),
            _caso("consulta-04", 0.0, hash_caso="hd"),
        ]
    )

    comparacao = comparar(base, novo)

    assert comparacao.casos_comparados == 1
    assert [caso.caso_id for caso in comparacao.casos] == ["consulta-01"]
    assert comparacao.total is not None
    assert comparacao.total.base_pp == pytest.approx(100.0)
    assert comparacao.total.novo_pp == pytest.approx(100.0)
    assert comparacao.so_na_base == ("consulta-03",)
    assert comparacao.so_no_novo == ("consulta-04",)
    assert comparacao.hash_diferente == ("consulta-02",)


def test_caso_ativo_sem_taxa_do_lado_novo_vai_para_sem_taxa() -> None:
    base = _relatorio([_caso("consulta-01", 1.0), _caso("consulta-02", 1.0)])
    novo = _relatorio([_caso("consulta-01", 1.0), _caso("consulta-02", None)])

    comparacao = comparar(base, novo)

    assert comparacao.sem_taxa == ("consulta-02",)
    assert comparacao.casos_comparados == 1


def test_pendente_nao_entra_em_sem_taxa() -> None:
    pendente_base = _caso("geracao-01", None, categoria=Categoria.GERACAO, situacao="pendente")
    pendente_novo = _caso("geracao-01", None, categoria=Categoria.GERACAO, situacao="pendente")
    base = _relatorio([_caso("consulta-01", 1.0), pendente_base])
    novo = _relatorio([_caso("consulta-01", 1.0), pendente_novo])

    comparacao = comparar(base, novo)

    assert comparacao.sem_taxa == ()


def test_relatorio_sem_hash_casa_por_id_com_aviso() -> None:
    base = _relatorio(
        [_caso("consulta-01", 1.0, hash_caso=None), _caso("consulta-02", 1.0, hash_caso=None)]
    )
    novo = _relatorio(
        [_caso("consulta-01", 0.0, hash_caso=None), _caso("consulta-02", 0.0, hash_caso=None)]
    )
    # Os dois lados sem hash: não dá para garantir que o caso é o mesmo, então casa por id.
    base = base.model_copy(update={"casos": tuple(_sem_hash(c) for c in base.casos)})
    novo = novo.model_copy(update={"casos": tuple(_sem_hash(c) for c in novo.casos)})

    comparacao = comparar(base, novo)

    assert comparacao.casos_comparados == 2
    assert any("sem hash por caso" in aviso for aviso in comparacao.avisos)
    assert comparacao.veredito == "regressao_provavel"


def _sem_hash(caso: ResumoCaso) -> ResumoCaso:
    return caso.model_copy(update={"hash_caso": None})


def test_contexto_diferente_e_listado_sem_bloquear() -> None:
    base = _relatorio(
        _bloco([1.0] * 3),
        modelo="modelo-a",
        hash_prompt="prompt-1",
        commit="a" * 40,
        commit_sujo=False,
    )
    novo = _relatorio(
        _bloco([1.0] * 3),
        modelo="modelo-b",
        hash_prompt="prompt-1",
        commit="b" * 40,
        commit_sujo=True,
    )

    comparacao = comparar(base, novo)
    diferentes = {item.campo for item in comparacao.contexto if item.diferente}

    assert {"modelo", "commit", "árvore do git suja"} <= diferentes
    assert "hash do prompt" not in diferentes
    assert comparacao.veredito != "incomparavel"


def test_versoes_de_conjunto_diferentes_geram_aviso_e_nao_bloqueiam() -> None:
    base = _relatorio(_bloco([1.0] * 3), versao_conjunto="v1")
    novo = _relatorio(_bloco([1.0] * 3), versao_conjunto="v2")

    comparacao = comparar(base, novo)

    assert comparacao.veredito == "ok"
    assert any("versões de conjunto diferentes" in aviso for aviso in comparacao.avisos)


def test_menos_de_tres_rodadas_gera_aviso_de_margem() -> None:
    base = _relatorio(_bloco([1.0] * 3))
    novo = _relatorio(_bloco([1.0] * 3))
    base = base.model_copy(update={"rodadas_por_caso": 2, "taxas_por_rodada": (None, None)})

    comparacao = comparar(base, novo)

    assert any("menos de 3 rodadas" in aviso for aviso in comparacao.avisos)


# --- flags por caso ----------------------------------------------------------------------------


def test_flags_por_caso_respeitam_o_limite_e_as_tres_rodadas() -> None:
    base = _relatorio(
        [
            _caso("consulta-01", 1.0),
            _caso("consulta-02", 1.0),
            _caso("consulta-03", 1.0, rodadas=2),
            _caso("consulta-04", 0.2),
        ]
    )
    novo = _relatorio(
        [
            _caso("consulta-01", 0.6),
            _caso("consulta-02", 0.8),
            _caso("consulta-03", 0.0),
            _caso("consulta-04", 0.9),
        ]
    )

    comparacao = comparar(base, novo)
    flags = {caso.caso_id: caso.flag for caso in comparacao.casos}

    assert flags == {
        "consulta-01": "piorou",
        "consulta-02": "igual",
        "consulta-03": "igual",
        "consulta-04": "melhorou",
    }


# --- recortes sem dados ------------------------------------------------------------------------


def test_categoria_sem_caso_comparavel_fica_sem_dados() -> None:
    base = _relatorio(
        [
            _caso("consulta-01", 1.0),
            _caso("permissao-01", 1.0, categoria=Categoria.PERMISSAO, hash_caso="x1"),
        ]
    )
    novo = _relatorio(
        [
            _caso("consulta-01", 1.0),
            _caso("permissao-01", 1.0, categoria=Categoria.PERMISSAO, hash_caso="x2"),
        ]
    )

    comparacao = comparar(base, novo)

    permissao = _recorte(comparacao, "permissao")
    assert permissao.veredito == "sem_dados"
    assert permissao.casos == 0
    assert comparacao.hash_diferente == ("permissao-01",)


# --- JSON e texto ------------------------------------------------------------------------------


def test_comparacao_serializa_em_json_sem_perda() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.5] * 6, [0.5] * 4, desvio_base=0.01)
    comparacao = comparar(base, novo)

    texto = formatar_json(comparacao)

    assert Comparacao.model_validate_json(texto) == comparacao


def test_texto_traz_veredito_em_portugues_e_sem_caminho_de_trace() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [0.5] * 6, [0.5] * 4)
    comparacao = comparar(base, novo)

    texto = formatar_texto(comparacao)

    assert "Veredito: REGRESSÃO PROVÁVEL" in texto
    assert "O que mudou entre as execuções:" in texto
    assert "p.p." in texto
    assert "caminho_trace" not in texto
    assert "traces/" not in texto


def test_custo_e_iteracoes_aparecem_quando_existem_nos_dois() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [1.0] * 4)
    base = base.model_copy(update={"custo_total": 0.07, "tokens_entrada_total": 100})
    novo = novo.model_copy(update={"custo_total": 0.05, "tokens_entrada_total": 150})

    comparacao = comparar(base, novo)
    valores = {v.nome: v for v in comparacao.valores}

    assert valores["custo total (US$)"].delta == pytest.approx(-0.02)
    assert valores["tokens de entrada"].delta == pytest.approx(50.0)
    assert valores["iterações médias por rodada"].base == pytest.approx(4.0)


def test_custo_ausente_em_um_lado_nao_inventa_delta() -> None:
    base, novo = _cenario([1.0] * 6, [1.0] * 4, [1.0] * 6, [1.0] * 4)
    base = base.model_copy(update={"custo_total": 0.07})

    comparacao = comparar(base, novo)
    custo = next(v for v in comparacao.valores if v.nome == "custo total (US$)")

    assert custo.delta is None
    assert "não informado" in formatar_texto(comparacao)


def test_comparacao_com_categoria_calculo_e_relatorio_antigo_sem_calculo() -> None:
    base = _relatorio(
        [_caso("consulta-01", 0.8), _caso("calculo-01", 0.5, categoria=Categoria.CALCULO)]
    )
    novo = _relatorio(
        [_caso("consulta-01", 0.8), _caso("calculo-01", 1.0, categoria=Categoria.CALCULO)]
    )
    comparacao = comparar(base, novo)
    calculo = _recorte(comparacao, "calculo")
    assert calculo.casos == 1
    assert calculo.delta_pp == pytest.approx(50.0)

    antigo_sem_calculo = _relatorio([_caso("consulta-01", 0.8)])
    comp_mista = comparar(antigo_sem_calculo, novo)
    assert _recorte(comp_mista, "calculo").veredito == "sem_dados"
    assert _recorte(comp_mista, "consulta").delta_pp == pytest.approx(0.0)
