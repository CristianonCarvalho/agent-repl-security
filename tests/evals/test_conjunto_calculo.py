"""Testes do conjunto de cálculo (`casos_de_calculo`), oráculo e coerência com a planilha.

Cobre aprovação das respostas corretas e reprovação de erros típicos nas escalas 1, 10 e 100
(sementes 42 e 7), unicidade do máximo de mediana por região, leitura de volta via openpyxl,
integridade do conjunto v1, seleção via CLI (`--conjunto calculo`) e compatibilidade de relatórios
antigos. Não usa rede nem Docker; gera escala 100 uma vez por módulo via fixture.
"""

import hashlib
import io
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, main
from harness.evals.casos import (
    Caso,
    Categoria,
    RecusaAcesso,
    Situacao,
    casos_para_json,
    formatar_numero_br,
)
from harness.evals.conjunto import casos_de_calculo, conjunto_completo
from harness.evals.correcao import ResultadoRegra, Veredito, corrigir
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.procedencia import RAIZ_DO_REPOSITORIO
from harness.evals.relatorio import (
    Classificacao,
    Relatorio,
    RodadaCaso,
    agregar,
    relatorio_de_json,
    relatorio_para_json,
)
from harness.llm.base import LLMFalso

SEMENTES = (42, 7)
ESCALAS = (1, 10, 100)
PEDIDOS_CAMINHO = "vendas/pedidos_2026-10.xlsx"
ARQUIVO_CONGELADO = RAIZ_DO_REPOSITORIO / "evals" / "conjuntos" / "v2-semente42.json"
HASH_CONJUNTO_ATUAL = "a12b79692bc51f7c790372dba5acd5072e687a318188a8a3eff4130207102405"
DIGEST_GERACAO_V1 = "543f15528770bcac4b2eb60b21637f3fc605146c1203ad880032114dddf51910"


def _n(valor: Decimal) -> str:
    return formatar_numero_br(valor)


def _digest(raiz: Path) -> str:
    digest = hashlib.sha256()
    for caminho in sorted(raiz.rglob("*")):
        if caminho.is_file():
            relativo = caminho.relative_to(raiz).as_posix().encode("utf-8")
            dados_hash = hashlib.sha256(caminho.read_bytes()).digest()
            digest.update(relativo + b"\0" + dados_hash)
    return digest.hexdigest()


def _ler_pedidos_openpyxl(raiz: Path) -> list[tuple[str, str, Decimal, str]]:
    """Lê (cliente, regiao, valor, status) da planilha de pedidos via openpyxl."""
    planilha = load_workbook(raiz / PEDIDOS_CAMINHO, read_only=True)["pedidos"]
    linhas = list(planilha.iter_rows(values_only=True))
    return [
        (
            str(linha[1]),
            str(linha[2]),
            Decimal(str(linha[4])).quantize(Decimal("0.01")),
            str(linha[6]),
        )
        for linha in linhas[1:]
    ]


def _mediana_decimal(valores: Sequence[Decimal]) -> Decimal:
    ordenados = sorted(valores)
    n = len(ordenados)
    meio = n // 2
    if n % 2 == 1:
        return ordenados[meio]
    return ((ordenados[meio - 1] + ordenados[meio]) / Decimal(2)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _media_decimal(valores: Sequence[Decimal]) -> Decimal:
    return (sum(valores, Decimal(0)) / Decimal(len(valores))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _desvio_decimal(valores: Sequence[Decimal], *, amostral: bool) -> Decimal:
    n = len(valores)
    divisor = n - 1 if amostral else n
    soma = sum(valores, Decimal(0))
    media = soma / Decimal(n)
    soma_quadrados = sum((v - media) ** 2 for v in valores)
    with localcontext() as ctx:
        ctx.prec = 50
        desvio = (soma_quadrados / Decimal(divisor)).sqrt()
    return desvio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.fixture(scope="module")
def geracoes(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[tuple[int, int], tuple[Path, FatosVerdadeiros]]]:
    """Gera os dados para sementes (42, 7) e escalas (1, 10, 100) uma única vez no módulo."""
    base = tmp_path_factory.mktemp("calculo")
    resultado: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]] = {}
    for semente in SEMENTES:
        for escala in ESCALAS:
            destino = base / f"s{semente}_e{escala}"
            fatos = gerar_dados(destino, semente, escala=escala)
            resultado[(semente, escala)] = (destino, fatos)
    yield resultado


@pytest.mark.parametrize("semente", SEMENTES)
@pytest.mark.parametrize("escala", ESCALAS)
def test_oraculo_aprova_respostas_corretas(
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
    semente: int,
    escala: int,
) -> None:
    _, fatos = geracoes[(semente, escala)]
    casos = {caso.id: caso for caso in casos_de_calculo(fatos)}
    fonte = [PEDIDOS_CAMINHO]

    assert corrigir(casos["calculo-01"], f"R$ {_n(fatos.mediana_validos)}", fonte).aprovado
    mediana_regiao = fatos.mediana_por_regiao[fatos.regiao_maior_mediana]
    assert corrigir(
        casos["calculo-02"],
        f"{fatos.regiao_maior_mediana}: R$ {_n(mediana_regiao)}",
        fonte,
    ).aprovado
    assert corrigir(casos["calculo-03"], f"R$ {_n(fatos.desvio_padrao_validos)}", fonte).aprovado
    assert corrigir(
        casos["calculo-04"],
        f"{fatos.cliente_maior_total}: R$ {_n(fatos.valor_maior_total)}",
        fonte,
    ).aprovado


@pytest.mark.parametrize("semente", SEMENTES)
@pytest.mark.parametrize("escala", ESCALAS)
def test_oraculo_reprova_erros_tipicos(
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
    semente: int,
    escala: int,
) -> None:
    destino, fatos = geracoes[(semente, escala)]
    casos = {caso.id: caso for caso in casos_de_calculo(fatos)}
    fonte = [PEDIDOS_CAMINHO]
    pedidos = _ler_pedidos_openpyxl(destino)
    validos = [valor for _, _, valor, status in pedidos if status != "cancelado"]
    todos = [valor for _, _, valor, _ in pedidos]

    # Média no lugar da mediana (calculo-01)
    media_validos = _media_decimal(validos)
    assert media_validos != fatos.mediana_validos
    assert not corrigir(casos["calculo-01"], f"R$ {_n(media_validos)}", fonte).aprovado

    # Incluir cancelados na mediana (calculo-01) e no desvio padrão (calculo-03)
    mediana_todos = _mediana_decimal(todos)
    assert mediana_todos != fatos.mediana_validos
    assert not corrigir(casos["calculo-01"], f"R$ {_n(mediana_todos)}", fonte).aprovado

    desvio_todos = _desvio_decimal(todos, amostral=False)
    assert desvio_todos != fatos.desvio_padrao_validos
    assert not corrigir(casos["calculo-03"], f"R$ {_n(desvio_todos)}", fonte).aprovado

    # Desvio amostral no lugar do populacional (calculo-03)
    desvio_amostral = _desvio_decimal(validos, amostral=True)
    assert abs(desvio_amostral - fatos.desvio_padrao_validos) > Decimal("0.01")
    assert not corrigir(casos["calculo-03"], f"R$ {_n(desvio_amostral)}", fonte).aprovado

    # Outra região (calculo-02), mesmo acompanhada da mediana vencedora
    maior_mediana = fatos.mediana_por_regiao[fatos.regiao_maior_mediana]
    for regiao, mediana in fatos.mediana_por_regiao.items():
        if regiao != fatos.regiao_maior_mediana:
            assert not corrigir(casos["calculo-02"], f"{regiao}: R$ {_n(mediana)}", fonte).aprovado
            assert not corrigir(
                casos["calculo-02"], f"{regiao}: R$ {_n(maior_mediana)}", fonte
            ).aprovado

    # Outro cliente ou valor errado em calculo-04
    for cliente, total in fatos.total_por_cliente.items():
        if cliente != fatos.cliente_maior_total:
            assert not corrigir(casos["calculo-04"], f"{cliente}: R$ {_n(total)}", fonte).aprovado
            assert not corrigir(
                casos["calculo-04"], f"{cliente}: R$ {_n(fatos.valor_maior_total)}", fonte
            ).aprovado
    assert not corrigir(
        casos["calculo-04"],
        f"{fatos.cliente_maior_total}: R$ {_n(fatos.valor_maior_total + Decimal('1.00'))}",
        fonte,
    ).aprovado
    assert not corrigir(
        casos["calculo-04"],
        f"{fatos.cliente_maior_total}: R$ {_n(fatos.valor_maior_total)}",
        [],
    ).aprovado


def test_calculo_04_reprova_incluir_cancelados_no_total_por_cliente(
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
) -> None:
    """Quando incluir cancelados muda o total do vencedor, responder com cancelados reprova."""
    destino, fatos = geracoes[(42, 1)]
    pedidos = _ler_pedidos_openpyxl(destino)
    total_com_cancelados = sum(
        (valor for cliente, _, valor, _ in pedidos if cliente == fatos.cliente_maior_total),
        Decimal("0.00"),
    )
    if total_com_cancelados == fatos.valor_maior_total:
        fatos_limite = replace(fatos, valor_maior_total=Decimal("100000.00"))
        caso_04 = casos_de_calculo(fatos_limite)[3]
        assert not corrigir(
            caso_04,
            f"{fatos.cliente_maior_total}: R$ {_n(Decimal('100500.00'))}",
            [PEDIDOS_CAMINHO],
        ).aprovado
    else:
        caso_04 = casos_de_calculo(fatos)[3]
        assert not corrigir(
            caso_04,
            f"{fatos.cliente_maior_total}: R$ {_n(total_com_cancelados)}",
            [PEDIDOS_CAMINHO],
        ).aprovado


@pytest.mark.parametrize("semente", SEMENTES)
@pytest.mark.parametrize("escala", ESCALAS)
def test_unicidade_do_maximo_de_mediana_por_regiao(
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
    semente: int,
    escala: int,
) -> None:
    _, fatos = geracoes[(semente, escala)]
    maior_valor = max(fatos.mediana_por_regiao.values())
    vencedoras = [
        regiao for regiao, mediana in fatos.mediana_por_regiao.items() if mediana == maior_valor
    ]
    assert len(vencedoras) == 1
    assert vencedoras[0] == fatos.regiao_maior_mediana


@pytest.mark.parametrize("semente", SEMENTES)
@pytest.mark.parametrize("escala", ESCALAS)
def test_fatos_coerentes_com_planilha_lida_de_volta(
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
    semente: int,
    escala: int,
) -> None:
    destino, fatos = geracoes[(semente, escala)]
    pedidos = _ler_pedidos_openpyxl(destino)
    validos = [
        (cliente, regiao, valor)
        for cliente, regiao, valor, status in pedidos
        if status != "cancelado"
    ]

    valores_validos = [valor for _, _, valor in validos]
    assert _mediana_decimal(valores_validos) == fatos.mediana_validos
    assert _desvio_decimal(valores_validos, amostral=False) == fatos.desvio_padrao_validos

    for regiao, mediana_esperada in fatos.mediana_por_regiao.items():
        valores_regiao = [valor for _, reg, valor in validos if reg == regiao]
        assert _mediana_decimal(valores_regiao) == mediana_esperada

    regiao_max = max(fatos.mediana_por_regiao, key=lambda r: fatos.mediana_por_regiao[r])
    assert regiao_max == fatos.regiao_maior_mediana

    totais_por_cliente = {
        cliente: sum(
            (valor for cli, _, valor in validos if cli == cliente),
            Decimal("0.00"),
        )
        for cliente in fatos.total_por_cliente
    }
    maior_total = max(totais_por_cliente.values())
    assert maior_total == fatos.valor_maior_total
    assert totais_por_cliente[fatos.cliente_maior_total] == fatos.valor_maior_total


def test_conjunto_v1_intacto(
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
) -> None:
    destino, fatos = geracoes[(42, 1)]
    texto = casos_para_json(conjunto_completo(fatos)) + "\n"

    assert hashlib.sha256(texto.encode("utf-8")).hexdigest() == HASH_CONJUNTO_ATUAL
    assert texto == ARQUIVO_CONGELADO.read_text(encoding="utf-8")
    assert _digest(destino) == DIGEST_GERACAO_V1


def _relatorio_stub(conjunto: str = "padrao", versao_conjunto: str | None = "v1") -> Relatorio:
    caso = Caso(
        id="calculo-01",
        categoria=Categoria.CALCULO,
        perfil="Vendas",
        pedido="Qual é o valor? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[RecusaAcesso()],
    )
    veredito = Veredito(
        caso_id="calculo-01",
        aprovado=True,
        regras=(ResultadoRegra(descricao="regra", passou=True, detalhe="ok"),),
    )
    rodadas = [RodadaCaso(numero=1, classificacao=Classificacao.APROVADA, veredito=veredito)]
    rel = agregar([caso], {"calculo-01": rodadas}, semente=42, rodadas_por_caso=1)
    return rel.model_copy(update={"conjunto": conjunto, "versao_conjunto": versao_conjunto})


def test_cli_conjunto_calculo_seleciona_4_casos_recusa_baseline_e_registra_relatorio(
    tmp_path: Path,
    geracoes: dict[tuple[int, int], tuple[Path, FatosVerdadeiros]],
) -> None:
    _, fatos = geracoes[(42, 1)]
    chamadas: list[list[Caso]] = []

    def executor_falso(
        casos: Callable[[FatosVerdadeiros], Sequence[Caso]],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
    ) -> Relatorio:
        chamadas.append(list(casos(fatos)))
        return _relatorio_stub()

    saida_dir = tmp_path / "saida"
    fala = io.StringIO()
    codigo = main(
        [
            "--conjunto",
            "calculo",
            "--rodadas",
            "1",
            "--saida",
            str(saida_dir),
            "--trace",
            str(tmp_path / "trace"),
        ],
        saida=fala,
        executar=executor_falso,
        fabrica_modelo=lambda: LLMFalso([]),
    )
    assert codigo == CODIGO_OK
    assert [c.id for c in chamadas[0]] == ["calculo-01", "calculo-02", "calculo-03", "calculo-04"]

    arquivo = next(saida_dir.glob("avaliacao-*.json"))
    gravado = relatorio_de_json(arquivo.read_text(encoding="utf-8"))
    assert gravado.conjunto == "calculo"
    assert gravado.versao_conjunto == "calculo-nao-congelado"

    raiz_falsa = tmp_path / "repo"
    raiz_falsa.mkdir()
    fala_erro = io.StringIO()
    codigo_erro = main(
        [
            "--conjunto",
            "calculo",
            "--salvar-linha-de-base",
            "base-calc",
            "--rodadas",
            "1",
            "--saida",
            str(tmp_path / "saida_err"),
            "--trace",
            str(tmp_path / "trace_err"),
        ],
        saida=fala_erro,
        executar=executor_falso,
        fabrica_modelo=lambda: LLMFalso([]),
        raiz=raiz_falsa,
        obter_procedencia=lambda _raiz: ("a" * 40, False),
    )
    assert codigo_erro == CODIGO_USO
    assert "linha de base exige o conjunto padrão" in fala_erro.getvalue()


def test_relatorio_antigo_sem_campo_conjunto_continua_lendo() -> None:
    dados = json.loads(relatorio_para_json(_relatorio_stub()))
    dados.pop("conjunto")

    antigo = relatorio_de_json(json.dumps(dados))
    assert antigo.conjunto == "padrao"
