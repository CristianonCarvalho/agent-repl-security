"""Testes dos nomes de função da API, do hash dos esquemas e das mensagens com chamadas (T18a)."""

import pytest
from pydantic import ValidationError

from harness.llm.base import ChamadaFerramenta, LLMFalso, Mensagem, RespostaLLM
from harness.llm.ferramentas import (
    NOME_API_VALIDO,
    EsquemaFerramenta,
    hash_dos_esquemas,
    nome_do_registro,
    nome_para_api,
)

NOMES_DO_REGISTRO: tuple[str, ...] = (
    "catalogo.listar",
    "descoberta.buscar",
    "descoberta.descrever",
    "arquivos.ler_planilha",
    "arquivos.ler_documento",
    "arquivos.agregar_planilha",
)


@pytest.mark.parametrize("nome", NOMES_DO_REGISTRO)
def test_nome_da_api_respeita_o_padrao_da_funcao_e_volta_ao_registro(nome: str) -> None:
    api = nome_para_api(nome)
    assert NOME_API_VALIDO.fullmatch(api) is not None
    assert nome_do_registro(api) == nome


def test_nomes_conhecidos_mapeiam_sem_colisao() -> None:
    apis = [nome_para_api(nome) for nome in NOMES_DO_REGISTRO]
    assert len(set(apis)) == len(NOMES_DO_REGISTRO)


def test_exemplo_documentado_do_mapeamento() -> None:
    assert nome_para_api("arquivos.ler_planilha") == "arquivos__ler_planilha"
    assert nome_do_registro("arquivos__ler_planilha") == "arquivos.ler_planilha"


@pytest.mark.parametrize(
    "ruim",
    ["a__b.c", "a._b", "a_.b", "A.b", "sem_ponto", "a.b.c", "a.", "a.b-c", "a.b_", "a.__b"],
)
def test_nome_do_registro_fora_da_forma_nao_tem_mapeamento(ruim: str) -> None:
    with pytest.raises(ValueError):
        nome_para_api(ruim)


def test_nome_da_api_ambiguo_nao_volta_a_nenhum_registro() -> None:
    # `a____b` poderia ser `a.__b` ou `a__.b`. Nenhum dos dois tem mapeamento, então a volta é None.
    assert nome_do_registro("a____b") is None


@pytest.mark.parametrize("api", ["finalizar", "a__b__c", "__a", "a__", "a__1", ""])
def test_nome_da_api_fora_do_formato_nao_vira_ferramenta_do_registro(api: str) -> None:
    assert nome_do_registro(api) is None


def test_esquema_recusa_nome_que_a_api_nao_aceita() -> None:
    with pytest.raises(ValidationError):
        EsquemaFerramenta(nome="arquivos.ler", descricao="x", parametros={})


def test_hash_dos_esquemas_e_estavel_e_sensivel_ao_conteudo_e_a_ordem() -> None:
    primeiro = EsquemaFerramenta(nome="catalogo__listar", descricao="x", parametros={"a": 1})
    segundo = EsquemaFerramenta(nome="arquivos__ler_planilha", descricao="y", parametros={})
    alterado = EsquemaFerramenta(nome="catalogo__listar", descricao="z", parametros={"a": 1})
    assert hash_dos_esquemas([primeiro]) == hash_dos_esquemas([primeiro])
    assert hash_dos_esquemas([primeiro]) != hash_dos_esquemas([alterado])
    assert hash_dos_esquemas([primeiro, segundo]) != hash_dos_esquemas([segundo, primeiro])


def test_hash_dos_esquemas_nao_depende_da_ordem_das_chaves() -> None:
    ordem_a = EsquemaFerramenta(nome="catalogo__listar", descricao="x", parametros={"a": 1, "b": 2})
    ordem_b = EsquemaFerramenta(nome="catalogo__listar", descricao="x", parametros={"b": 2, "a": 1})
    assert hash_dos_esquemas([ordem_a]) == hash_dos_esquemas([ordem_b])


def test_mensagem_sem_ferramenta_mantem_o_formato_de_antes() -> None:
    mensagem = Mensagem(role="user", content="oi")
    assert mensagem.tool_call_id is None
    assert mensagem.name is None
    assert mensagem.chamadas_ferramenta == ()


def test_mensagem_tool_exige_id_e_nome() -> None:
    with pytest.raises(ValidationError):
        Mensagem(role="tool", content="resultado")
    with pytest.raises(ValidationError):
        Mensagem(role="tool", content="resultado", tool_call_id="c1")


def test_mensagem_tool_com_id_e_nome_e_valida() -> None:
    mensagem = Mensagem(
        role="tool", content="resultado", tool_call_id="c1", name="catalogo__listar"
    )
    assert mensagem.tool_call_id == "c1"


def test_campos_de_tool_nao_existem_em_outros_papeis() -> None:
    with pytest.raises(ValidationError):
        Mensagem(role="user", content="oi", name="catalogo__listar")


def test_chamadas_so_existem_em_assistant() -> None:
    chamada = ChamadaFerramenta(id="c1", nome="catalogo__listar", argumentos_brutos="{}")
    with pytest.raises(ValidationError):
        Mensagem(role="user", content="", chamadas_ferramenta=(chamada,))


def test_assistant_com_chamadas_aceita_texto_vazio() -> None:
    chamada = ChamadaFerramenta(id="c1", nome="catalogo__listar", argumentos_brutos="{}")
    mensagem = Mensagem(role="assistant", content="", chamadas_ferramenta=(chamada,))
    assert mensagem.chamadas_ferramenta == (chamada,)


def test_resposta_sem_chamadas_tem_tupla_vazia() -> None:
    assert RespostaLLM(texto="x", tokens_entrada=0, tokens_saida=0).chamadas_ferramenta == ()


def test_llm_falso_devolve_chamadas_do_roteiro_e_registra_as_ferramentas() -> None:
    esquema = EsquemaFerramenta(nome="catalogo__listar", descricao="lista", parametros={})
    chamada = ChamadaFerramenta(id="c1", nome="catalogo__listar", argumentos_brutos="{}")
    falso = LLMFalso(
        [RespostaLLM(texto="", tokens_entrada=1, tokens_saida=1, chamadas_ferramenta=(chamada,))]
    )
    resposta = falso.responder_com_ferramentas(
        [Mensagem(role="user", content="oi")], [esquema], escolha="auto"
    )
    assert resposta.chamadas_ferramenta == (chamada,)
    assert falso.ferramentas_recebidas == [((esquema,), "auto")]
    assert len(falso.chamadas) == 1


def test_llm_falso_sem_ferramentas_continua_respondendo_texto() -> None:
    falso = LLMFalso(["texto"])
    assert falso.responder([Mensagem(role="user", content="oi")]).texto == "texto"
