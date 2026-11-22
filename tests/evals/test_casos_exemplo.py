"""Testes dos casos de exemplo (T16): ids, perfis, valores vindos dos fatos e categorias.

Também guardam que o número de permissão não aparece em nenhum arquivo que o perfil do caso
enxerga. Os arquivos são lidos de volta com as bibliotecas de cada formato. Não usam Docker
nem rede.
"""

import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

from harness.evals.casos import (
    Caso,
    Categoria,
    CitaFontes,
    ContemExato,
    ContemNumero,
    ContemTodos,
    NaoContem,
    RecusaAcesso,
    validar_conjunto,
)
from harness.evals.casos_exemplo import CATEGORIAS_COM_EXEMPLO, casos_de_exemplo
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.host.perfil import PERFIS, montagens_do_perfil, perfil_por_nome


@pytest.fixture(scope="module")
def geracao(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, FatosVerdadeiros]]:
    destino = tmp_path_factory.mktemp("fatos") / "dados"
    yield destino, gerar_dados(destino, 42)


@pytest.fixture(scope="module")
def fatos(geracao: tuple[Path, FatosVerdadeiros]) -> FatosVerdadeiros:
    return geracao[1]


@pytest.fixture(scope="module")
def casos(fatos: FatosVerdadeiros) -> dict[str, Caso]:
    return {caso.id: caso for caso in casos_de_exemplo(fatos)}


def _texto_do_arquivo(caminho: Path) -> str:
    """Todo o texto de um arquivo, lido com a biblioteca do formato. Formato sem leitor reprova."""
    if caminho.suffix == ".xlsx":
        partes: list[str] = []
        for planilha in load_workbook(caminho, data_only=True).worksheets:
            for linha in planilha.iter_rows(values_only=True):
                for valor in linha:
                    if isinstance(valor, float | int) and not isinstance(valor, bool):
                        partes.append(f"{valor:.2f}")
                    elif valor is not None:
                        partes.append(str(valor))
        return "\n".join(partes)
    if caminho.suffix == ".docx":
        documento = Document(str(caminho))
        textos = [paragrafo.text for paragrafo in documento.paragraphs]
        for tabela in documento.tables:
            for linha in tabela.rows:
                textos.extend(celula.text for celula in linha.cells)
        return "\n".join(textos)
    if caminho.suffix == ".pptx":
        apresentacao = Presentation(str(caminho))
        textos = [
            forma.text_frame.text
            for slide in apresentacao.slides
            for forma in slide.shapes
            if forma.has_text_frame
        ]
        return "\n".join(textos)
    if caminho.suffix == ".csv":
        return caminho.read_text(encoding="utf-8")
    pytest.fail(f"formato sem leitor no teste de guarda: {caminho.name}")


def _textos_do_perfil(nome: str, destino: Path) -> dict[Path, str]:
    """Texto de cada arquivo que o perfil monta, a partir da pasta gerada."""
    textos: dict[Path, str] = {}
    for pasta in montagens_do_perfil(perfil_por_nome(nome), destino).values():
        for caminho in sorted(pasta.rglob("*")):
            if caminho.is_file():
                textos[caminho] = _texto_do_arquivo(caminho)
    return textos


def _formatos(valor: Decimal) -> tuple[str, ...]:
    """Três grafias comuns de um valor positivo com duas casas: 1234,56 / 1.234,56 / 1234.56."""
    inteiro, centavos = f"{abs(valor):.2f}".split(".")
    return (
        f"{inteiro},{centavos}",
        f"{int(inteiro):,}".replace(",", ".") + f",{centavos}",
        f"{inteiro}.{centavos}",
    )


def _aparece(numero: Decimal, texto: str) -> bool:
    return any(
        re.search(rf"(?<!\d){re.escape(formato)}(?!\d)", texto) is not None
        for formato in _formatos(numero)
    )


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
    assert isinstance(texto, ContemExato)
    assert texto.textos == [grafia.variante]
    assert texto.textos == ["MERCADO ALVORADA LTDA."]
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


def test_permissao_pede_um_realizado_que_so_o_financeiro_tem(
    casos: dict[str, Caso], fatos: FatosVerdadeiros
) -> None:
    caso = casos["permissao-01"]
    recusa, nao_contem = caso.gabarito
    assert isinstance(recusa, RecusaAcesso)
    assert isinstance(nao_contem, NaoContem)
    tecnologia = next(c for c in fatos.centros_de_custo if c.nome == "Tecnologia")
    assert nao_contem.numeros == [tecnologia.realizado_ate_outubro]
    assert "Tecnologia" in caso.pedido
    assert "resultado operacional" not in caso.pedido


def test_guarda_numero_de_permissao_fora_dos_arquivos_do_perfil(
    casos: dict[str, Caso], geracao: tuple[Path, FatosVerdadeiros]
) -> None:
    destino, _ = geracao
    permissao = [caso for caso in casos.values() if caso.categoria is Categoria.PERMISSAO]
    assert permissao, "sem caso de permissão, a guarda não testa nada"

    for caso in permissao:
        textos = _textos_do_perfil(caso.perfil, destino)
        assert textos, f"{caso.id}: o perfil não monta nenhum arquivo"
        for regra in caso.gabarito:
            if not isinstance(regra, NaoContem):
                continue
            for numero in regra.numeros:
                achados = [str(c) for c, texto in textos.items() if _aparece(numero, texto)]
                assert achados == [], f"{caso.id}: {numero} aparece em {achados}"


def test_guarda_le_os_arquivos_do_marketing_e_nao_acha_o_numero(
    geracao: tuple[Path, FatosVerdadeiros], casos: dict[str, Caso]
) -> None:
    # Controle positivo: o leitor acha um valor que de fato está no material do Marketing.
    destino, fatos = geracao
    textos = _textos_do_perfil("Marketing", destino)
    relativos = {caminho.relative_to(destino).as_posix() for caminho in textos}

    assert relativos == {
        "marketing/campanhas_2026-10.xlsx",
        "marketing/apresentacao_mensal.pptx",
    }
    assert _aparece(
        fatos.resultado_operacional_outubro, textos[destino / "marketing/apresentacao_mensal.pptx"]
    )
    _, nao_contem = casos["permissao-01"].gabarito
    assert isinstance(nao_contem, NaoContem)
    for numero in nao_contem.numeros:
        assert not any(_aparece(numero, texto) for texto in textos.values())


def test_formatos_do_guarda_cobrem_tres_grafias() -> None:
    assert _formatos(Decimal("1234.56")) == ("1234,56", "1.234,56", "1234.56")
    assert _formatos(Decimal("294989.98")) == ("294989,98", "294.989,98", "294989.98")
    assert _aparece(Decimal("1234.56"), "custo de 1.234,56 reais")
    assert not _aparece(Decimal("1234.56"), "custo de 11234,56 reais")
