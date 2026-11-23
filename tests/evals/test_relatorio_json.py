"""Testes do relatório com as métricas de contexto da variante JSON (T18a).

As contas esperadas estão escritas nos testes (à mão). Relatório antigo, sem os campos novos,
precisa continuar carregando.
"""

import json
from typing import Any

import pytest

from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    RodadaCaso,
    agregar,
    relatorio_de_json,
    relatorio_para_json,
)

APROVADA = Classificacao.APROVADA
REPROVADA = Classificacao.REPROVADA
ERRO = Classificacao.ERRO_INFRA


def _caso(caso_id: str) -> Caso:
    return Caso(
        id=caso_id,
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
        comportamento_esperado=None,
    )


def _veredito(caso_id: str, aprovado: bool) -> Veredito:
    regra = ResultadoRegra(descricao="regra", passou=aprovado, detalhe="ok" if aprovado else "não")
    return Veredito(caso_id=caso_id, aprovado=aprovado, regras=(regra,))


def _rodada(
    numero: int,
    classificacao: Classificacao,
    *,
    turnos: tuple[int, ...] = (),
    custo: float | None = 0.01,
    caracteres: int | None = None,
    caso_id: str = "consulta-01",
) -> RodadaCaso:
    pico = max(turnos) if turnos else None
    veredito = None
    if classificacao is APROVADA:
        veredito = _veredito(caso_id, True)
    elif classificacao is REPROVADA:
        veredito = _veredito(caso_id, False)
    detalhe = "erro_llm: X" if classificacao is ERRO else None
    return RodadaCaso(
        numero=numero,
        classificacao=classificacao,
        veredito=veredito,
        custo=custo,
        tokens_entrada=sum(turnos),
        tokens_saida=5,
        tokens_entrada_por_turno=turnos,
        pico_contexto=pico,
        caracteres_observacao=caracteres,
        detalhe=detalhe,
    )


def _agregar(rodadas: dict[str, list[RodadaCaso]], variante: str = "json") -> Relatorio:
    casos = [_caso(cid) for cid in rodadas]
    return agregar(
        casos,
        rodadas,
        semente=42,
        rodadas_por_caso=max(len(r) for r in rodadas.values()),
        variante=variante,  # type: ignore[arg-type]
        ferramentas_json="basicas+agregacao",
        hash_ferramentas="a" * 64,
    )


def test_pico_e_caracteres_medios_e_custo_por_acerto_conferidos_a_mao() -> None:
    # Três rodadas válidas: 2 aprovadas, 1 reprovada. Custo 0,01 cada: total 0,03.
    rodadas = {
        "consulta-01": [
            _rodada(1, APROVADA, turnos=(100, 200), caracteres=50),
            _rodada(2, APROVADA, turnos=(100, 300), caracteres=70),
            _rodada(3, REPROVADA, turnos=(100, 400), caracteres=30),
        ]
    }
    relatorio = _agregar(rodadas)
    assert relatorio.pico_contexto_medio == pytest.approx((200 + 300 + 400) / 3)
    assert relatorio.caracteres_observacao_medio == pytest.approx((50 + 70 + 30) / 3)
    assert relatorio.custo_total == pytest.approx(0.03)
    assert relatorio.custo_por_acerto == pytest.approx(0.03 / 2)


def test_custo_por_acerto_e_none_sem_aprovada_ou_sem_custo() -> None:
    sem_aprovada = _agregar({"consulta-01": [_rodada(1, REPROVADA, turnos=(10,))]})
    assert sem_aprovada.custo_por_acerto is None
    sem_custo = _agregar({"consulta-01": [_rodada(1, APROVADA, turnos=(10,), custo=None)]})
    assert sem_custo.custo_por_acerto is None


def test_erro_de_infra_fica_fora_das_medias_de_contexto() -> None:
    rodadas = {
        "consulta-01": [
            _rodada(1, APROVADA, turnos=(100,), caracteres=10),
            _rodada(2, ERRO, turnos=(9999,), caracteres=9999),
        ]
    }
    relatorio = _agregar(rodadas)
    assert relatorio.pico_contexto_medio == 100.0
    assert relatorio.caracteres_observacao_medio == 10.0


def test_curva_de_contexto_so_tem_turnos_com_pelo_menos_dez_rodadas() -> None:
    dez = [_rodada(n, APROVADA, turnos=(100, 200)) for n in range(1, 11)]
    nove = [_rodada(n, REPROVADA, turnos=(300, 400, 500)) for n in range(1, 10)]
    relatorio = _agregar({"consulta-01": dez, "consulta-02": nove})
    # Turno 1: 19 rodadas válidas, média de (10×100 + 9×300) / 19 = 3700 / 19 ≈ 194,7 → 195.
    # Turno 2: média de (10×200 + 9×400) / 19 = 5600 / 19 ≈ 294,7 → 295.
    # Turno 3: só as 9 rodadas de consulta-02, abaixo do mínimo de 10: fica de fora.
    assert relatorio.curva_de_contexto == ((1, 195), (2, 295))


def test_curva_arredonda_a_media_e_usa_turno_a_partir_de_um() -> None:
    rodadas = [_rodada(n, APROVADA, turnos=(7, 11)) for n in range(1, 11)]
    relatorio = _agregar({"consulta-01": rodadas})
    assert relatorio.curva_de_contexto == ((1, 7), (2, 11))


def test_metricas_ausentes_ficam_vazias_e_o_relatorio_valida() -> None:
    relatorio = _agregar({"consulta-01": [_rodada(1, APROVADA, custo=None)]})
    assert relatorio.curva_de_contexto == ()
    assert relatorio.pico_contexto_medio is None
    assert relatorio.caracteres_observacao_medio is None


def test_campos_novos_vao_e_voltam_em_json() -> None:
    relatorio = _agregar({"consulta-01": [_rodada(1, APROVADA, turnos=(100, 200), caracteres=5)]})
    texto = relatorio_para_json(relatorio)
    recarregado = relatorio_de_json(texto)
    assert recarregado == relatorio
    assert recarregado.variante == "json"
    assert recarregado.ferramentas_json == "basicas+agregacao"
    assert recarregado.hash_ferramentas == "a" * 64


@pytest.mark.parametrize("variante", ["repl", "json", "json-agregacao"])
def test_variantes_validas_passam_na_agregacao(variante: str) -> None:
    relatorio = _agregar({"consulta-01": [_rodada(1, APROVADA)]}, variante=variante)
    assert relatorio.variante == variante


def test_variante_desconhecida_e_recusada() -> None:
    with pytest.raises(ValueError):
        _agregar({"consulta-01": [_rodada(1, APROVADA)]}, variante="xpto")


def test_relatorio_antigo_sem_os_campos_novos_carrega_com_padroes() -> None:
    relatorio = _agregar({"consulta-01": [_rodada(1, APROVADA)]}, variante="repl")
    dados: dict[str, Any] = json.loads(relatorio_para_json(relatorio))
    for campo in (
        "ferramentas_json",
        "hash_ferramentas",
        "curva_de_contexto",
        "pico_contexto_medio",
        "caracteres_observacao_medio",
        "custo_por_acerto",
        "variante",
    ):
        dados.pop(campo)
    for rodada in dados["casos"][0]["rodadas"]:
        for campo in ("tokens_entrada_por_turno", "pico_contexto", "caracteres_observacao"):
            rodada.pop(campo)
    antigo = relatorio_de_json(json.dumps(dados))
    assert antigo.variante == "repl"
    assert antigo.curva_de_contexto == ()
    assert antigo.hash_ferramentas is None
    assert antigo.casos[0].rodadas[0].pico_contexto is None
    assert antigo.casos[0].rodadas[0].tokens_entrada_por_turno == ()
