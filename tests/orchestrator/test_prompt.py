"""Testes do prompt de sistema versionado (Tarefa 7, spec v0 passo 3)."""

import hashlib
from importlib import resources

from pydantic import BaseModel

from harness.host.registry import Nivel, Registro
from harness.orchestrator.prompt import MARCADOR_CATALOGO, carregar_modelo, montar_prompt_sistema


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


def test_marcador_do_catalogo_existe_uma_vez_no_modelo_e_some_no_texto_final() -> None:
    assert carregar_modelo().count(MARCADOR_CATALOGO) == 1
    assert MARCADOR_CATALOGO not in montar_prompt_sistema(_registro()).texto


def test_catalogo_tem_uma_linha_por_ferramenta_com_descricao_em_uma_linha() -> None:
    registro = _registro(
        ("catalogo.listar", "Lista arquivos.\nEm duas linhas."),
        ("arquivos.historico", "Mostra versões anteriores."),
    )
    texto = montar_prompt_sistema(registro).texto
    assert "- catalogo.listar: Lista arquivos. Em duas linhas." in texto
    assert "- arquivos.historico: Mostra versões anteriores." in texto
    # Ordenado por nome: "arquivos.historico" vem antes de "catalogo.listar".
    assert texto.index("- arquivos.historico") < texto.index("- catalogo.listar")


def test_sem_ferramentas_o_catalogo_avisa() -> None:
    assert "(nenhuma ferramenta registrada)" in montar_prompt_sistema(_registro()).texto


def test_hash_e_o_sha256_do_texto_final() -> None:
    prompt = montar_prompt_sistema(_registro(("catalogo.listar", "Lista.")))
    assert prompt.hash_sha256 == hashlib.sha256(prompt.texto.encode("utf-8")).hexdigest()


def test_hash_e_estavel_e_nao_depende_da_ordem_de_registro() -> None:
    a = _registro(("catalogo.listar", "Lista."), ("arquivos.historico", "Histórico."))
    b = _registro(("arquivos.historico", "Histórico."), ("catalogo.listar", "Lista."))
    assert montar_prompt_sistema(a).hash_sha256 == montar_prompt_sistema(a).hash_sha256
    assert montar_prompt_sistema(a).hash_sha256 == montar_prompt_sistema(b).hash_sha256


def test_hash_muda_quando_o_catalogo_muda() -> None:
    antes = montar_prompt_sistema(_registro(("catalogo.listar", "Lista.")))
    depois = montar_prompt_sistema(_registro(("catalogo.listar", "Lista tudo.")))
    assert antes.hash_sha256 != depois.hash_sha256


def test_arquivo_do_prompt_faz_parte_do_pacote() -> None:
    arquivo = resources.files("harness.orchestrator.prompts").joinpath("sistema.md")
    assert arquivo.is_file()
