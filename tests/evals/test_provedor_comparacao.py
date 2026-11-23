"""Avisos de provedor na comparação e campo `provedor_solicitado` no relatório (sem rede).

Os avisos não mudam o veredito: os testes conferem o texto e também que o veredito é o mesmo.
"""

import json

from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.comparacao import comparar, formatar_texto
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    RodadaCaso,
    agregar,
    relatorio_de_json,
    relatorio_para_json,
)


def _caso() -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )


def _relatorio(
    *,
    solicitado: str | None = None,
    efetivos: tuple[str, ...] = (),
    aprovadas: int = 3,
) -> Relatorio:
    veredito = Veredito(
        caso_id="consulta-01",
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    rodadas = [
        RodadaCaso(numero=n, classificacao=Classificacao.APROVADA, veredito=veredito)
        for n in range(1, aprovadas + 1)
    ]
    base = agregar(
        [_caso()],
        {"consulta-01": rodadas},
        semente=42,
        rodadas_por_caso=3,
        provedores=efetivos,
    )
    return base.model_copy(update={"provedor_solicitado": solicitado})


def _avisos(base: Relatorio, novo: Relatorio) -> list[str]:
    return list(comparar(base, novo).avisos)


def test_relatorio_grava_e_le_o_provedor_solicitado() -> None:
    relatorio = _relatorio(solicitado="Wafer", efetivos=("Wafer",))

    lido = relatorio_de_json(relatorio_para_json(relatorio))

    assert lido.provedor_solicitado == "Wafer"


def test_relatorio_antigo_sem_provedor_solicitado_carrega_como_none() -> None:
    dados = json.loads(relatorio_para_json(_relatorio()))
    dados.pop("provedor_solicitado", None)

    lido = relatorio_de_json(json.dumps(dados))

    assert lido.provedor_solicitado is None


def test_provedores_iguais_nao_geram_aviso() -> None:
    base = _relatorio(solicitado="Wafer", efetivos=("Wafer",))
    novo = _relatorio(solicitado="Wafer", efetivos=("Wafer",))

    assert _avisos(base, novo) == []


def test_provedores_solicitados_diferentes_geram_aviso_com_os_dois_nomes() -> None:
    base = _relatorio(solicitado="Wafer", efetivos=("Wafer",))
    novo = _relatorio(solicitado="Novita", efetivos=("Novita",))

    avisos = _avisos(base, novo)

    assert len(avisos) == 1
    assert "diferentes" in avisos[0]
    assert "Wafer" in avisos[0] and "Novita" in avisos[0]


def test_so_a_base_declara_o_provedor_gera_aviso() -> None:
    base = _relatorio(solicitado="Wafer", efetivos=("Wafer",))
    novo = _relatorio(efetivos=("Wafer",))

    avisos = _avisos(base, novo)

    assert any("o novo não declara" in aviso and "Wafer" in aviso for aviso in avisos)


def test_so_o_novo_declara_o_provedor_gera_aviso() -> None:
    base = _relatorio(efetivos=("Wafer",))
    novo = _relatorio(solicitado="Wafer", efetivos=("Wafer",))

    avisos = _avisos(base, novo)

    assert any("a linha de base não declara" in aviso for aviso in avisos)


def test_provedor_efetivo_diferente_do_solicitado_no_novo_gera_aviso() -> None:
    base = _relatorio(solicitado="Wafer", efetivos=("Wafer",))
    novo = _relatorio(solicitado="Wafer", efetivos=("Tenstorrent",))

    avisos = _avisos(base, novo)

    assert len(avisos) == 1
    assert avisos[0].startswith("novo:")
    assert "Tenstorrent" in avisos[0]


def test_provedor_efetivo_diferente_na_base_gera_aviso_com_rotulo_base() -> None:
    base = _relatorio(solicitado="Wafer", efetivos=("Tenstorrent",))
    novo = _relatorio(solicitado="Wafer", efetivos=("Wafer",))

    avisos = _avisos(base, novo)

    assert len(avisos) == 1
    assert avisos[0].startswith("base:")


def test_diferenca_so_de_caixa_no_nome_nao_gera_aviso() -> None:
    base = _relatorio(solicitado="Wafer", efetivos=("wafer",))
    novo = _relatorio(solicitado="WAFER", efetivos=("Wafer",))

    assert _avisos(base, novo) == []


def test_avisos_de_provedor_nao_mudam_o_veredito() -> None:
    limpa = comparar(
        _relatorio(solicitado="Wafer", efetivos=("Wafer",)),
        _relatorio(solicitado="Wafer", efetivos=("Wafer",)),
    )
    com_avisos = comparar(
        _relatorio(solicitado="Wafer", efetivos=("Wafer",)),
        _relatorio(solicitado="Novita", efetivos=("Tenstorrent",)),
    )

    assert limpa.veredito == com_avisos.veredito == "ok"
    assert com_avisos.avisos
    assert "Avisos:" in formatar_texto(com_avisos)
