"""Testes das ferramentas `descoberta.buscar` e `descoberta.descrever` (Tarefa 10).

Os testes usam o registro completo (catálogo e descoberta) e registros pequenos com ferramentas
de nomes controlados, para que a pontuação e a ordem fiquem previsíveis.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.host.tools import descoberta, registrar_todas
from harness.host.tools.descoberta import TAMANHO_RESUMO
from harness.sandbox.protocol import ToolCall


class _SemArgs(BaseModel):
    pass


class _Exemplo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    obrigatorio: str
    numero: int = 3
    ativo: bool = False
    opcional: str | None = Field(default=None, description="Texto livre, se houver.")


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido\n1\n", encoding="utf-8")
    return raiz


@pytest.fixture
def registro(raiz: Path) -> Registro:
    reg = Registro()
    registrar_todas(reg, raiz)
    return reg


@pytest.fixture
def perfil() -> Perfil:
    return perfil_por_nome("Vendas")


def _despachar(
    registro: Registro, perfil: Perfil, nome: str, **args: Any
) -> tuple[bool, Any, str | None]:
    resultado = registro.despachar(ToolCall(call_id="c1", name=nome, args=args), perfil)
    return resultado.ok, resultado.data, resultado.error


def _buscar(registro: Registro, perfil: Perfil, query: str, **extra: Any) -> list[str]:
    ok, dados, erro = _despachar(registro, perfil, "descoberta.buscar", query=query, **extra)
    assert ok, erro
    return [item["nome"] for item in dados]


def _com_descoberta() -> Registro:
    """Registro vazio com só as ferramentas de descoberta; os testes acrescentam as suas."""
    novo = Registro()
    descoberta.registrar(novo)
    return novo


def _ferramenta(
    registro: Registro,
    nome: str,
    descricao: str,
    args: type[BaseModel] = _SemArgs,
) -> None:
    registro.ferramenta(nome=nome, nivel=Nivel.LEITURA, descricao=descricao, args=args)(
        lambda a, p: None
    )


# --- Busca: quais ferramentas casam ---------------------------------------------------------


def test_busca_acha_ferramenta_pelo_nome(registro: Registro, perfil: Perfil) -> None:
    assert "catalogo.listar" in _buscar(registro, perfil, "listar")


def test_busca_acha_ferramenta_pela_descricao(registro: Registro, perfil: Perfil) -> None:
    assert "catalogo.listar" in _buscar(registro, perfil, "visíveis")


def test_busca_ignora_acento_e_caixa(registro: Registro, perfil: Perfil) -> None:
    # "visíveis" na descrição não tem acento na consulta, e a caixa também muda.
    assert "catalogo.listar" in _buscar(registro, perfil, "VISIVEIS")
    assert "catalogo.listar" in _buscar(registro, perfil, "Visiveis")


def test_busca_acha_pelo_nome_de_parametro(registro: Registro, perfil: Perfil) -> None:
    # `departamento` é parâmetro de catalogo.listar e não aparece no nome nem na descrição.
    assert "catalogo.listar" in _buscar(registro, perfil, "departamento")


def test_busca_com_acento_na_consulta_casa_descricao_sem_acento(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.versoes", "Mostra o historico de versoes anteriores.")
    assert _buscar(reg, perfil, "histórico") == ["grupo.versoes"]


def test_busca_sem_resultado_devolve_lista_vazia(registro: Registro, perfil: Perfil) -> None:
    assert _buscar(registro, perfil, "xyzzy") == []


def test_consulta_so_com_pontuacao_nao_casa_nada(registro: Registro, perfil: Perfil) -> None:
    assert _buscar(registro, perfil, "!!! ...") == []


# --- Ordem: pontuação, desempate e estabilidade ---------------------------------------------


def test_nome_pesa_mais_que_descricao_e_descricao_mais_que_parametro(perfil: Perfil) -> None:
    reg = _com_descoberta()

    class _Parametro(BaseModel):
        alfa: int = 0

    _ferramenta(reg, "grupo.parametro", "Sem relação.", _Parametro)
    _ferramenta(reg, "grupo.descricao", "Usa alfa aqui.")
    _ferramenta(reg, "grupo.alfa", "Sem relação.")
    assert _buscar(reg, perfil, "alfa") == ["grupo.alfa", "grupo.descricao", "grupo.parametro"]


def test_empate_de_pontuacao_desempata_pelo_nome(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.zeta", "Usa alfa.")
    _ferramenta(reg, "grupo.beta", "Usa alfa.")
    assert _buscar(reg, perfil, "alfa") == ["grupo.beta", "grupo.zeta"]


def test_ordem_e_estavel_entre_chamadas_e_ordens_de_registro(perfil: Perfil) -> None:
    def montar(ordem: list[str]) -> Registro:
        reg = _com_descoberta()
        for nome in ordem:
            _ferramenta(reg, nome, "Usa alfa.")
        return reg

    a = montar(["grupo.um", "grupo.dois", "grupo.tres"])
    b = montar(["grupo.tres", "grupo.um", "grupo.dois"])
    assert _buscar(a, perfil, "alfa") == _buscar(a, perfil, "alfa")
    assert _buscar(a, perfil, "alfa") == _buscar(b, perfil, "alfa")


# --- Limite: 1 a 20, padrão 5 --------------------------------------------------------------


def _registro_com_muitas(quantidade: int) -> Registro:
    reg = _com_descoberta()
    for indice in range(quantidade):
        _ferramenta(reg, f"grupo.item_{chr(ord('a') + indice)}", "Usa alfa.")
    return reg


def test_limite_padrao_e_cinco(perfil: Perfil) -> None:
    reg = _registro_com_muitas(8)
    assert len(_buscar(reg, perfil, "alfa")) == 5


@pytest.mark.parametrize("limite", [1, 20])
def test_limite_nos_extremos_e_aceito(perfil: Perfil, limite: int) -> None:
    reg = _registro_com_muitas(25)
    assert len(_buscar(reg, perfil, "alfa", limit=limite)) == limite


@pytest.mark.parametrize("limite", [0, 21, -1, True, "5", 2.0])
def test_limite_fora_da_faixa_ou_de_tipo_errado_e_recusado(
    registro: Registro, perfil: Perfil, limite: Any
) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.buscar", query="listar", limit=limite)
    assert ok is False
    assert erro is not None
    assert "limit" in erro


@pytest.mark.parametrize("consulta", ["", "   ", "\n\t"])
def test_consulta_vazia_ou_so_espacos_e_recusada(
    registro: Registro, perfil: Perfil, consulta: str
) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.buscar", query=consulta)
    assert ok is False
    assert erro is not None
    assert "query" in erro


def test_argumento_extra_na_busca_e_recusado(registro: Registro, perfil: Perfil) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.buscar", query="listar", zz=1)
    assert ok is False
    assert erro is not None
    assert "zz" in erro


# --- Resumo ---------------------------------------------------------------------------------


def test_resumo_une_espacos_e_fica_em_uma_linha(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.quebrada", "Linha um.\n   Linha   dois alfa.")
    ok, dados, _ = _despachar(reg, perfil, "descoberta.buscar", query="alfa")
    assert ok is True
    assert dados == [{"nome": "grupo.quebrada", "resumo": "Linha um. Linha dois alfa."}]


def test_resumo_longo_e_truncado_com_reticencias(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.longa", "alfa " + "palavra " * 80)
    ok, dados, _ = _despachar(reg, perfil, "descoberta.buscar", query="alfa")
    assert ok is True
    resumo = dados[0]["resumo"]
    assert len(resumo) <= TAMANHO_RESUMO
    assert resumo.endswith("…")


# --- descoberta.* fica de fora ----------------------------------------------------------------


def test_ferramentas_descoberta_nao_aparecem_nos_resultados(
    registro: Registro, perfil: Perfil
) -> None:
    for consulta in ["descoberta", "buscar", "descrever", "tools", "nome"]:
        achados = _buscar(registro, perfil, consulta)
        assert all(not nome.startswith("descoberta.") for nome in achados), consulta


def test_descrever_ferramenta_de_descoberta_diz_que_nao_existe(
    registro: Registro, perfil: Perfil
) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.descrever", name="descoberta.buscar")
    assert ok is False
    assert erro is not None
    assert "não existe" in erro


# --- Descrição -------------------------------------------------------------------------------


def test_descrever_devolve_assinatura_e_exemplo_do_catalogo(
    registro: Registro, perfil: Perfil
) -> None:
    ok, dados, erro = _despachar(registro, perfil, "descoberta.descrever", name="catalogo.listar")
    assert ok, erro
    assert dados == {
        "nome": "catalogo.listar",
        "nivel": "leitura",
        "descricao": (
            "Lista os arquivos visíveis ao perfil, com tamanho, hash, data e flag de confiança."
        ),
        "parametros": [
            {"nome": "departamento", "tipo": "string|null", "obrigatorio": False, "padrao": None},
        ],
        "exemplo": 'tools.catalogo.listar(departamento="vendas")',
    }


def test_descrever_mostra_tipos_obrigatorio_padrao_e_descricao(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.exemplo", "Exemplo.", _Exemplo)
    ok, dados, erro = _despachar(reg, perfil, "descoberta.descrever", name="grupo.exemplo")
    assert ok, erro
    assert dados["parametros"] == [
        {"nome": "obrigatorio", "tipo": "string", "obrigatorio": True},
        {"nome": "numero", "tipo": "integer", "obrigatorio": False, "padrao": 3},
        {"nome": "ativo", "tipo": "boolean", "obrigatorio": False, "padrao": False},
        {
            "nome": "opcional",
            "tipo": "string|null",
            "obrigatorio": False,
            "padrao": None,
            "descricao": "Texto livre, se houver.",
        },
    ]
    assert dados["exemplo"] == ""


def test_descrever_ferramenta_sem_argumentos(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.vazia", "Sem argumentos.")
    ok, dados, _ = _despachar(reg, perfil, "descoberta.descrever", name="grupo.vazia")
    assert ok is True
    assert dados["parametros"] == []


def test_nome_inexistente_traz_ate_tres_sugestoes(registro: Registro, perfil: Perfil) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.descrever", name="catalogo.listr")
    assert ok is False
    assert erro is not None
    assert "não existe" in erro
    assert "catalogo.listar" in erro


def test_sugestoes_vem_so_de_ferramentas_visiveis(registro: Registro, perfil: Perfil) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.descrever", name="descoberta.buscr")
    assert ok is False
    assert erro is not None
    assert "descoberta.buscar" not in erro


def test_nome_inexistente_nao_ecoa_mais_de_oitenta_caracteres(
    registro: Registro, perfil: Perfil
) -> None:
    nome = "grupo." + "x" * 500
    ok, _, erro = _despachar(registro, perfil, "descoberta.descrever", name=nome)
    assert ok is False
    assert erro is not None
    assert "x" * 81 not in erro


def test_descrever_extra_e_recusado(registro: Registro, perfil: Perfil) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.descrever", name="catalogo.listar", zz=1)
    assert ok is False
    assert erro is not None
    assert "zz" in erro


def test_descrever_sem_nome_e_recusado(registro: Registro, perfil: Perfil) -> None:
    ok, _, erro = _despachar(registro, perfil, "descoberta.descrever")
    assert ok is False
    assert erro is not None
    assert "name" in erro


# --- Registro -------------------------------------------------------------------------------


def test_registro_trata_exemplo_como_opcional_com_padrao_vazio(perfil: Perfil) -> None:
    reg = _com_descoberta()
    _ferramenta(reg, "grupo.sem_exemplo", "x")
    ferramenta = reg.obter("grupo.sem_exemplo")
    assert ferramenta is not None
    assert ferramenta.exemplo == ""


def test_descoberta_e_registrada_como_leitura_sem_flags_de_privacidade(
    registro: Registro,
) -> None:
    for nome in ["descoberta.buscar", "descoberta.descrever"]:
        ferramenta = registro.obter(nome)
        assert ferramenta is not None
        assert ferramenta.nivel is Nivel.LEITURA
        assert ferramenta.dado_privado is False
        assert ferramenta.conteudo_nao_confiavel is False
        assert ferramenta.canal_externo is False
