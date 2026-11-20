"""Testes do prompt de sistema versionado (Tarefa 7, spec v0 passo 3)."""

import hashlib
from importlib import resources

from pydantic import BaseModel

from harness.host.registry import Nivel, Registro
from harness.orchestrator.prompt import MARCADOR_GRUPOS, carregar_modelo, montar_prompt_sistema


class _SemArgs(BaseModel):
    pass


def _registro(*ferramentas: tuple[str, str]) -> Registro:
    registro = Registro()
    for nome, descricao in ferramentas:
        registro.ferramenta(nome=nome, nivel=Nivel.LEITURA, descricao=descricao, args=_SemArgs)(
            lambda args, perfil: None
        )
    return registro


def test_prompt_traz_as_regras_do_harness() -> None:
    texto = montar_prompt_sistema(_registro()).texto
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
    assert MARCADOR_GRUPOS not in montar_prompt_sistema(_registro()).texto


def test_prompt_nao_traz_lista_de_ferramentas_so_os_nomes_dos_grupos() -> None:
    registro = _registro(
        ("catalogo.listar", "Lista arquivos.\nEm duas linhas."),
        ("arquivos.historico", "Mostra versões anteriores."),
    )
    texto = montar_prompt_sistema(registro).texto
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
    texto = montar_prompt_sistema(registro).texto
    assert "- catalogo" in texto
    assert "- descoberta" not in texto


def test_prompt_manda_usar_search_tools_e_describe_tool() -> None:
    texto = montar_prompt_sistema(_registro()).texto
    assert "search_tools(query, limit=5)" in texto
    assert "describe_tool(name)" in texto


def test_sem_ferramentas_o_catalogo_avisa() -> None:
    assert "(nenhuma ferramenta registrada)" in montar_prompt_sistema(_registro()).texto


def test_sem_ferramentas_de_grupo_visivel_tambem_avisa() -> None:
    registro = Registro()
    registro.ferramenta(
        nome="descoberta.buscar", nivel=Nivel.LEITURA, descricao="x", args=_SemArgs
    )(lambda args, perfil: None)
    assert "(nenhuma ferramenta registrada)" in montar_prompt_sistema(registro).texto


def test_hash_e_o_sha256_do_texto_final() -> None:
    prompt = montar_prompt_sistema(_registro(("catalogo.listar", "Lista.")))
    assert prompt.hash_sha256 == hashlib.sha256(prompt.texto.encode("utf-8")).hexdigest()


def test_hash_e_estavel_e_nao_depende_da_ordem_de_registro() -> None:
    a = _registro(("catalogo.listar", "Lista."), ("arquivos.historico", "Histórico."))
    b = _registro(("arquivos.historico", "Histórico."), ("catalogo.listar", "Lista."))
    assert montar_prompt_sistema(a).hash_sha256 == montar_prompt_sistema(a).hash_sha256
    assert montar_prompt_sistema(a).hash_sha256 == montar_prompt_sistema(b).hash_sha256


def test_hash_nao_depende_da_descricao_das_ferramentas() -> None:
    antes = montar_prompt_sistema(_registro(("catalogo.listar", "Lista.")))
    depois = montar_prompt_sistema(_registro(("catalogo.listar", "Lista tudo, com mais detalhes.")))
    assert antes.hash_sha256 == depois.hash_sha256


def test_hash_nao_muda_ao_adicionar_ferramenta_a_grupo_existente() -> None:
    antes = montar_prompt_sistema(_registro(("catalogo.listar", "Lista.")))
    depois = montar_prompt_sistema(
        _registro(("catalogo.listar", "Lista."), ("catalogo.buscar", "Busca."))
    )
    assert antes.hash_sha256 == depois.hash_sha256


def test_hash_muda_ao_adicionar_grupo_novo() -> None:
    antes = montar_prompt_sistema(_registro(("catalogo.listar", "Lista.")))
    depois = montar_prompt_sistema(
        _registro(("catalogo.listar", "Lista."), ("arquivos.historico", "Histórico."))
    )
    assert antes.hash_sha256 != depois.hash_sha256


def test_arquivo_do_prompt_faz_parte_do_pacote() -> None:
    arquivo = resources.files("harness.orchestrator.prompts").joinpath("sistema.md")
    assert arquivo.is_file()
