"""Testes da verificação estática do código gerado (Tarefa 7, spec v0 passo 5).

A verificação é uma camada extra, NÃO uma fronteira de segurança: o isolamento é o contêiner
(D4). Por isso há testes que registram desvios conhecidos que ela não pega.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from harness.orchestrator.estatico import verificar

FERRAMENTAS = {"catalogo.listar", "arquivos.historico"}


def test_codigo_limpo_e_aprovado() -> None:
    assert verificar("import json\nprint(json.dumps({'a': 1}))", FERRAMENTAS) == []


def test_ferramenta_existente_e_aprovada() -> None:
    assert verificar("x = tools.catalogo.listar(departamento='vendas')", FERRAMENTAS) == []


@pytest.mark.parametrize(
    "codigo",
    [
        "import subprocess",
        "import socket as s",
        "from ctypes import CDLL",
        "import multiprocessing.pool",
        "from importlib.util import find_spec",
        "import os, pty",
        "import builtins",
        "import inspect",
    ],
)
def test_import_proibido_e_reprovado_com_a_linha(codigo: str) -> None:
    problemas = verificar(codigo, FERRAMENTAS)
    assert len(problemas) == 1
    assert problemas[0].startswith("linha 1:")
    assert "import proibido" in problemas[0]


@pytest.mark.parametrize(
    "codigo",
    ["__import__('os')", "eval('1')", "exec('x = 1')", "compile('x', 'f', 'exec')"],
)
def test_chamada_proibida_e_reprovada(codigo: str) -> None:
    problemas = verificar(codigo, FERRAMENTAS)
    assert len(problemas) == 1
    assert "chamada proibida" in problemas[0]


def test_eval_como_metodo_de_outro_objeto_nao_e_reprovado() -> None:
    # df.eval(...) do pandas é legítimo; só a chamada pelo nome nu é proibida.
    assert verificar("resultado = tabela.eval('a + b')", FERRAMENTAS) == []


def test_ferramenta_desconhecida_e_reprovada() -> None:
    problemas = verificar("tools.catalogo.apagar(arquivo='x')", FERRAMENTAS)
    assert len(problemas) == 1
    assert "catalogo.apagar" in problemas[0]
    assert "desconhecida" in problemas[0]


def test_chamada_dinamica_nao_e_verificada() -> None:
    # Nome montado em tempo de execução não tem como ser conferido estaticamente: tudo bem.
    assert verificar("getattr(tools.catalogo, 'listar')()", FERRAMENTAS) == []


def test_erro_de_sintaxe_informa_a_linha() -> None:
    problemas = verificar("x = 1\nif x\n  pass", FERRAMENTAS)
    assert len(problemas) == 1
    assert problemas[0].startswith("linha 2:")
    assert "sintaxe" in problemas[0]


def test_codigo_acima_do_limite_e_reprovado_sem_analisar() -> None:
    problemas = verificar("x = 1", FERRAMENTAS, limite_caracteres=3)
    assert len(problemas) == 1
    assert "limite de 3" in problemas[0]


def test_limite_padrao_e_de_oito_mil_caracteres() -> None:
    assert verificar("x=1\n" * 2000, FERRAMENTAS) == []
    assert len(verificar("x=1\n" * 2001, FERRAMENTAS)) == 1


def test_varias_violacoes_saem_ordenadas_por_linha() -> None:
    codigo = "import socket\nx = eval('1')\nimport pty"
    problemas = verificar(codigo, FERRAMENTAS)
    assert [p.split(":")[0] for p in problemas] == ["linha 1", "linha 2", "linha 3"]


# --- Desvios conhecidos: a verificação NÃO pega estes casos (esperado, D4) -----------------
# O teste fixa o comportamento para que ninguém ache que a verificação é uma barreira. A
# barreira real é o contêiner: sem rede, sem privilégios, sistema de arquivos somente leitura.


@pytest.mark.parametrize(
    "codigo",
    [
        # Acesso a builtins por atributo, sem o nome `eval` ou `__import__` no código.
        "getattr(__builtins__, 'ev' + 'al')('1')",
        # Acesso a atributo privado da ponte, sem passar por `tools.grupo.nome(...)`.
        "object.__getattribute__(tools, '_ponte')",
        # `os` não está na lista de imports proibidos (o código de teste usa os.listdir).
        "import os\nos.system('id')",
    ],
)
def test_desvio_conhecido_nao_e_detectado_pela_verificacao(codigo: str) -> None:
    # Comentário: este resultado é ESPERADO. Se um dia a verificação passar a pegar estes casos,
    # o teste falha e deve ser atualizado com a nova regra, não removido.
    assert verificar(codigo, FERRAMENTAS) == []


# --- Propriedades (Hypothesis) -------------------------------------------------------------

_FRAGMENTOS = st.sampled_from(
    [
        "import ",
        "from ",
        "tools.",
        "catalogo",
        ".listar(",
        "eval(",
        "exec(",
        "__import__(",
        "subprocess",
        "(",
        ")",
        "\n",
        " ",
        "x",
        "=",
        "'",
        '"',
        ":",
        ".",
    ]
)


@given(st.text(), st.integers(min_value=-5, max_value=20000))
def test_verificar_nunca_levanta_para_texto_arbitrario(codigo: str, limite: int) -> None:
    resultado = verificar(codigo, FERRAMENTAS, limite_caracteres=limite)
    assert isinstance(resultado, list)
    assert all(isinstance(problema, str) for problema in resultado)


@given(st.lists(_FRAGMENTOS, max_size=40).map("".join))
def test_verificar_nunca_levanta_para_codigo_quase_python(codigo: str) -> None:
    resultado = verificar(codigo, FERRAMENTAS)
    assert isinstance(resultado, list)
