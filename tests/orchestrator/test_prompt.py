"""Testes do prompt de sistema versionado (Tarefa 7, spec v0 passo 3; grupos por perfil, D14)."""

import hashlib
from importlib import resources

from pydantic import BaseModel

from harness.host.perfil import Perfil
from harness.host.registry import Nivel, Registro
from harness.orchestrator.prompt import (
    MARCADOR_GRUPOS,
    PromptSistema,
    carregar_modelo,
    montar_prompt_sistema,
)


class _SemArgs(BaseModel):
    pass


def _registro(*ferramentas: tuple[str, str]) -> Registro:
    registro = Registro()
    for nome, descricao in ferramentas:
        registro.ferramenta(nome=nome, nivel=Nivel.LEITURA, descricao=descricao, args=_SemArgs)(
            lambda args, perfil: None
        )
    return registro


def _perfil_com(*concessoes: str) -> Perfil:
    return Perfil(nome="Teste", departamentos=frozenset(), concessoes=frozenset(concessoes))


def _perfil_que_ve_tudo(registro: Registro) -> Perfil:
    return _perfil_com(*(ferramenta.nome for ferramenta in registro.listar()))


def _prompt(registro: Registro, perfil: Perfil | None = None) -> PromptSistema:
    """Prompt do registro para um perfil; sem perfil, o que enxerga todas as ferramentas."""
    return montar_prompt_sistema(registro, perfil or _perfil_que_ve_tudo(registro))


def test_prompt_traz_as_regras_do_harness() -> None:
    texto = _prompt(_registro()).texto
    for trecho in [
        "```python",
        "print",
        "tools.grupo.nome",
        "/dados",
        "/saida",
        "<saida_nao_confiavel>",
        "FINAL(",
        "fontes",
        "reinício",
    ]:
        assert trecho in texto, trecho


def test_marcador_dos_grupos_existe_uma_vez_no_modelo_e_some_no_texto_final() -> None:
    assert carregar_modelo().count(MARCADOR_GRUPOS) == 1
    assert MARCADOR_GRUPOS not in _prompt(_registro()).texto


def test_prompt_nao_traz_lista_de_ferramentas_so_os_nomes_dos_grupos() -> None:
    registro = _registro(
        ("catalogo.listar", "Lista arquivos.\nEm duas linhas."),
        ("arquivos.historico", "Mostra versões anteriores."),
    )
    texto = _prompt(registro).texto
    # Os grupos aparecem, em ordem, uma linha cada.
    assert "- arquivos\n- catalogo" in texto
    # Nenhuma ferramenta nem descrição entra no prompt.
    assert "catalogo.listar" not in texto
    assert "arquivos.historico" not in texto
    assert "Mostra versões anteriores" not in texto


def test_grupo_descoberta_fica_fora_da_lista_do_prompt() -> None:
    registro = _registro(("catalogo.listar", "Lista."))
    registro.ferramenta(
        nome="descoberta.buscar",
        nivel=Nivel.LEITURA,
        descricao="Procura.",
        args=_SemArgs,
    )(lambda args, perfil: None)
    texto = _prompt(registro).texto
    assert "- catalogo" in texto
    assert "- descoberta" not in texto


def test_prompt_manda_usar_search_tools_e_describe_tool() -> None:
    texto = _prompt(_registro()).texto
    assert "search_tools(query, limit=5)" in texto
    assert "describe_tool(name)" in texto


def test_prompt_proibe_chamada_de_funcao_nativa_e_exige_bloco_python() -> None:
    texto = _prompt(_registro()).texto
    for trecho in [
        "UM bloco ```python",
        "só existem DENTRO do bloco de código",
        "NUNCA use chamadas de função ou de ferramenta nativas da API",
        "tool calls",
        "FINAL(resposta, fontes)",
    ]:
        assert trecho in texto, trecho


def test_sem_ferramentas_o_catalogo_avisa() -> None:
    assert "(nenhuma ferramenta registrada)" in _prompt(_registro()).texto


def test_sem_ferramentas_de_grupo_visivel_tambem_avisa() -> None:
    registro = Registro()
    registro.ferramenta(
        nome="descoberta.buscar", nivel=Nivel.LEITURA, descricao="x", args=_SemArgs
    )(lambda args, perfil: None)
    assert "(nenhuma ferramenta registrada)" in _prompt(registro).texto


def test_hash_e_o_sha256_do_texto_final() -> None:
    registro = _registro(("catalogo.listar", "Lista."))
    prompt = _prompt(registro)
    assert prompt.hash_sha256 == hashlib.sha256(prompt.texto.encode("utf-8")).hexdigest()


def test_hash_e_estavel_e_nao_depende_da_ordem_de_registro() -> None:
    a = _registro(("catalogo.listar", "Lista."), ("arquivos.historico", "Histórico."))
    b = _registro(("arquivos.historico", "Histórico."), ("catalogo.listar", "Lista."))
    perfil = _perfil_com("catalogo.listar", "arquivos.historico")
    assert _prompt(a, perfil).hash_sha256 == _prompt(a, perfil).hash_sha256
    assert _prompt(a, perfil).hash_sha256 == _prompt(b, perfil).hash_sha256


def test_hash_nao_depende_da_descricao_das_ferramentas() -> None:
    antes = _prompt(_registro(("catalogo.listar", "Lista.")))
    depois = _prompt(_registro(("catalogo.listar", "Lista tudo, com mais detalhes.")))
    assert antes.hash_sha256 == depois.hash_sha256


def test_hash_nao_muda_ao_adicionar_ferramenta_a_grupo_existente() -> None:
    antes = _prompt(_registro(("catalogo.listar", "Lista.")))
    depois = _prompt(_registro(("catalogo.listar", "Lista."), ("catalogo.buscar", "Busca.")))
    assert antes.hash_sha256 == depois.hash_sha256


def test_hash_muda_ao_adicionar_grupo_novo() -> None:
    antes = _prompt(_registro(("catalogo.listar", "Lista.")))
    depois = _prompt(_registro(("catalogo.listar", "Lista."), ("arquivos.historico", "Histórico.")))
    assert antes.hash_sha256 != depois.hash_sha256


def test_arquivo_do_prompt_faz_parte_do_pacote() -> None:
    arquivo = resources.files("harness.orchestrator.prompts").joinpath("sistema.md")
    assert arquivo.is_file()


# --- Grupos por perfil (D14) ---------------------------------------------------------------


def test_so_entram_os_grupos_com_ferramenta_concedida_ao_perfil() -> None:
    registro = _registro(
        ("catalogo.listar", "Lista."),
        ("arquivos.historico", "Histórico."),
    )
    texto = _prompt(registro, _perfil_com("catalogo.listar")).texto
    assert "- catalogo" in texto
    assert "- arquivos" not in texto


def test_grupo_aparece_quando_uma_de_suas_ferramentas_e_concedida() -> None:
    registro = _registro(("arquivos.historico", "Histórico."), ("arquivos.copia", "Cópia."))
    texto = _prompt(registro, _perfil_com("arquivos.copia")).texto
    assert "- arquivos" in texto


def test_perfil_sem_concessao_ve_so_o_aviso_de_nenhuma_ferramenta() -> None:
    registro = _registro(("catalogo.listar", "Lista."))
    assert "(nenhuma ferramenta registrada)" in _prompt(registro, _perfil_com()).texto


def test_hash_difere_entre_perfis_com_grupos_distintos() -> None:
    registro = _registro(("catalogo.listar", "Lista."), ("arquivos.historico", "Histórico."))
    so_catalogo = _prompt(registro, _perfil_com("catalogo.listar"))
    so_arquivos = _prompt(registro, _perfil_com("arquivos.historico"))
    assert so_catalogo.hash_sha256 != so_arquivos.hash_sha256


def test_hash_e_igual_entre_perfis_com_os_mesmos_grupos() -> None:
    # Dentro do grupo `catalogo`, concessões diferentes não mudam o prompt.
    registro = _registro(("catalogo.listar", "Lista."), ("catalogo.buscar", "Busca."))
    so_listar = _prompt(registro, _perfil_com("catalogo.listar"))
    tudo_do_grupo = _prompt(registro, _perfil_com("catalogo.*"))
    assert so_listar.hash_sha256 == tudo_do_grupo.hash_sha256
