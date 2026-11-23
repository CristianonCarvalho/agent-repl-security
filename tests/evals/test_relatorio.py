"""Testes do relatório (T16): agregação conferida à mão, JSON, coerência e gravação em arquivo.

As contas esperadas estão escritas nos testes. Com a semente fixa, os números não mudam.
"""

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from harness.evals.casos import Caso, Categoria, RecusaAcesso, Situacao, casos_para_json
from harness.evals.correcao import ResultadoRegra, Veredito
from harness.evals.relatorio import (
    Classificacao,
    ErroDeRelatorio,
    Relatorio,
    RodadaCaso,
    agregar,
    escrever_relatorio,
    relatorio_de_json,
    relatorio_para_json,
)

APROVADA = Classificacao.APROVADA
REPROVADA = Classificacao.REPROVADA
ERRO = Classificacao.ERRO_INFRA
NAO_EXECUTADA = Classificacao.NAO_EXECUTADA


def _caso(
    caso_id: str, categoria: Categoria, *, ativo: bool = True, perfil: str = "Vendas"
) -> Caso:
    situacao = (
        Situacao(ativo=True)
        if ativo
        else Situacao(
            ativo=False,
            motivo_pendente="aguardando a alçada da diretoria",
            bloqueios=("aprovacao_humana",),
        )
    )
    return Caso(
        id=caso_id,
        categoria=categoria,
        perfil=perfil,
        pedido="Qual é o valor? Cite a fonte.",
        situacao=situacao,
        gabarito=[RecusaAcesso()],
        comportamento_esperado=None if ativo else "A resposta recusa o pedido.",
    )


def _veredito(caso_id: str, aprovado: bool) -> Veredito:
    regra = ResultadoRegra(descricao="regra", passou=aprovado, detalhe="ok" if aprovado else "não")
    return Veredito(caso_id=caso_id, aprovado=aprovado, regras=(regra,))


def _rodada(
    numero: int,
    classificacao: Classificacao,
    *,
    caso_id: str = "consulta-01",
    tokens: tuple[int, int] = (0, 0),
    custo: float | None = None,
    hash_prompt: str | None = None,
    detalhe: str | None = None,
) -> RodadaCaso:
    veredito: Veredito | None = None
    if classificacao is APROVADA:
        veredito = _veredito(caso_id, True)
    elif classificacao is REPROVADA:
        veredito = _veredito(caso_id, False)
    return RodadaCaso(
        numero=numero,
        classificacao=classificacao,
        veredito=veredito,
        tokens_entrada=tokens[0],
        tokens_saida=tokens[1],
        custo=custo,
        hash_prompt=hash_prompt,
        detalhe=detalhe,
    )


def _cenario() -> tuple[list[Caso], dict[str, list[RodadaCaso]]]:
    """Três casos ativos e um pendente, com rodadas escolhidas para conferir cada regra.

    A (consulta-01): aprovada, aprovada, reprovada. Taxa 2/3.
    B (cruzamento-01): aprovada, erro de infraestrutura, reprovada. Taxa 1/2, sem a rodada 2.
    C (conformidade-01): três não executadas. Sem taxa.
    D (permissao-01): pendente, fora da média.
    """
    casos = [
        _caso("consulta-01", Categoria.CONSULTA),
        _caso("cruzamento-01", Categoria.CRUZAMENTO, perfil="Financeiro"),
        _caso("conformidade-01", Categoria.CONFORMIDADE, perfil="Direção"),
        _caso("permissao-01", Categoria.PERMISSAO, perfil="Marketing", ativo=False),
    ]
    rodadas = {
        "consulta-01": [
            _rodada(1, APROVADA, tokens=(100, 10), custo=0.10, hash_prompt="abc"),
            _rodada(2, APROVADA, tokens=(100, 10), custo=0.20),
            _rodada(3, REPROVADA, tokens=(100, 10)),
        ],
        "cruzamento-01": [
            _rodada(1, APROVADA, caso_id="cruzamento-01", tokens=(50, 5), custo=0.05),
            _rodada(2, ERRO, caso_id="cruzamento-01", detalhe="erro_llm"),
            _rodada(3, REPROVADA, caso_id="cruzamento-01", tokens=(50, 5)),
        ],
        "conformidade-01": [_rodada(n, NAO_EXECUTADA, detalhe="teto") for n in (1, 2, 3)],
        "permissao-01": [],
    }
    return casos, rodadas


def _agregar(
    casos: list[Caso],
    rodadas: dict[str, list[RodadaCaso]],
    *,
    provedores: tuple[str, ...] = (),
    modelo: str | None = None,
    variante: Literal["repl", "json"] = "repl",
    teto_motivo: str | None = None,
) -> Relatorio:
    return agregar(
        casos,
        rodadas,
        semente=42,
        rodadas_por_caso=3,
        provedores=provedores,
        modelo=modelo,
        variante=variante,
        teto_motivo=teto_motivo,
    )


def test_taxas_desvios_e_totais_conferidos_a_mao() -> None:
    casos, rodadas = _cenario()
    relatorio = _agregar(casos, rodadas, provedores=("Provedor B", "Provedor A"), modelo="m")

    por_caso = {resumo.caso_id: resumo for resumo in relatorio.casos}
    # A: 2 aprovadas em 3 válidas. Desvio populacional de [1, 1, 0] é raiz de 2/9.
    assert por_caso["consulta-01"].taxa == pytest.approx(2 / 3)
    assert por_caso["consulta-01"].desvio == pytest.approx(math.sqrt(2 / 9))
    # B: a erro_infra fica de fora, sobra [1, 0]. Taxa 1/2 e desvio 1/2.
    assert por_caso["cruzamento-01"].taxa == pytest.approx(0.5)
    assert por_caso["cruzamento-01"].desvio == pytest.approx(0.5)
    # C: nenhuma rodada válida. D: pendente, sem rodadas.
    assert por_caso["conformidade-01"].taxa is None
    assert por_caso["conformidade-01"].desvio is None
    assert por_caso["permissao-01"].situacao == "pendente"
    assert por_caso["permissao-01"].taxa is None

    categorias = {c.categoria: c for c in relatorio.categorias}
    assert categorias[Categoria.CONSULTA].taxa == pytest.approx(2 / 3)
    assert categorias[Categoria.CRUZAMENTO].taxa == pytest.approx(0.5)
    assert categorias[Categoria.CONFORMIDADE].taxa is None
    assert categorias[Categoria.CONFORMIDADE].casos_considerados == 0
    assert Categoria.PERMISSAO not in categorias

    # Taxa total: média das taxas dos casos com taxa, (2/3 + 1/2) / 2 = 7/12.
    assert relatorio.taxa_total == pytest.approx(7 / 12)
    # Por rodada: k1 = [A aprovada, B aprovada] -> 1; k2 = [A aprovada] -> 1; k3 = [0, 0] -> 0.
    assert relatorio.taxas_por_rodada == (1.0, 1.0, 0.0)
    # Desvio entre rodadas: população de [1, 1, 0], raiz de 2/9.
    assert relatorio.desvio_entre_rodadas == pytest.approx(math.sqrt(2 / 9))

    assert relatorio.tokens_entrada_total == 400
    assert relatorio.tokens_saida_total == 40
    assert relatorio.custo_total == pytest.approx(0.35)
    # Rodadas que responderam sem custo: A3 e B3. A rodada erro_infra não respondeu.
    assert relatorio.rodadas_sem_custo == 2
    assert relatorio.erros_infra == 1
    assert relatorio.nao_executadas == 3
    assert relatorio.pendentes == ("permissao-01",)
    assert relatorio.provedores == ("Provedor A", "Provedor B")
    assert relatorio.modelo == "m"
    assert relatorio.hash_prompt == "abc"
    assert relatorio.teto_estourado is False


def test_hash_dos_casos_e_sha256_de_casos_para_json() -> None:
    casos, rodadas = _cenario()
    relatorio = _agregar(casos, rodadas)

    esperado = hashlib.sha256(casos_para_json(casos).encode("utf-8")).hexdigest()
    assert relatorio.hash_casos == esperado


def test_relatorio_sem_nenhuma_rodada_valida() -> None:
    casos = [_caso("consulta-01", Categoria.CONSULTA)]
    rodadas = {"consulta-01": [_rodada(n, ERRO, detalhe="erro_llm") for n in (1, 2, 3)]}

    relatorio = _agregar(casos, rodadas)

    assert relatorio.casos[0].taxa is None
    assert relatorio.casos[0].desvio is None
    assert relatorio.taxa_total is None
    assert relatorio.taxas_por_rodada == (None, None, None)
    assert relatorio.desvio_entre_rodadas is None
    assert relatorio.custo_total is None
    assert relatorio.erros_infra == 3


def test_com_uma_rodada_o_desvio_entre_rodadas_e_none() -> None:
    casos = [_caso("consulta-01", Categoria.CONSULTA)]
    rodadas = {"consulta-01": [_rodada(1, APROVADA, caso_id="consulta-01")]}

    relatorio = agregar(casos, rodadas, semente=7, rodadas_por_caso=1)

    assert relatorio.rodadas_por_caso == 1
    assert relatorio.taxas_por_rodada == (1.0,)
    assert relatorio.desvio_entre_rodadas is None
    assert relatorio.casos[0].desvio is None
    assert relatorio.taxa_total == 1.0


def test_custo_fica_none_quando_nenhuma_resposta_informa_custo() -> None:
    casos = [_caso("consulta-01", Categoria.CONSULTA)]
    rodadas = {"consulta-01": [_rodada(1, APROVADA, tokens=(10, 2))]}

    relatorio = agregar(casos, rodadas, semente=1, rodadas_por_caso=1)

    assert relatorio.custo_total is None
    assert relatorio.rodadas_sem_custo == 1
    assert relatorio.tokens_entrada_total == 10


def test_ida_e_volta_do_json_nao_perde_nada() -> None:
    casos, rodadas = _cenario()
    relatorio = _agregar(casos, rodadas, provedores=("A",), modelo="m", variante="json")

    texto = relatorio_para_json(relatorio)
    de_volta = relatorio_de_json(texto)

    assert de_volta == relatorio
    assert de_volta.variante == "json"
    assert '"consulta"' in texto


def test_teto_estourado_exige_motivo_e_aparece_no_relatorio() -> None:
    casos, rodadas = _cenario()
    relatorio = _agregar(casos, rodadas, teto_motivo="custo acumulado passou do teto")

    assert relatorio.teto_estourado is True
    assert relatorio.teto_motivo == "custo acumulado passou do teto"


def test_rodada_pendente_em_caso_ativo_e_recusada() -> None:
    casos = [_caso("consulta-01", Categoria.CONSULTA)]
    rodadas = {"consulta-01": [_rodada(1, Classificacao.PENDENTE)]}

    with pytest.raises(ValueError, match="pendente"):
        agregar(casos, rodadas, semente=1, rodadas_por_caso=1)


def test_rodada_executada_em_caso_pendente_e_recusada() -> None:
    casos = [_caso("permissao-01", Categoria.PERMISSAO, ativo=False)]
    rodadas = {"permissao-01": [_rodada(1, APROVADA, caso_id="permissao-01")]}

    with pytest.raises(ValueError, match="pendente"):
        agregar(casos, rodadas, semente=1, rodadas_por_caso=1)


def test_numero_de_rodada_fora_da_faixa_e_recusado() -> None:
    casos = [_caso("consulta-01", Categoria.CONSULTA)]
    rodadas = {"consulta-01": [_rodada(4, APROVADA, caso_id="consulta-01")]}

    with pytest.raises(ValueError, match="acima de 3"):
        agregar(casos, rodadas, semente=1, rodadas_por_caso=3)


def test_rodada_fora_do_conjunto_e_recusada() -> None:
    casos = [_caso("consulta-01", Categoria.CONSULTA)]

    with pytest.raises(ValueError, match="fora do conjunto"):
        agregar(casos, {"consulta-99": []}, semente=1, rodadas_por_caso=1)


def test_aprovada_sem_veredito_aprovado_e_recusada() -> None:
    with pytest.raises(ValidationError):
        RodadaCaso(numero=1, classificacao=APROVADA, veredito=_veredito("x", False))


def test_reprovada_com_veredito_aprovado_e_recusada() -> None:
    with pytest.raises(ValidationError):
        RodadaCaso(numero=1, classificacao=REPROVADA, veredito=_veredito("x", True))


def test_erro_infra_nao_tem_veredito() -> None:
    with pytest.raises(ValidationError):
        RodadaCaso(numero=1, classificacao=ERRO, veredito=_veredito("x", False))


def test_detalhe_so_vale_para_erro_infra_e_nao_executada() -> None:
    with pytest.raises(ValidationError):
        RodadaCaso(numero=1, classificacao=APROVADA, veredito=_veredito("x", True), detalhe="x")


def test_escrever_cria_a_pasta_e_nunca_sobrescreve(tmp_path: Path) -> None:
    casos, rodadas = _cenario()
    relatorio = _agregar(casos, rodadas)
    pasta = tmp_path / "avaliacoes" / "novas"

    primeiro = escrever_relatorio(relatorio, pasta)
    segundo = escrever_relatorio(relatorio, pasta)

    assert primeiro != segundo
    assert primeiro.parent == pasta
    assert primeiro.name.startswith("avaliacao-")
    assert primeiro.name.endswith("-semente42.json")
    assert relatorio_de_json(primeiro.read_text(encoding="utf-8")) == relatorio
    assert relatorio_de_json(segundo.read_text(encoding="utf-8")) == relatorio


def test_falha_de_io_levanta_erro_claro(tmp_path: Path) -> None:
    casos, rodadas = _cenario()
    relatorio = _agregar(casos, rodadas)
    # Um arquivo no lugar da pasta impede a criação do diretório.
    bloqueio = tmp_path / "bloqueio"
    bloqueio.write_text("não é pasta", encoding="utf-8")

    with pytest.raises(ErroDeRelatorio, match="não foi possível gravar o relatório"):
        escrever_relatorio(relatorio, bloqueio / "sub")


# --- grupos -----------------------------------------------------------------------------------


def _caso_com_grupo(
    caso_id: str, *, grupo: Literal["desenvolvimento", "reservado"], ativo: bool = True
) -> Caso:
    situacao = (
        Situacao(ativo=True)
        if ativo
        else Situacao(
            ativo=False,
            motivo_pendente="aguardando a alçada da diretoria",
            bloqueios=("aprovacao_humana",),
        )
    )
    return Caso(
        id=caso_id,
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=situacao,
        gabarito=[RecusaAcesso()],
        grupo=grupo,
        comportamento_esperado=None if ativo else "A resposta recusa o pedido.",
    )


def _cenario_com_grupos() -> tuple[list[Caso], dict[str, list[RodadaCaso]]]:
    """Dois casos de desenvolvimento, um reservado e um reservado pendente.

    consulta-01 (desenvolvimento): aprovada e reprovada. Taxa 0,5.
    consulta-02 (desenvolvimento): aprovada. Taxa 1,0.
    consulta-03 (reservado): reprovada. Taxa 0,0.
    consulta-04 (reservado, pendente): sem rodadas, fora de todos os grupos.
    Desenvolvimento: média de 0,5 e 1,0 = 0,75. Reservado: 0,0. Total: (0,5 + 1,0 + 0,0) / 3.
    """
    casos = [
        _caso_com_grupo("consulta-01", grupo="desenvolvimento"),
        _caso_com_grupo("consulta-02", grupo="desenvolvimento"),
        _caso_com_grupo("consulta-03", grupo="reservado"),
        _caso_com_grupo("consulta-04", grupo="reservado", ativo=False),
    ]
    rodadas = {
        "consulta-01": [
            _rodada(1, APROVADA, caso_id="consulta-01"),
            _rodada(2, REPROVADA, caso_id="consulta-01"),
        ],
        "consulta-02": [_rodada(1, APROVADA, caso_id="consulta-02")],
        "consulta-03": [_rodada(1, REPROVADA, caso_id="consulta-03")],
        "consulta-04": [],
    }
    return casos, rodadas


def test_taxa_por_grupo_conferida_a_mao() -> None:
    casos, rodadas = _cenario_com_grupos()
    relatorio = _agregar(casos, rodadas)

    grupos = {resumo.grupo: resumo for resumo in relatorio.grupos}
    assert [resumo.grupo for resumo in relatorio.grupos] == ["desenvolvimento", "reservado"]
    assert grupos["desenvolvimento"].taxa == pytest.approx(0.75)
    assert grupos["desenvolvimento"].casos_considerados == 2
    assert grupos["reservado"].taxa == pytest.approx(0.0)
    assert grupos["reservado"].casos_considerados == 1
    assert relatorio.taxa_total == pytest.approx(0.5)
    assert [resumo.grupo for resumo in relatorio.casos] == [
        "desenvolvimento",
        "desenvolvimento",
        "reservado",
        "reservado",
    ]


def test_grupo_sem_casos_ativos_nao_aparece_no_relatorio() -> None:
    casos = [_caso_com_grupo("consulta-01", grupo="desenvolvimento")]
    relatorio = _agregar(casos, {"consulta-01": [_rodada(1, APROVADA, caso_id="consulta-01")]})

    assert [resumo.grupo for resumo in relatorio.grupos] == ["desenvolvimento"]


def test_grupos_sobrevivem_ao_json() -> None:
    casos, rodadas = _cenario_com_grupos()
    relatorio = _agregar(casos, rodadas)

    de_volta = relatorio_de_json(relatorio_para_json(relatorio))

    assert de_volta == relatorio
    assert de_volta.grupos == relatorio.grupos


def test_relatorio_antigo_sem_grupo_carrega_como_desenvolvimento() -> None:
    casos, rodadas = _cenario_com_grupos()
    dados = json.loads(relatorio_para_json(_agregar(casos, rodadas)))
    for resumo in dados["casos"]:
        del resumo["grupo"]
    del dados["grupos"]

    antigo = relatorio_de_json(json.dumps(dados))

    assert {resumo.grupo for resumo in antigo.casos} == {"desenvolvimento"}
    assert antigo.grupos == ()
