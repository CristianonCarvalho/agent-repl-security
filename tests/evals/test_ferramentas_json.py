"""Testes das ferramentas de arquivo da variante JSON (T18a).

Os valores esperados saem de uma leitura independente dos arquivos gerados (openpyxl, python-docx,
python-pptx e csv), e não da própria ferramenta. Os testes de segurança cobrem caminho, permissão,
symlink, extensão, tamanho e erro de parsing. Nada aqui usa rede ou Docker.
"""

import csv
import os
import re
import shutil
import struct
import time
import tracemalloc
import zipfile
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from docx import Document
from openpyxl import xml as openpyxl_xml
from pptx import Presentation
from pydantic import ValidationError

from harness.evals.dados import gerar_dados
from harness.evals.ferramentas_json import (
    LIMITE_GRUPOS,
    LINHAS_MAXIMAS,
    MAXIMO_PARTES_XML,
    MENSAGEM_DTD,
    TAMANHO_INSPECAO_PARTE,
    TAMANHO_MAXIMO_ARQUIVO,
    AgregarPlanilhaArgs,
    LerDocumentoArgs,
    LerPlanilhaArgs,
    _declara_dtd,
    agregar_planilha,
    ler_documento,
    ler_planilha,
    nome_do_conjunto,
    perfil_json,
    registrar_arquivos,
    registro_json,
)
from harness.host.perfil import PERFIS, perfil_por_nome
from harness.host.registry import ErroFerramenta, Registro
from harness.host.tools import registrar_todas
from harness.sandbox.protocol import ToolCall

PEDIDOS = "vendas/pedidos_2026-10.xlsx"
PIPELINE = "vendas/pipeline.csv"
ATA_DOCX = "direcao/atas/ata_2026-10-07.docx"
METAS = "direcao/metas_2026.docx"
APRESENTACAO = "marketing/apresentacao_mensal.pptx"
MENSAGEM_NAO_EXISTE = "não existe ou não está disponível para o perfil"


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Dados gerados uma vez por módulo. Os testes que mexem nos arquivos copiam esta pasta."""
    pasta = tmp_path_factory.mktemp("base") / "dados"
    gerar_dados(pasta, 42)
    return pasta


@pytest.fixture
def raiz(base: Path, tmp_path: Path) -> Path:
    """Cópia própria dos dados para o teste, para que ele possa criar arquivos maliciosos."""
    copia = tmp_path / "dados"
    shutil.copytree(base, copia, symlinks=True)
    return copia


def _vendas() -> Any:
    return perfil_json(perfil_por_nome("Vendas"), "json-agregacao")


def _direcao() -> Any:
    return perfil_json(perfil_por_nome("Direção"), "json-agregacao")


def _planilha(raiz: Path, arquivo: str, **kwargs: Any) -> dict[str, Any]:
    return ler_planilha(LerPlanilhaArgs(arquivo=arquivo, **kwargs), _vendas(), raiz)


def _oraculo_pedidos(raiz: Path) -> list[dict[str, Any]]:
    """Linhas de pedidos lidas direto com openpyxl, como dicionários."""
    livro = openpyxl.load_workbook(raiz / PEDIDOS, data_only=True)
    planilha = livro.active
    assert planilha is not None
    linhas = list(planilha.iter_rows(values_only=True))
    cabecalho = linhas[0]
    return [dict(zip(cabecalho, linha, strict=True)) for linha in linhas[1:] if any(linha)]


# --- planilhas ---------------------------------------------------------------------------------


def test_ler_planilha_bate_com_o_oraculo_na_primeira_pagina(raiz: Path) -> None:
    esperado = _oraculo_pedidos(raiz)
    resultado = _planilha(raiz, PEDIDOS, limite=20)
    assert resultado["total_linhas"] == len(esperado)
    assert resultado["linhas"] == esperado[:20]
    assert resultado["truncado"] is True
    assert resultado["aba"] == "pedidos"
    assert resultado["abas"] == ["pedidos"]
    assert resultado["colunas"] == [
        "pedido",
        "cliente",
        "regiao",
        "produto",
        "valor",
        "vendedor",
        "status",
    ]


def test_paginacao_cobre_todas_as_linhas_sem_repeti_las(raiz: Path) -> None:
    esperado = _oraculo_pedidos(raiz)
    juntas: list[dict[str, Any]] = []
    inicio = 0
    while True:
        pagina = _planilha(raiz, PEDIDOS, inicio=inicio, limite=200)
        juntas.extend(pagina["linhas"])
        if not pagina["truncado"]:
            break
        inicio += 200
    assert juntas == esperado


def test_pagina_alem_do_fim_volta_vazia_e_sem_truncagem(raiz: Path) -> None:
    total = len(_oraculo_pedidos(raiz))
    pagina = _planilha(raiz, PEDIDOS, inicio=total + 10, limite=50)
    assert pagina["linhas"] == []
    assert pagina["truncado"] is False


def test_colunas_pedidas_saem_na_ordem_pedida(raiz: Path) -> None:
    pagina = _planilha(raiz, PEDIDOS, colunas=["valor", "pedido"], limite=3)
    assert all(list(linha) == ["valor", "pedido"] for linha in pagina["linhas"])
    assert pagina["linhas"][0]["pedido"] == _oraculo_pedidos(raiz)[0]["pedido"]


def test_coluna_inexistente_e_erro_claro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="coluna inexistente"):
        _planilha(raiz, PEDIDOS, colunas=["nao_existe"])


def test_colunas_repetidas_sao_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="repetidos"):
        _planilha(raiz, PEDIDOS, colunas=["valor", "valor"])


def test_aba_inexistente_e_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="não existe no arquivo"):
        _planilha(raiz, PEDIDOS, aba="aba_que_nao_existe")


def test_aba_nomeada_e_lida(raiz: Path) -> None:
    assert _planilha(raiz, PEDIDOS, aba="pedidos")["aba"] == "pedidos"


def _esperado_csv(bruto: str) -> Any:
    """Regra de conversão escrita de novo no teste: vazio é None, inteiro e decimal são número."""
    if bruto == "":
        return None
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", bruto):
        return int(bruto)
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)\.[0-9]+", bruto):
        return float(bruto)
    return bruto


def test_csv_converte_numeros_e_mantem_texto(raiz: Path) -> None:
    with (raiz / PIPELINE).open(encoding="utf-8", newline="") as arquivo:
        linhas = list(csv.reader(arquivo))
    cabecalho = linhas[0]
    pagina = _planilha(raiz, PIPELINE, limite=200)
    assert pagina["aba"] is None
    assert pagina["abas"] == []
    assert pagina["total_linhas"] == len(linhas) - 1
    for linha_lida, bruta in zip(pagina["linhas"], linhas[1:], strict=False):
        esperada = {
            nome: _esperado_csv(valor) for nome, valor in zip(cabecalho, bruta, strict=True)
        }
        assert linha_lida == esperada


def test_csv_nao_aceita_aba(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="não tem abas"):
        _planilha(raiz, PIPELINE, aba="x")


def test_datas_de_planilha_viram_texto_iso(raiz: Path) -> None:
    from datetime import datetime

    livro = openpyxl.Workbook()
    planilha = livro.active
    assert planilha is not None
    planilha.title = "datas"
    planilha.append(["quando", "valor"])
    planilha.append([datetime(2026, 10, 2, 9, 30), 12.5])
    caminho = raiz / "vendas" / "datas.xlsx"
    livro.save(caminho)
    pagina = _planilha(raiz, "vendas/datas.xlsx", limite=5)
    assert pagina["linhas"] == [{"quando": "2026-10-02T09:30:00", "valor": 12.5}]


def test_planilha_com_mais_de_50000_linhas_e_recusada(raiz: Path) -> None:
    caminho = raiz / "vendas" / "muitas.csv"
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(["a", "b"])
        for indice in range(LINHAS_MAXIMAS + 1):
            escritor.writerow([indice, "x"])
    with pytest.raises(ErroFerramenta, match="mais de 50.000 linhas"):
        _planilha(raiz, "vendas/muitas.csv")


def test_limite_de_linhas_por_resposta_e_validado() -> None:
    with pytest.raises(ValidationError):
        LerPlanilhaArgs(arquivo=PEDIDOS, limite=201)
    with pytest.raises(ValidationError):
        LerPlanilhaArgs(arquivo=PEDIDOS, limite=0)


# --- documentos --------------------------------------------------------------------------------


def _texto(raiz: Path, arquivo: str, **kwargs: Any) -> dict[str, Any]:
    return ler_documento(LerDocumentoArgs(arquivo=arquivo, **kwargs), _direcao(), raiz)


def _linhas_do_docx(caminho: Path) -> list[str]:
    documento = Document(str(caminho))
    linhas = [p.text for p in documento.paragraphs if p.text]
    for tabela in documento.tables:
        linhas.extend(" | ".join(celula.text for celula in linha.cells) for linha in tabela.rows)
    return linhas


def test_docx_traz_paragrafos_e_tabelas_do_oraculo(raiz: Path) -> None:
    texto = _texto(raiz, METAS, limite_caracteres=20000)["texto"]
    for linha in _linhas_do_docx(raiz / METAS):
        assert linha in texto, linha


def test_docx_de_ata_tem_o_titulo_primeiro(raiz: Path) -> None:
    documento = Document(str(raiz / ATA_DOCX))
    titulo = documento.paragraphs[0].text
    assert _texto(raiz, ATA_DOCX, limite_caracteres=20000)["texto"].startswith(titulo)


def test_pptx_marca_os_slides_e_traz_o_texto(raiz: Path) -> None:
    texto = _texto(raiz, APRESENTACAO, limite_caracteres=20000)["texto"]
    apresentacao = Presentation(str(raiz / APRESENTACAO))
    for numero, slide in enumerate(apresentacao.slides, start=1):
        assert f"--- Slide {numero} ---" in texto
        for forma in slide.shapes:
            if forma.has_text_frame:
                for paragrafo in forma.text_frame.paragraphs:
                    if paragrafo.text.strip():
                        assert paragrafo.text in texto
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notas = slide.notes_slide.notes_text_frame.text
            if notas.strip():
                assert notas in texto


def test_documento_pagina_por_caracteres_e_reconstitui_o_todo(raiz: Path) -> None:
    completo = _texto(raiz, METAS, limite_caracteres=20000)
    texto = completo["texto"]
    assert completo["total_caracteres"] == len(texto)
    pedacos = []
    inicio = 0
    while inicio < len(texto):
        pagina = _texto(raiz, METAS, inicio=inicio, limite_caracteres=50)
        pedacos.append(pagina["texto"])
        inicio += 50
    assert "".join(pedacos) == texto


def test_documento_truncado_sinaliza(raiz: Path) -> None:
    pagina = _texto(raiz, METAS, limite_caracteres=30)
    assert len(pagina["texto"]) == 30
    assert pagina["truncado"] is True


def test_txt_e_csv_sao_lidos_como_texto(raiz: Path) -> None:
    (raiz / "vendas" / "nota.txt").write_text("linha um\nlinha dois", encoding="utf-8")
    pagina = ler_documento(LerDocumentoArgs(arquivo="vendas/nota.txt"), _vendas(), raiz)
    assert pagina["texto"] == "linha um\nlinha dois"
    csv_texto = ler_documento(LerDocumentoArgs(arquivo=PIPELINE), _vendas(), raiz)["texto"]
    primeira = (raiz / PIPELINE).read_text(encoding="utf-8").splitlines()[0]
    assert csv_texto.splitlines()[0] == primeira


def test_limite_de_caracteres_e_validado() -> None:
    with pytest.raises(ValidationError):
        LerDocumentoArgs(arquivo=METAS, limite_caracteres=20001)


# --- agregação ---------------------------------------------------------------------------------


def _agregar(raiz: Path, perfil: Any = None, **kwargs: Any) -> dict[str, Any]:
    args = AgregarPlanilhaArgs(arquivo=PEDIDOS, **kwargs)
    return agregar_planilha(args, perfil or _vendas(), raiz)


def test_agregacao_com_filtro_e_grupo_bate_com_o_oraculo(raiz: Path) -> None:
    linhas = [r for r in _oraculo_pedidos(raiz) if r["status"] == "faturado"]
    esperado: dict[str, list[float]] = {}
    for linha in linhas:
        esperado.setdefault(linha["vendedor"], []).append(linha["valor"])
    resultado = _agregar(
        raiz,
        filtros=[{"coluna": "status", "operador": "==", "valor": "faturado"}],
        agrupar_por=["vendedor"],
        agregacoes=[
            {"coluna": "valor", "funcao": "soma"},
            {"coluna": "pedido", "funcao": "contagem"},
            {"coluna": "valor", "funcao": "media"},
            {"coluna": "valor", "funcao": "maximo"},
            {"coluna": "valor", "funcao": "minimo"},
        ],
    )
    assert resultado["linhas_consideradas"] == len(linhas)
    assert resultado["grupos_truncados"] is False
    grupos = {g["vendedor"]: g for g in resultado["grupos"]}
    assert set(grupos) == set(esperado)
    for vendedor, valores in esperado.items():
        grupo = grupos[vendedor]
        assert grupo["soma(valor)"] == pytest.approx(sum(valores))
        assert grupo["contagem(pedido)"] == len(valores)
        assert grupo["media(valor)"] == pytest.approx(sum(valores) / len(valores))
        assert grupo["maximo(valor)"] == pytest.approx(max(valores))
        assert grupo["minimo(valor)"] == pytest.approx(min(valores))


def test_agregacao_sem_grupo_da_um_grupo_so_mesmo_sem_linhas(raiz: Path) -> None:
    resultado = _agregar(
        raiz,
        filtros=[{"coluna": "valor", "operador": ">", "valor": 1e15}],
        agregacoes=[{"coluna": "valor", "funcao": "soma"}, {"coluna": "valor", "funcao": "media"}],
    )
    assert resultado["linhas_consideradas"] == 0
    assert resultado["grupos"] == [{"soma(valor)": 0, "media(valor)": None}]


def test_filtro_contem_ignora_caixa(raiz: Path) -> None:
    linhas = _oraculo_pedidos(raiz)
    alvo = linhas[0]["cliente"].split()[0]
    esperado = [r for r in linhas if alvo.casefold() in str(r["cliente"]).casefold()]
    resultado = _agregar(
        raiz,
        filtros=[{"coluna": "cliente", "operador": "contem", "valor": alvo.upper()}],
        agregacoes=[{"coluna": "pedido", "funcao": "contagem"}],
    )
    assert resultado["linhas_consideradas"] == len(esperado)


def test_filtro_em_lista_e_diferente_de_e_comparacao_numerica(raiz: Path) -> None:
    linhas = _oraculo_pedidos(raiz)
    esperado = [r for r in linhas if r["status"] in ("faturado", "entregue") and r["valor"] >= 1000]
    resultado = _agregar(
        raiz,
        filtros=[
            {"coluna": "status", "operador": "em", "valor": ["faturado", "entregue"]},
            {"coluna": "status", "operador": "!=", "valor": "nada"},
            {"coluna": "valor", "operador": ">=", "valor": 1000},
        ],
        agregacoes=[{"coluna": "pedido", "funcao": "contagem"}],
    )
    assert resultado["linhas_consideradas"] == len(esperado)


def test_agregacao_de_coluna_de_texto_numerica_e_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="não numéricos"):
        _agregar(raiz, agregacoes=[{"coluna": "cliente", "funcao": "soma"}])


def test_contagem_aceita_coluna_de_texto(raiz: Path) -> None:
    resultado = _agregar(raiz, agregacoes=[{"coluna": "cliente", "funcao": "contagem"}])
    assert resultado["grupos"] == [{"contagem(cliente)": len(_oraculo_pedidos(raiz))}]


def test_coluna_inexistente_em_filtro_ou_agregacao_e_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="coluna inexistente"):
        _agregar(
            raiz,
            filtros=[{"coluna": "nada", "operador": "==", "valor": 1}],
            agregacoes=[{"coluna": "valor", "funcao": "soma"}],
        )
    with pytest.raises(ErroFerramenta, match="coluna inexistente"):
        _agregar(raiz, agregacoes=[{"coluna": "nada", "funcao": "soma"}])


def test_operador_em_com_valor_escalar_e_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="pede uma lista"):
        _agregar(
            raiz,
            filtros=[{"coluna": "status", "operador": "em", "valor": "faturado"}],
            agregacoes=[{"coluna": "pedido", "funcao": "contagem"}],
        )


def test_operador_contem_com_numero_e_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="pede um texto"):
        _agregar(
            raiz,
            filtros=[{"coluna": "status", "operador": "contem", "valor": 3}],
            agregacoes=[{"coluna": "pedido", "funcao": "contagem"}],
        )


def test_operador_invalido_e_recusado_pelo_modelo_de_argumentos() -> None:
    with pytest.raises(ValidationError):
        AgregarPlanilhaArgs.model_validate(
            {
                "arquivo": PEDIDOS,
                "filtros": [{"coluna": "status", "operador": "LIKE", "valor": "x"}],
                "agregacoes": [{"coluna": "pedido", "funcao": "contagem"}],
            }
        )


def test_agregacao_repetida_e_erro(raiz: Path) -> None:
    with pytest.raises(ErroFerramenta, match="agregação repetida"):
        _agregar(
            raiz,
            agregacoes=[
                {"coluna": "valor", "funcao": "soma"},
                {"coluna": "valor", "funcao": "soma"},
            ],
        )


def test_grupos_sao_limitados_a_200_com_aviso(raiz: Path) -> None:
    livro = openpyxl.Workbook()
    planilha = livro.active
    assert planilha is not None
    planilha.title = "grupos"
    planilha.append(["codigo", "valor"])
    for indice in range(LIMITE_GRUPOS + 50):
        planilha.append([f"g{indice:04d}", 1])
    livro.save(raiz / "vendas" / "grupos.xlsx")
    resultado = agregar_planilha(
        AgregarPlanilhaArgs.model_validate(
            {
                "arquivo": "vendas/grupos.xlsx",
                "agrupar_por": ["codigo"],
                "agregacoes": [{"coluna": "valor", "funcao": "soma"}],
            }
        ),
        _vendas(),
        raiz,
    )
    assert len(resultado["grupos"]) == LIMITE_GRUPOS
    assert resultado["grupos_truncados"] is True


# --- segurança ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arquivo",
    [
        "../vendas/pedidos_2026-10.xlsx",
        "vendas/../financeiro/contas_a_receber.xlsx",
        "/etc/passwd",
        "vendas\\pedidos_2026-10.xlsx",
        "vendas//pedidos_2026-10.xlsx",
        "vendas",
        "vendas/nao_existe.xlsx",
        "nao_existe/arquivo.xlsx",
        "vendas/./pedidos_2026-10.xlsx",
        "C:/pedidos.xlsx",
    ],
)
def test_caminho_suspeito_ou_inexistente_recebe_a_mesma_mensagem(raiz: Path, arquivo: str) -> None:
    with pytest.raises(ErroFerramenta, match=MENSAGEM_NAO_EXISTE):
        _planilha(raiz, arquivo)


def test_arquivo_de_outro_departamento_tem_a_mesma_mensagem_do_inexistente(raiz: Path) -> None:
    negado = "financeiro/contas_a_receber.xlsx"
    inexistente = "vendas/nao_existe.xlsx"
    with pytest.raises(ErroFerramenta) as erro_negado:
        _planilha(raiz, negado)
    with pytest.raises(ErroFerramenta) as erro_inexistente:
        _planilha(raiz, inexistente)
    assert str(erro_negado.value).replace(negado, "X") == str(erro_inexistente.value).replace(
        inexistente, "X"
    )


def test_symlink_de_arquivo_e_negado(raiz: Path) -> None:
    os.symlink(raiz / PEDIDOS, raiz / "vendas" / "atalho.xlsx")
    with pytest.raises(ErroFerramenta, match=MENSAGEM_NAO_EXISTE):
        _planilha(raiz, "vendas/atalho.xlsx")


def test_symlink_de_diretorio_e_negado(raiz: Path, tmp_path: Path) -> None:
    fora = tmp_path / "fora"
    fora.mkdir()
    (fora / "segredo.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    os.symlink(fora, raiz / "vendas" / "link")
    with pytest.raises(ErroFerramenta, match=MENSAGEM_NAO_EXISTE):
        _planilha(raiz, "vendas/link/segredo.csv")


def test_extensao_nao_suportada_para_cada_leitura(raiz: Path) -> None:
    # A ata é da Direção: o perfil Vendas não a vê. O teste usa uma cópia dentro de Vendas.
    shutil.copy(raiz / ATA_DOCX, raiz / "vendas" / "copia_da_ata.docx")
    with pytest.raises(ErroFerramenta, match="extensão não suportada"):
        _planilha(raiz, "vendas/copia_da_ata.docx")
    with pytest.raises(ErroFerramenta, match="extensão não suportada"):
        ler_documento(LerDocumentoArgs(arquivo=PEDIDOS), _vendas(), raiz)
    (raiz / "vendas" / "programa.exe").write_bytes(b"MZ")
    with pytest.raises(ErroFerramenta, match="extensão não suportada"):
        ler_documento(LerDocumentoArgs(arquivo="vendas/programa.exe"), _vendas(), raiz)


def test_arquivo_acima_de_5_mib_e_recusado(raiz: Path) -> None:
    (raiz / "vendas" / "grande.csv").write_bytes(b"a" * (TAMANHO_MAXIMO_ARQUIVO + 1))
    with pytest.raises(ErroFerramenta, match="5 MiB"):
        _planilha(raiz, "vendas/grande.csv")


def test_zip_com_conteudo_descompactado_acima_do_limite_e_recusado(raiz: Path) -> None:
    caminho = raiz / "vendas" / "bomba.xlsx"
    with zipfile.ZipFile(caminho, "w", compression=zipfile.ZIP_DEFLATED) as pacote:
        pacote.writestr("enorme.xml", b"\0" * (65 * 1024 * 1024))
    assert caminho.stat().st_size < TAMANHO_MAXIMO_ARQUIVO
    with pytest.raises(ErroFerramenta, match="64 MiB"):
        _planilha(raiz, "vendas/bomba.xlsx")


def test_erro_de_parsing_nao_vaza_caminho_do_host(raiz: Path) -> None:
    (raiz / "vendas" / "corrompido.xlsx").write_bytes(b"isto nao e um zip")
    with pytest.raises(ErroFerramenta) as erro:
        _planilha(raiz, "vendas/corrompido.xlsx")
    texto = str(erro.value)
    assert texto == "não foi possível ler o arquivo informado"
    assert str(raiz) not in texto


def test_documento_corrompido_tem_mensagem_generica(raiz: Path) -> None:
    (raiz / "direcao" / "quebrado.docx").write_bytes(b"\x00\x01 nao e docx")
    with pytest.raises(ErroFerramenta) as erro:
        ler_documento(LerDocumentoArgs(arquivo="direcao/quebrado.docx"), _direcao(), raiz)
    assert str(raiz) not in str(erro.value)


def test_registro_padrao_e_perfis_embutidos_nao_ganharam_ferramentas(raiz: Path) -> None:
    registro = Registro()
    registrar_todas(registro, raiz)
    nomes = {f.nome for f in registro.listar()}
    assert not any(nome.startswith("arquivos.") for nome in nomes)
    for perfil in PERFIS:
        assert not any(c.startswith("arquivos.") for c in perfil.concessoes)


def test_registro_json_tem_so_as_ferramentas_da_variante(raiz: Path) -> None:
    basico = {f.nome for f in registro_json(raiz, "json").listar()}
    agregacao = {f.nome for f in registro_json(raiz, "json-agregacao").listar()}
    assert basico == {"catalogo.listar", "arquivos.ler_planilha", "arquivos.ler_documento"}
    assert agregacao == basico | {"arquivos.agregar_planilha"}
    assert nome_do_conjunto("json") == "basicas"
    assert nome_do_conjunto("json-agregacao") == "basicas+agregacao"


def test_perfil_json_e_copia_e_nao_altera_o_embutido(raiz: Path) -> None:
    base_perfil = perfil_por_nome("Vendas")
    copia = perfil_json(base_perfil, "json")
    assert copia.permite("arquivos.ler_planilha")
    assert not copia.permite("arquivos.agregar_planilha")
    assert not base_perfil.permite("arquivos.ler_planilha")


def test_despachante_recusa_ferramenta_negada_como_inexistente(raiz: Path) -> None:
    registro = registro_json(raiz, "json-agregacao")
    # Marketing (perfil embutido, sem a concessão de arquivos) não pode chamar a ferramenta.
    negada = registro.despachar(
        ToolCall(call_id="c1", name="arquivos.ler_planilha", args={"arquivo": PEDIDOS}),
        perfil_por_nome("Marketing"),
    )
    inexistente = registro.despachar(
        ToolCall(call_id="c2", name="arquivos.nao_existe", args={}), perfil_por_nome("Marketing")
    )
    assert negada.ok is False
    assert inexistente.ok is False
    assert "não existe" in str(negada.error)
    assert str(negada.error).replace("arquivos.ler_planilha", "X") == str(
        inexistente.error
    ).replace("arquivos.nao_existe", "X")
    # Vendas pede um arquivo do Marketing: a mesma mensagem de inexistente.
    fora = registro.despachar(
        ToolCall(
            call_id="c3",
            name="arquivos.ler_planilha",
            args={"arquivo": "marketing/campanhas_2026-10.xlsx"},
        ),
        _vendas(),
    )
    assert fora.ok is False
    assert MENSAGEM_NAO_EXISTE in str(fora.error)


def test_resultado_acima_do_limite_do_registro_e_recusado(raiz: Path) -> None:
    registro = Registro(limite_resultado_bytes=100)
    registrar_arquivos(registro, raiz, agregacao=False)
    resultado = registro.despachar(
        ToolCall(
            call_id="c1", name="arquivos.ler_planilha", args={"arquivo": PEDIDOS, "limite": 50}
        ),
        _vendas(),
    )
    assert resultado.ok is False
    assert "excede o limite" in str(resultado.error)


def test_erros_de_argumento_viram_mensagem_de_ferramenta(raiz: Path) -> None:
    registro = registro_json(raiz, "json-agregacao")
    resultado = registro.despachar(
        ToolCall(call_id="c1", name="arquivos.agregar_planilha", args={"arquivo": PEDIDOS}),
        _vendas(),
    )
    assert resultado.ok is False
    assert "argumento obrigatório ausente" in str(resultado.error)


def test_esquemas_dos_argumentos_saem_como_json_schema() -> None:
    esquema = LerPlanilhaArgs.model_json_schema()
    assert esquema["properties"]["limite"]["maximum"] == 200
    assert "arquivo" in esquema["required"]
    assert esquema.get("additionalProperties") is False


def test_csv_com_bytes_invalidos_vira_erro_generico(raiz: Path) -> None:
    (raiz / "vendas" / "ruim.csv").write_bytes(b"\xff\xfe\x00bruto")
    with pytest.raises(ErroFerramenta) as erro:
        _planilha(raiz, "vendas/ruim.csv")
    assert str(raiz) not in str(erro.value)


# --- DTD e entidades em pacotes OOXML ----------------------------------------------------------

ERRO_DE_LEITURA = "não foi possível ler o arquivo informado"
CASOS_OOXML = [
    pytest.param(PEDIDOS, "xl/worksheets/sheet1.xml", id="xlsx"),
    pytest.param(METAS, "word/document.xml", id="docx"),
    pytest.param(APRESENTACAO, "ppt/slides/slide1.xml", id="pptx"),
]
ARQUIVOS_OOXML = [PEDIDOS, METAS, APRESENTACAO]


def _ler_arquivo(raiz: Path, arquivo: str) -> dict[str, Any]:
    if arquivo.endswith(".xlsx"):
        return _planilha(raiz, arquivo)
    return _texto(raiz, arquivo)


def _declaracoes_aninhadas(niveis: int = 5) -> str:
    """Entidades aninhadas ("bilhão de risadas"): cada nível repete o anterior dez vezes."""
    partes = ['<!ENTITY lol "lol">']
    for nivel in range(1, niveis + 1):
        anterior = "lol" if nivel == 1 else f"lol{nivel - 1}"
        partes.append(f'<!ENTITY lol{nivel} "{"&" + anterior + ";" * 10}">')
    return "".join(partes)


def _ataque_xxe() -> tuple[str, str]:
    return '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/hosts">]>', "&xxe;"


def _ataque_risadas() -> tuple[str, str]:
    return f"<!DOCTYPE r [{_declaracoes_aninhadas()}]>", "&lol5;"


def _infectar(conteudo: bytes, doctype: str, referencia: str) -> bytes:
    """Põe a DTD antes da raiz e a referência como texto logo depois da abertura da raiz."""
    texto = conteudo.decode("utf-8")
    inicio = texto.index("?>") + 2 if texto.startswith("<?xml") else 0
    raiz_aberta = re.compile(r"<(?![?!])[^>]*>").search(texto, inicio)
    assert raiz_aberta is not None
    novo = (
        texto[:inicio]
        + doctype
        + texto[inicio : raiz_aberta.end()]
        + referencia
        + texto[raiz_aberta.end() :]
    )
    return novo.encode("utf-8")


def _reempacotar(origem: Path, destino: Path, trocas: dict[str, bytes]) -> None:
    """Copia o zip item a item, trocando só as partes indicadas (o resto fica igual)."""
    with (
        zipfile.ZipFile(origem) as entrada,
        zipfile.ZipFile(destino, "w", compression=zipfile.ZIP_DEFLATED) as saida,
    ):
        for item in entrada.infolist():
            dados = trocas[item.filename] if item.filename in trocas else entrada.read(item)
            saida.writestr(item.filename, dados)


def _com_parte_trocada(raiz: Path, arquivo: str, parte: str, doctype: str, referencia: str) -> None:
    caminho = raiz / arquivo
    with zipfile.ZipFile(caminho) as pacote:
        original = pacote.read(parte)
    infectada = _infectar(original, doctype, referencia)
    _reempacotar(caminho, caminho.with_suffix(".tmp"), {parte: infectada})
    caminho.with_suffix(".tmp").replace(caminho)
    with zipfile.ZipFile(caminho) as pacote:
        assert b"<!DOCTYPE" in pacote.read(parte), "o ataque precisa estar na parte alterada"


@pytest.mark.parametrize(("arquivo", "parte"), CASOS_OOXML)
def test_dtd_com_entidade_externa_e_recusada(raiz: Path, arquivo: str, parte: str) -> None:
    doctype, referencia = _ataque_xxe()
    _com_parte_trocada(raiz, arquivo, parte, doctype, referencia)
    with pytest.raises(ErroFerramenta) as erro:
        _ler_arquivo(raiz, arquivo)
    texto = str(erro.value)
    assert texto == MENSAGEM_DTD
    assert "/etc/hosts" not in texto
    assert str(raiz) not in texto


@pytest.mark.parametrize(("arquivo", "parte"), CASOS_OOXML)
def test_dtd_com_entidades_aninhadas_e_recusada(raiz: Path, arquivo: str, parte: str) -> None:
    doctype, referencia = _ataque_risadas()
    _com_parte_trocada(raiz, arquivo, parte, doctype, referencia)
    with pytest.raises(ErroFerramenta) as erro:
        _ler_arquivo(raiz, arquivo)
    texto = str(erro.value)
    assert texto == MENSAGEM_DTD
    assert "lollol" not in texto
    assert len(texto) < 200


@pytest.mark.parametrize("arquivo", ARQUIVOS_OOXML)
def test_arquivo_gerado_continua_sendo_lido(raiz: Path, arquivo: str) -> None:
    """Sem falso positivo: o gerador não põe DTD em nenhuma parte XML."""
    assert _ler_arquivo(raiz, arquivo)["arquivo"] == arquivo


def test_texto_doctype_como_conteudo_nao_e_recusado(raiz: Path) -> None:
    """DOCTYPE só conta no prólogo: como texto de célula, parágrafo ou slide, o arquivo passa."""
    documento = Document()
    documento.add_paragraph("DOCTYPE html")
    documento.add_paragraph("<!DOCTYPE html> escrito como texto")
    documento.save(str(raiz / "direcao" / "doctype.docx"))
    texto = _texto(raiz, "direcao/doctype.docx", limite_caracteres=20000)["texto"]
    assert "<!DOCTYPE html> escrito como texto" in texto

    planilha = openpyxl.Workbook()
    aba = planilha.active
    assert aba is not None
    aba.append(["DOCTYPE"])
    aba.append(["<!DOCTYPE html>"])
    planilha.save(raiz / "vendas" / "doctype.xlsx")
    linhas = _planilha(raiz, "vendas/doctype.xlsx")["linhas"]
    assert linhas == [{"DOCTYPE": "<!DOCTYPE html>"}]

    apresentacao = Presentation()
    slide = apresentacao.slides.add_slide(apresentacao.slide_layouts[5])
    assert slide.shapes.title is not None
    slide.shapes.title.text = "DOCTYPE"
    apresentacao.save(str(raiz / "direcao" / "doctype.pptx"))
    assert "DOCTYPE" in _texto(raiz, "direcao/doctype.pptx")["texto"]


def test_declaracao_no_prologo_e_detectada_mesmo_depois_de_comentario() -> None:
    """Um `<a` dentro de comentário não pode esconder a DTD que vem depois dele."""
    prologo = b'<?xml version="1.0"?><!-- <a --><!DOCTYPE r [<!ENTITY x "y">]><r/>'
    assert _declara_dtd(prologo) is True
    assert _declara_dtd(b"\xef\xbb\xbf<!entity x 'y'><r/>") is True


def test_doctype_depois_da_raiz_nao_conta() -> None:
    assert _declara_dtd(b'\xef\xbb\xbf<?xml version="1.0"?>\r\n<r><!DOCTYPE x/></r>') is False
    assert _declara_dtd(b"") is False


def test_prologo_inconclusivo_e_recusado_sem_detalhe() -> None:
    """Prólogo maior que a janela, ou em UTF-16, não se confere: a leitura é recusada."""
    comentario_longo = b"<?xml?><!--" + b"a" * TAMANHO_INSPECAO_PARTE
    with pytest.raises(ErroFerramenta) as erro:
        _declara_dtd(comentario_longo)
    assert str(erro.value) == ERRO_DE_LEITURA
    with pytest.raises(ErroFerramenta) as erro:
        _declara_dtd("<!DOCTYPE r>".encode("utf-16-le"))
    assert str(erro.value) == ERRO_DE_LEITURA


def test_pacote_com_partes_xml_demais_e_recusado(raiz: Path) -> None:
    caminho = raiz / "vendas" / "muitas_partes.xlsx"
    with zipfile.ZipFile(caminho, "w", compression=zipfile.ZIP_DEFLATED) as pacote:
        for indice in range(MAXIMO_PARTES_XML + 1):
            pacote.writestr(f"extra/{indice}.xml", b"<a/>")
    with pytest.raises(ErroFerramenta) as erro:
        _planilha(raiz, "vendas/muitas_partes.xlsx")
    assert str(erro.value) == ERRO_DE_LEITURA


def _remendar_tamanho(caminho: Path, nome: str, novo: int) -> None:
    """Troca o tamanho descompactado de uma parte nos dois lugares: cabeçalho local e diretório."""
    dados = bytearray(caminho.read_bytes())
    with zipfile.ZipFile(caminho) as pacote:
        local = pacote.getinfo(nome).header_offset
    assert dados[local : local + 4] == b"PK\x03\x04"
    struct.pack_into("<I", dados, local + 22, novo)
    posicao = dados.find(b"PK\x01\x02")
    while posicao != -1:
        tamanho_nome = struct.unpack_from("<H", dados, posicao + 28)[0]
        if bytes(dados[posicao + 46 : posicao + 46 + tamanho_nome]) == nome.encode():
            struct.pack_into("<I", dados, posicao + 24, novo)
            break
        posicao = dados.find(b"PK\x01\x02", posicao + 4)
    caminho.write_bytes(bytes(dados))


def test_zip_com_tamanho_declarado_falso_e_recusado_sem_memoria(raiz: Path) -> None:
    """Parte de 80 MiB declarada como 1000 bytes: recusada rápido e com pico de memória baixo."""
    caminho = raiz / "vendas" / "falso.xlsx"
    with zipfile.ZipFile(caminho, "w", compression=zipfile.ZIP_DEFLATED) as pacote:
        pacote.writestr("xl/worksheets/sheet1.xml", b"<worksheet>" + b" " * (80 * 1024 * 1024))
    _remendar_tamanho(caminho, "xl/worksheets/sheet1.xml", 1000)

    tracemalloc.start()
    inicio = time.perf_counter()
    try:
        with pytest.raises(ErroFerramenta) as erro:
            _planilha(raiz, "vendas/falso.xlsx")
        _, pico = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    duracao = time.perf_counter() - inicio

    assert str(erro.value) == ERRO_DE_LEITURA
    assert pico < 50 * 1024 * 1024
    assert duracao < 5


def test_openpyxl_usa_defusedxml_no_ambiente_de_testes() -> None:
    assert openpyxl_xml.DEFUSEDXML, (
        "a proteção contra expansão de entidades foi perdida: openpyxl.xml.DEFUSEDXML é False. "
        "Verifique se a dependência defusedxml está instalada (uv sync)."
    )
