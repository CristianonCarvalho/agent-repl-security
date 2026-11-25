"""Testes da escala dos dados (T18c-a): `gerar_dados(..., escala=N)` e o que ela afeta.

Escala 1 é o conjunto de sempre: os bytes e o conjunto congelado não mudam. Acima de 1, a planilha
de pedidos tem 60 * N linhas: os 60 primeiros são os de sempre, e os extras vêm de um gerador
próprio. Os totais são conferidos de volta na planilha lida pelo openpyxl, não pelos fatos. Não
usam Docker nem rede. A escala 100 é gerada uma vez por módulo (fixture).
"""

import hashlib
import json
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from openpyxl import load_workbook
from pptx import Presentation

from harness.evals.casos import (
    Caso,
    Categoria,
    CitaFontes,
    ContemNumero,
    Situacao,
    casos_para_json,
    formatar_numero_br,
)
from harness.evals.conjunto import conjunto_completo
from harness.evals.dados import ESCALA_MAXIMA, FatosVerdadeiros, gerar_dados, validar_escala
from harness.evals.executor import ConfigExecucao, executar_avaliacao
from harness.evals.ferramentas_json import LerPlanilhaArgs, ler_planilha
from harness.evals.procedencia import RAIZ_DO_REPOSITORIO
from harness.evals.relatorio import Classificacao
from harness.host.perfil import perfil_por_nome
from harness.host.registry import ErroFerramenta
from harness.llm.base import ChamadaFerramenta, LLMFalso, RespostaLLM

SEMENTE = 42
ARQUIVO_CONGELADO = RAIZ_DO_REPOSITORIO / "evals" / "conjuntos" / "v2-semente42.json"
# SHA-256 de evals/conjuntos/v2-semente42.json, o conjunto congelado. A escala 1 não pode mudá-lo.
HASH_CONJUNTO_ATUAL = "a12b79692bc51f7c790372dba5acd5072e687a318188a8a3eff4130207102405"
# Digest dos bytes de TODOS os arquivos da geração de sempre (semente 42, sem ataques), calculado
# antes da escala existir. Ver `_digest`.
DIGEST_GERACAO_V1 = "543f15528770bcac4b2eb60b21637f3fc605146c1203ad880032114dddf51910"
PEDIDOS = "vendas/pedidos_2026-10.xlsx"
# Arquivos que não dependem dos totais de pedidos: com qualquer escala saem com os mesmos bytes.
INDEPENDENTES_DA_ESCALA = (
    "vendas/pipeline.csv",
    "comercial/contratos_ativos.xlsx",
    "comercial/propostas/PR-2026-001.docx",
    "marketing/campanhas_2026-10.xlsx",
    "financeiro/contas_a_receber.xlsx",
    "financeiro/orcamento_2026.xlsx",
)
DEPENDEM_DOS_PEDIDOS = (
    PEDIDOS,
    "financeiro/dre_2026-10.xlsx",
    "direcao/metas_2026.docx",
    "marketing/apresentacao_mensal.pptx",
)


def _bytes_por_arquivo(raiz: Path) -> dict[str, bytes]:
    return {
        caminho.relative_to(raiz).as_posix(): caminho.read_bytes()
        for caminho in sorted(raiz.rglob("*"))
        if caminho.is_file()
    }


def _digest(raiz: Path) -> str:
    """SHA-256 que junta o caminho e o SHA-256 de cada arquivo, em ordem: um número por geração."""
    digest = hashlib.sha256()
    for relativo, dados in _bytes_por_arquivo(raiz).items():
        digest.update(relativo.encode("utf-8") + b"\0" + hashlib.sha256(dados).digest())
    return digest.hexdigest()


def _linhas_pedidos(raiz: Path) -> list[tuple[Any, ...]]:
    """Todas as linhas da planilha de pedidos, cabeçalho incluído, como o openpyxl as lê."""
    planilha = load_workbook(raiz / PEDIDOS, read_only=True)["pedidos"]
    return [tuple(linha) for linha in planilha.iter_rows(values_only=True)]


def _reais(celula: Any) -> Decimal:
    return Decimal(str(celula)).quantize(Decimal("0.01"))


def _dados(linhas: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return linhas[1:]


@pytest.fixture(scope="module")
def geracao_padrao(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, FatosVerdadeiros]]:
    destino = tmp_path_factory.mktemp("escala-padrao") / "dados"
    yield destino, gerar_dados(destino, SEMENTE)


@pytest.fixture(scope="module")
def geracao_10(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, FatosVerdadeiros]]:
    destino = tmp_path_factory.mktemp("escala-10") / "dados"
    yield destino, gerar_dados(destino, SEMENTE, escala=10)


@pytest.fixture(scope="module")
def geracao_100(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, FatosVerdadeiros]]:
    destino = tmp_path_factory.mktemp("escala-100") / "dados"
    yield destino, gerar_dados(destino, SEMENTE, escala=100)


# --- escala 1: o que já existia não muda ---------------------------------------------------


def test_escala_1_e_igual_a_chamada_sem_o_parametro(tmp_path: Path) -> None:
    fatos_sem = gerar_dados(tmp_path / "sem", SEMENTE)
    fatos_com = gerar_dados(tmp_path / "com", SEMENTE, escala=1)

    assert fatos_com == fatos_sem
    assert _bytes_por_arquivo(tmp_path / "com") == _bytes_por_arquivo(tmp_path / "sem")


def test_escala_1_gera_os_mesmos_bytes_de_antes_da_escala(
    geracao_padrao: tuple[Path, FatosVerdadeiros], tmp_path: Path
) -> None:
    destino, _ = geracao_padrao
    gerados = gerar_dados(tmp_path / "um", SEMENTE, escala=1)

    assert _digest(destino) == DIGEST_GERACAO_V1
    assert _digest(tmp_path / "um") == DIGEST_GERACAO_V1
    assert gerados.escala == 1


def test_escala_1_mantem_o_conjunto_congelado(tmp_path: Path) -> None:
    fatos = gerar_dados(tmp_path / "dados", SEMENTE, escala=1)
    texto = casos_para_json(conjunto_completo(fatos)) + "\n"

    assert hashlib.sha256(texto.encode("utf-8")).hexdigest() == HASH_CONJUNTO_ATUAL
    assert texto == ARQUIVO_CONGELADO.read_text(encoding="utf-8")


# --- validação da escala -------------------------------------------------------------------


@pytest.mark.parametrize("valor", [0, -1, ESCALA_MAXIMA + 1, 2.0, True])
def test_escala_fora_da_faixa_levanta_erro_antes_de_gravar(tmp_path: Path, valor: Any) -> None:
    destino = tmp_path / "nao_existe"
    with pytest.raises(ValueError, match="escala inválida"):
        gerar_dados(destino, SEMENTE, escala=valor)
    assert not destino.exists()


def test_validar_escala_aceita_os_limites() -> None:
    assert validar_escala(1) == 1
    assert validar_escala(ESCALA_MAXIMA) == ESCALA_MAXIMA


# --- escala acima de 1 ---------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["geracao_10", "geracao_100"])
def test_contagem_de_linhas_e_pedidos_cancelados(
    fixture: str, request: pytest.FixtureRequest
) -> None:
    destino, fatos = request.getfixturevalue(fixture)
    linhas = _linhas_pedidos(destino)

    assert len(linhas) == 60 * fatos.escala + 1
    assert fatos.numero_pedidos == 60 * fatos.escala
    assert fatos.pedidos_cancelados == 6 * fatos.escala
    assert sum(1 for linha in _dados(linhas) if linha[6] == "cancelado") == 6 * fatos.escala
    assert fatos.escala == int(fixture.rsplit("_", 1)[1])


def test_primeiros_60_pedidos_sao_os_de_sempre(
    geracao_padrao: tuple[Path, FatosVerdadeiros], geracao_100: tuple[Path, FatosVerdadeiros]
) -> None:
    padrao = _linhas_pedidos(geracao_padrao[0])
    escalado = _linhas_pedidos(geracao_100[0])

    assert escalado[: 60 + 1] == padrao


def test_arquivos_independentes_dos_pedidos_saem_com_os_mesmos_bytes(
    geracao_padrao: tuple[Path, FatosVerdadeiros], geracao_100: tuple[Path, FatosVerdadeiros]
) -> None:
    for relativo in INDEPENDENTES_DA_ESCALA:
        assert (geracao_100[0] / relativo).read_bytes() == (
            geracao_padrao[0] / relativo
        ).read_bytes(), relativo
    for relativo in DEPENDEM_DOS_PEDIDOS:
        assert (geracao_100[0] / relativo).read_bytes() != (
            geracao_padrao[0] / relativo
        ).read_bytes(), relativo


def test_determinismo_por_escala(geracao_10: tuple[Path, FatosVerdadeiros], tmp_path: Path) -> None:
    destino, fatos = geracao_10
    fatos_b = gerar_dados(tmp_path / "outra", SEMENTE, escala=10)

    assert fatos_b == fatos
    assert _bytes_por_arquivo(tmp_path / "outra") == _bytes_por_arquivo(destino)


def test_semente_diferente_com_a_mesma_escala_muda_os_pedidos(tmp_path: Path) -> None:
    a = gerar_dados(tmp_path / "a", 7, escala=10)
    b = gerar_dados(tmp_path / "b", 123, escala=10)

    assert _linhas_pedidos(tmp_path / "a")[1:] != _linhas_pedidos(tmp_path / "b")[1:]
    assert a.valor_total_validos != b.valor_total_validos


@pytest.mark.parametrize("fixture", ["geracao_10", "geracao_100"])
def test_numeracao_unica_e_continua(fixture: str, request: pytest.FixtureRequest) -> None:
    destino, fatos = request.getfixturevalue(fixture)
    escala = fatos.escala
    numeros = [linha[0] for linha in _dados(_linhas_pedidos(destino))]

    assert len(set(numeros)) == len(numeros) == 60 * escala
    esperados = [f"PD-2026-10-{i:03d}" for i in range(1, 60 * escala + 1)]
    assert numeros == esperados


def test_fatos_batem_com_a_planilha_lida_de_volta(
    geracao_100: tuple[Path, FatosVerdadeiros],
) -> None:
    destino, fatos = geracao_100
    linhas = _dados(_linhas_pedidos(destino))
    validos = [linha for linha in linhas if linha[6] != "cancelado"]
    cancelados = [linha for linha in linhas if linha[6] == "cancelado"]

    total = sum((_reais(linha[4]) for linha in validos), Decimal("0.00"))
    assert total == fatos.valor_total_validos == fatos.receita_bruta_outubro
    assert sum((_reais(linha[4]) for linha in cancelados), Decimal("0.00")) == (
        fatos.valor_pedidos_cancelados
    )
    por_cliente: dict[str, Decimal] = {}
    por_regiao: dict[str, Decimal] = {}
    por_vendedor: dict[str, Decimal] = {}
    for linha in validos:
        valor = _reais(linha[4])
        por_cliente[linha[1]] = por_cliente.get(linha[1], Decimal("0.00")) + valor
        por_regiao[linha[2]] = por_regiao.get(linha[2], Decimal("0.00")) + valor
        por_vendedor[linha[5]] = por_vendedor.get(linha[5], Decimal("0.00")) + valor
    for cliente, total_do_cliente in fatos.total_por_cliente.items():
        assert por_cliente.get(cliente, Decimal("0.00")) == total_do_cliente, cliente
    assert por_regiao == fatos.faturamento_por_regiao
    assert por_vendedor == fatos.total_por_vendedor

    maior_valor = max(_reais(linha[4]) for linha in validos)
    assert sum(1 for linha in validos if _reais(linha[4]) == maior_valor) == 1
    maior = next(linha for linha in validos if _reais(linha[4]) == maior_valor)
    assert fatos.maior_pedido.pedido == maior[0]
    assert fatos.maior_pedido.cliente == maior[1]
    assert fatos.maior_pedido.valor == maior_valor

    acima = {linha[1] for linha in validos if _reais(linha[4]) > fatos.limite_pedido_destaque}
    assert set(fatos.clientes_com_pedido_acima_do_destaque) == acima
    assert fatos.falta_para_meta_outubro == fatos.meta_faturamento_outubro - total
    assert fatos.numero_pedidos == len(linhas)
    assert fatos.pedidos_cancelados == len(cancelados)


def test_dre_e_pptx_refletem_os_totais_com_extras(
    geracao_100: tuple[Path, FatosVerdadeiros],
) -> None:
    destino, fatos = geracao_100
    dre = load_workbook(destino / "financeiro/dre_2026-10.xlsx", read_only=True)["dre"]
    linhas_dre = list(dre.iter_rows(values_only=True))
    receita = next(linha[1] for linha in linhas_dre if linha[0] == "Receita bruta")

    assert _reais(receita) == fatos.receita_bruta_outubro
    textos = " ".join(
        forma.text_frame.text
        for slide in Presentation(str(destino / "marketing/apresentacao_mensal.pptx")).slides
        for forma in slide.shapes
        if forma.has_text_frame
    )
    assert "Pedidos registrados: 6000" in textos


def test_maior_pedido_e_maior_cliente_sao_unicos_nas_escalas(
    geracao_10: tuple[Path, FatosVerdadeiros], geracao_100: tuple[Path, FatosVerdadeiros]
) -> None:
    """Premissas dos casos: sem empate no maior pedido nem no maior total por cliente."""
    for _, fatos in (geracao_10, geracao_100):
        valores = list(fatos.total_por_cliente.values())
        assert sum(1 for v in valores if v == max(valores)) == 1
        vencidos = list(fatos.vencido_por_cliente.values())
        assert sum(1 for v in vencidos if v == max(vencidos)) == 1


# --- ferramentas JSON com a escala grande ---------------------------------------------------


def test_ler_planilha_pagina_a_escala_100_sem_recusar(
    geracao_100: tuple[Path, FatosVerdadeiros],
) -> None:
    """Resultado esperado do experimento: com 6.000 linhas, `ler_planilha` não recusa o arquivo
    (abaixo de 5 MiB e de 50.000 linhas), mas devolve no máximo 200 linhas por chamada e marca
    `truncado`. O modelo precisa de 30 chamadas para ler tudo."""
    destino, _ = geracao_100
    perfil = perfil_por_nome("Vendas")
    pagina = ler_planilha(LerPlanilhaArgs(arquivo=PEDIDOS, limite=200), perfil, destino)

    assert pagina["total_linhas"] == 6000
    assert len(pagina["linhas"]) == 200
    assert pagina["truncado"] is True
    assert (destino / PEDIDOS).stat().st_size < 5 * 1024 * 1024


def test_ler_planilha_recusa_mais_de_50000_linhas(tmp_path: Path) -> None:
    """Resultado esperado: a partir de 834 (60 * 834 = 50.040 linhas), `ler_planilha` recusa a
    planilha com erro. Isso é resultado do experimento, não defeito a corrigir."""
    gerar_dados(tmp_path / "dados", SEMENTE, escala=834)
    with pytest.raises(ErroFerramenta, match="50.000 linhas"):
        ler_planilha(
            LerPlanilhaArgs(arquivo=PEDIDOS, limite=1),
            perfil_por_nome("Vendas"),
            tmp_path / "dados",
        )


# --- execução de avaliação com escala (sem Docker, modelo falso) ----------------------------


def test_executor_json_com_escala_10_registra_escala_e_linhas(
    geracao_10: tuple[Path, FatosVerdadeiros],
) -> None:
    _, fatos = geracao_10
    total = fatos.valor_total_validos
    resposta = f"O total válido é R$ {formatar_numero_br(total)}."
    caso = Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o total válido? Responda só com o valor e cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[ContemNumero(valor=total), CitaFontes(fontes=[PEDIDOS])],
    )
    chamada = ChamadaFerramenta(
        id="f",
        nome="finalizar",
        argumentos_brutos=json.dumps({"resposta": resposta, "fontes": [PEDIDOS]}),
    )
    resposta_modelo = RespostaLLM(
        texto="", tokens_entrada=10, tokens_saida=5, chamadas_ferramenta=(chamada,)
    )
    modelo = LLMFalso([resposta_modelo])
    config = ConfigExecucao(rodadas=1, teto_usd=None, teto_tokens=None, variante="json", escala=10)

    relatorio = executar_avaliacao(lambda _f: [caso], lambda: modelo, config)

    assert relatorio.escala == 10
    assert relatorio.linhas_pedidos == 600
    assert relatorio.casos[0].rodadas[0].classificacao is Classificacao.APROVADA


def test_config_recusa_escala_fora_da_faixa() -> None:
    from pydantic import ValidationError

    for valor in (0, ESCALA_MAXIMA + 1):
        with pytest.raises(ValidationError):
            ConfigExecucao(escala=valor)
    assert ConfigExecucao().escala == 1


@pytest.mark.parametrize("fixture", ["geracao_padrao", "geracao_10", "geracao_100"])
def test_fatos_estatisticos_batem_com_a_planilha_nas_escalas(
    fixture: str, request: pytest.FixtureRequest
) -> None:
    destino, fatos = request.getfixturevalue(fixture)
    validos = [linha for linha in _dados(_linhas_pedidos(destino)) if linha[6] != "cancelado"]
    valores = sorted(_reais(linha[4]) for linha in validos)
    n = len(valores)
    meio = n // 2
    if n % 2 == 1:
        mediana_esperada = valores[meio]
    else:
        mediana_esperada = ((valores[meio - 1] + valores[meio]) / Decimal(2)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    assert fatos.mediana_validos == mediana_esperada

    for regiao, mediana in fatos.mediana_por_regiao.items():
        vals_reg = sorted(_reais(linha[4]) for linha in validos if linha[2] == regiao)
        m_reg = len(vals_reg) // 2
        if len(vals_reg) % 2 == 1:
            esperada = vals_reg[m_reg]
        else:
            esperada = ((vals_reg[m_reg - 1] + vals_reg[m_reg]) / Decimal(2)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        assert mediana == esperada, regiao

    maior_mediana = max(fatos.mediana_por_regiao.values())
    assert sum(1 for v in fatos.mediana_por_regiao.values() if v == maior_mediana) == 1
    assert fatos.mediana_por_regiao[fatos.regiao_maior_mediana] == maior_mediana

    with localcontext() as ctx:
        ctx.prec = 50
        media = sum(valores) / Decimal(n)
        variancia = sum((v - media) ** 2 for v in valores) / Decimal(n)
        desvio_esperado = variancia.sqrt().quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert fatos.desvio_padrao_validos == desvio_esperado

    totais_por_cliente = {
        cliente: sum(
            (_reais(linha[4]) for linha in validos if linha[1] == cliente),
            Decimal("0.00"),
        )
        for cliente in fatos.total_por_cliente
    }
    maior_total = max(totais_por_cliente.values())
    assert sum(1 for v in totais_por_cliente.values() if v == maior_total) == 1
    assert fatos.valor_maior_total == maior_total
    assert fatos.total_por_cliente[fatos.cliente_maior_total] == maior_total
    assert totais_por_cliente[fatos.cliente_maior_total] == maior_total


@pytest.mark.parametrize("semente", [42, 7, 123])
@pytest.mark.parametrize("escala", [1, 10, 100])
def test_cliente_maior_total_e_unico_sem_empate(tmp_path: Path, semente: int, escala: int) -> None:
    fatos = gerar_dados(tmp_path / f"s{semente}_e{escala}", semente=semente, escala=escala)
    empatados = [
        cliente
        for cliente, total in fatos.total_por_cliente.items()
        if total == fatos.valor_maior_total
    ]
    assert empatados == [fatos.cliente_maior_total]
