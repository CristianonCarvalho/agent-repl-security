"""Testes do contexto dentro do runner: `context`, `context_info` e a mensagem `contexto`.

Os testes rodam o código do modelo no mesmo processo, como test_runner.py, sem Docker.
"""

import io
import json
from typing import Any

import pytest

from harness.sandbox.protocol import (
    Contexto,
    ContextoOk,
    Exec,
    ExecDone,
    ProtocolError,
    decode,
    encode,
)
from harness.sandbox.runner import Repl, serve


@pytest.fixture
def repl() -> Repl:
    return Repl()


def _info(repl: Repl, cell_id: int) -> Any:
    """Chama `context_info()` numa célula e lê o resultado como JSON."""
    codigo = "import json\nprint(json.dumps(context_info(), ensure_ascii=False))"
    resultado = repl.run_cell(cell_id, codigo, timeout_s=2.0)
    assert resultado.done.exception is None, resultado.done.exception
    return json.loads(resultado.done.stdout)


# --- context_info por tipo ----------------------------------------------------


def test_context_info_de_texto(repl: Repl) -> None:
    repl.carregar_contexto("abc" * 100)
    assert _info(repl, 1) == {"tipo": "texto", "tamanho": 300, "previa": ("abc" * 100)[:200]}


def test_context_info_de_objeto(repl: Repl) -> None:
    repl.carregar_contexto({"a": "x", "b": [1], "c": 2.5})
    assert _info(repl, 1) == {
        "tipo": "objeto",
        "tamanho": 3,
        "previa": "a: texto, b: lista, c: numero",
    }


def test_context_info_de_lista(repl: Repl) -> None:
    repl.carregar_contexto([{"id": 1}, 2])
    assert _info(repl, 1) == {
        "tipo": "lista",
        "tamanho": 2,
        "previa": 'primeiro item: objeto; {"id": 1}',
    }


@pytest.mark.parametrize(
    ("valor", "tipo"),
    [("", "texto"), ({}, "objeto"), ([], "lista")],
)
def test_context_info_de_valor_vazio(repl: Repl, valor: Any, tipo: str) -> None:
    repl.carregar_contexto(valor)
    assert _info(repl, 1) == {"tipo": tipo, "tamanho": 0, "previa": ""}


def test_context_info_considera_so_as_vinte_primeiras_chaves(repl: Repl) -> None:
    repl.carregar_contexto({f"c{i:02d}": i for i in range(30)})
    info = _info(repl, 1)
    assert info["tamanho"] == 30
    assert "c20" not in info["previa"]


# --- cópia do contexto original -----------------------------------------------


def test_reatribuir_context_nao_muda_context_info(repl: Repl) -> None:
    repl.carregar_contexto("original")
    resultado = repl.run_cell(1, "context = 'outro, bem maior que o original'", timeout_s=2.0)
    assert resultado.done.exception is None
    assert _info(repl, 2) == {"tipo": "texto", "tamanho": 8, "previa": "original"}


def test_alterar_context_no_lugar_nao_muda_context_info(repl: Repl) -> None:
    repl.carregar_contexto([1])
    repl.run_cell(1, "context.append(9)\ncontext.append(9)", timeout_s=2.0)
    assert _info(repl, 2)["tamanho"] == 1


# --- persistência, substituição e ausência -----------------------------------


def test_context_persiste_entre_celulas(repl: Repl) -> None:
    repl.carregar_contexto("dados")
    repl.run_cell(1, "x = 1", timeout_s=2.0)
    resultado = repl.run_cell(2, "print(context, x)", timeout_s=2.0)
    assert resultado.done.stdout == "dados 1\n"


def test_segundo_carregamento_substitui_o_primeiro(repl: Repl) -> None:
    repl.carregar_contexto("primeiro")
    confirmacao = repl.carregar_contexto([1, 2, 3])
    assert confirmacao == ContextoOk(tipo="lista", tamanho=3)
    resultado = repl.run_cell(1, "print(context, context_info()['tipo'])", timeout_s=2.0)
    assert resultado.done.stdout == "[1, 2, 3] lista\n"


def test_carregar_devolve_tipo_e_tamanho(repl: Repl) -> None:
    assert repl.carregar_contexto("abcd") == ContextoOk(tipo="texto", tamanho=4)
    assert repl.carregar_contexto({"a": 1}) == ContextoOk(tipo="objeto", tamanho=1)


def test_sem_contexto_context_info_levanta_erro_legivel(repl: Repl) -> None:
    resultado = repl.run_cell(1, "context_info()", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "ValueError" in resultado.done.exception
    assert "não há contexto carregado" in resultado.done.exception


def test_sem_contexto_context_nao_existe(repl: Repl) -> None:
    resultado = repl.run_cell(1, "print(context)", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "NameError" in resultado.done.exception


# --- mensagem `contexto` no serve ---------------------------------------------


def test_serve_responde_contexto_ok_com_tipo_e_tamanho() -> None:
    entrada = io.StringIO(encode(Contexto(valor="dados")))
    saida = io.StringIO()
    serve(entrada, saida)
    assert decode(saida.getvalue()) == ContextoOk(tipo="texto", tamanho=5)


def test_serve_processa_contexto_antes_do_exec_seguinte_e_nao_antes_do_anterior() -> None:
    entrada = io.StringIO(
        encode(Exec(cell_id=1, code="print(context)", timeout_s=2.0))
        + encode(Contexto(valor="pedido"))
        + encode(Exec(cell_id=2, code="print(context)", timeout_s=2.0))
    )
    saida = io.StringIO()
    serve(entrada, saida)
    primeira, confirmacao, segunda = [
        decode(linha + "\n") for linha in saida.getvalue().splitlines()
    ]
    assert isinstance(primeira, ExecDone)
    assert primeira.exception is not None and "NameError" in primeira.exception
    assert confirmacao == ContextoOk(tipo="texto", tamanho=6)
    assert isinstance(segunda, ExecDone)
    assert segunda.stdout == "pedido\n"


def test_serve_segundo_contexto_substitui_o_primeiro() -> None:
    entrada = io.StringIO(
        encode(Contexto(valor="um"))
        + encode(Contexto(valor={"b": 1, "c": 2}))
        + encode(Exec(cell_id=1, code="print(context_info()['tipo'])", timeout_s=2.0))
    )
    saida = io.StringIO()
    serve(entrada, saida)
    linhas = saida.getvalue().splitlines()
    assert decode(linhas[1] + "\n") == ContextoOk(tipo="objeto", tamanho=2)
    resultado = decode(linhas[2] + "\n")
    assert isinstance(resultado, ExecDone)
    assert resultado.stdout == "objeto\n"


def test_serve_recusa_contexto_com_valor_que_nao_e_json_de_contexto() -> None:
    entrada = io.StringIO('{"jsonrpc":"2.0","method":"contexto","valor":3}\n')
    with pytest.raises(ProtocolError):
        serve(entrada, io.StringIO())
