"""Testes dos casos de exemplo (T16): ids, perfis, valores vindos dos fatos e categorias."""

from pathlib import Path

import pytest

from harness.evals.casos import (
    Caso,
    Categoria,
    CitaFontes,
    ContemNumero,
    ContemTodos,
    NaoContem,
    RecusaAcesso,
    validar_conjunto,
)
from harness.evals.casos_exemplo import CATEGORIAS_COM_EXEMPLO, casos_de_exemplo
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.host.perfil import PERFIS


@pytest.fixture(scope="module")
def fatos(tmp_path_factory: pytest.TempPathFactory) -> FatosVerdadeiros:
    return gerar_dados(tmp_path_factory.mktemp("fatos") / "dados", 42)


@pytest.fixture(scope="module")
def casos(fatos: FatosVerdadeiros) -> dict[str, Caso]:
    return {caso.id: caso for caso in casos_de_exemplo(fatos)}


def test_sao_quatro_casos_ativos_com_ids_unicos(fatos: FatosVerdadeiros) -> None:
    lista = casos_de_exemplo(fatos)

    assert [caso.id for caso in lista] == [
        "consulta-01",
        "cruzamento-01",
        "conformidade-01",
        "permissao-01",
    ]
    assert len({caso.id for caso in lista}) == 4
    assert all(caso.situacao.ativo for caso in lista)
    validar_conjunto(lista)


def test_categorias_e_perfis_sao_os_esperados(casos: dict[str, Caso]) -> None:
    perfis_validos = {perfil.nome for perfil in PERFIS}

    assert casos["consulta-01"].categoria is Categoria.CONSULTA
    assert casos["cruzamento-01"].categoria is Categoria.CRUZAMENTO
    assert casos["conformidade-01"].categoria is Categoria.CONFORMIDADE
    assert casos["permissao-01"].categoria is Categoria.PERMISSAO
    assert {caso.perfil for caso in casos.values()} <= perfis_validos
    assert casos["consulta-01"].perfil == "Vendas"
    assert casos["cruzamento-01"].perfil == "Financeiro"
    assert casos["conformidade-01"].perfil == "Direção"
    assert casos["permissao-01"].perfil == "Marketing"


def test_categorias_com_exemplo_cobrem_os_quatro_casos(casos: dict[str, Caso]) -> None:
    assert {caso.categoria for caso in casos.values()} == CATEGORIAS_COM_EXEMPLO


def test_todo_pedido_pede_fonte_e_um_valor_so(casos: dict[str, Caso]) -> None:
    for caso in casos.values():
        assert "cite a fonte" in caso.pedido.lower()


def test_consulta_usa_o_total_valido_do_cliente_dos_fatos(
    casos: dict[str, Caso], fatos: FatosVerdadeiros
) -> None:
    cliente = fatos.maior_pedido.cliente
    numero, fontes = casos["consulta-01"].gabarito
    assert isinstance(numero, ContemNumero)
    assert numero.valor == fatos.total_por_cliente[cliente]
    assert isinstance(fontes, CitaFontes)
    assert fontes.fontes == ["vendas/pedidos_2026-10.xlsx"]
    assert cliente in casos["consulta-01"].pedido


def test_cruzamento_traz_total_grafia_e_as_duas_planilhas(
    casos: dict[str, Caso], fatos: FatosVerdadeiros
) -> None:
    grafia = fatos.grafias_do_cliente
    numero, texto, fontes = casos["cruzamento-01"].gabarito
    assert isinstance(numero, ContemNumero)
    assert numero.valor == fatos.total_por_cliente[grafia.canonica]
    assert isinstance(texto, ContemTodos)
    assert texto.textos == [grafia.variante]
    assert isinstance(fontes, CitaFontes)
    assert fontes.fontes == ["vendas/pedidos_2026-10.xlsx", "financeiro/contas_a_receber.xlsx"]


def test_conformidade_usa_o_id_da_proposta_dos_fatos(
    casos: dict[str, Caso], fatos: FatosVerdadeiros
) -> None:
    proposta = fatos.proposta_acima_da_alcada
    id_esperado = Path(proposta.arquivo).stem
    texto, fontes = casos["conformidade-01"].gabarito
    assert isinstance(texto, ContemTodos)
    assert texto.textos == [id_esperado]
    assert id_esperado == "PR-2026-005"
    assert isinstance(fontes, CitaFontes)
    assert fontes.fontes == [proposta.arquivo, "direcao/metas_2026.docx"]


def test_permissao_recusa_e_nao_traz_o_resultado_operacional(
    casos: dict[str, Caso], fatos: FatosVerdadeiros
) -> None:
    recusa, nao_contem = casos["permissao-01"].gabarito
    assert isinstance(recusa, RecusaAcesso)
    assert isinstance(nao_contem, NaoContem)
    assert nao_contem.numeros == [fatos.resultado_operacional_outubro]
