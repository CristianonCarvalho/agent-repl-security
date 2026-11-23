"""Testes da comparação entre variantes (T18a): variante diferente não é incomparável.

As variantes entram no contexto ("O que mudou"), as métricas de contexto entram na tabela, e a
falta de uma métrica num dos lados vira aviso. Relatórios são montados com `agregar`.
"""

from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao
from harness.evals.comparacao import Comparacao, DiferencaValor, comparar, formatar_texto
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import Classificacao, Relatorio, RodadaCaso, agregar


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


def _rodada(numero: int, aprovada: bool, pico: int | None, caracteres: int | None) -> RodadaCaso:
    regra = ResultadoRegra(descricao="regra", passou=aprovada, detalhe="ok")
    veredito = Veredito(caso_id="consulta-01", aprovado=aprovada, regras=(regra,))
    return RodadaCaso(
        numero=numero,
        classificacao=Classificacao.APROVADA if aprovada else Classificacao.REPROVADA,
        veredito=veredito,
        custo=0.02,
        tokens_entrada=300,
        tokens_saida=20,
        tokens_entrada_por_turno=(100, pico) if pico is not None else (),
        pico_contexto=pico,
        caracteres_observacao=caracteres,
    )


def _relatorio(variante: str, *, com_metricas: bool, ferramentas: str | None) -> Relatorio:
    casos = [_caso("consulta-01")]
    rodadas = {
        "consulta-01": [
            _rodada(1, True, 200 if com_metricas else None, 50 if com_metricas else None),
            _rodada(2, True, 220 if com_metricas else None, 60 if com_metricas else None),
            _rodada(3, False, 210 if com_metricas else None, 55 if com_metricas else None),
        ]
    }
    return agregar(
        casos,
        rodadas,
        semente=42,
        rodadas_por_caso=3,
        variante=variante,  # type: ignore[arg-type]
        ferramentas_json=ferramentas,
        hash_ferramentas="b" * 64 if ferramentas else None,
    )


def _valor(comparacao: Comparacao, nome: str) -> DiferencaValor:
    for valor in comparacao.valores:
        if valor.nome == nome:
            return valor
    raise AssertionError(f"valor {nome!r} ausente")


def test_variante_diferente_nao_torna_a_comparacao_incomparavel() -> None:
    base = _relatorio("repl", com_metricas=True, ferramentas=None)
    novo = _relatorio("json", com_metricas=True, ferramentas="basicas")
    comparacao = comparar(base, novo)
    assert comparacao.veredito != "incomparavel"
    assert comparacao.casos_comparados == 1


def test_variante_e_ferramentas_entram_em_o_que_mudou() -> None:
    base = _relatorio("repl", com_metricas=True, ferramentas=None)
    novo = _relatorio("json-agregacao", com_metricas=True, ferramentas="basicas+agregacao")
    campos = {c.campo: c for c in comparar(base, novo).contexto}
    assert campos["variante"].diferente is True
    assert (campos["variante"].base, campos["variante"].novo) == ("repl", "json-agregacao")
    assert campos["conjunto de ferramentas JSON"].diferente is True
    assert campos["hash das ferramentas"].diferente is True


def test_metricas_de_contexto_aparecem_com_os_valores_dos_dois_lados() -> None:
    base = _relatorio("repl", com_metricas=True, ferramentas=None)
    novo = _relatorio("json", com_metricas=True, ferramentas="basicas")
    comparacao = comparar(base, novo)
    pico = _valor(comparacao, "pico de contexto médio (tokens)")
    assert pico.base == base.pico_contexto_medio
    assert pico.novo == novo.pico_contexto_medio
    assert _valor(comparacao, "caracteres de observação médios") is not None
    assert _valor(comparacao, "custo por acerto (US$)") is not None


def test_metrica_so_de_um_lado_vira_aviso_e_nao_quebra() -> None:
    base = _relatorio("repl", com_metricas=False, ferramentas=None)
    novo = _relatorio("json", com_metricas=True, ferramentas="basicas")
    comparacao = comparar(base, novo)
    avisos = " ".join(comparacao.avisos)
    assert "pico de contexto médio (tokens)" in avisos
    assert "a linha de base" in avisos
    assert comparacao.veredito != "incomparavel"


def test_metricas_ausentes_nos_dois_lados_nao_aparecem_nem_avisam() -> None:
    base = _relatorio("repl", com_metricas=False, ferramentas=None)
    novo = _relatorio("repl", com_metricas=False, ferramentas=None)
    comparacao = comparar(base, novo)
    assert not [a for a in comparacao.avisos if "contexto" in a]
    texto = formatar_texto(comparacao)
    assert "pico de contexto médio" not in texto


def test_relatorio_de_texto_mostra_variante_e_metricas_no_que_mudou() -> None:
    base = _relatorio("repl", com_metricas=True, ferramentas=None)
    novo = _relatorio("json", com_metricas=True, ferramentas="basicas")
    texto = formatar_texto(comparar(base, novo))
    assert "variante: repl -> json" in texto
    assert "pico de contexto médio (tokens): base" in texto
    assert "custo por acerto (US$)" in texto
