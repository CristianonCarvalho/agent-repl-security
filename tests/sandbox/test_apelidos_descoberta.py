"""Apelidos `search_tools` e `describe_tool` no REPL (Tarefa 10, contrato 1 da spec).

Sem Docker: o host é simulado com StringIO, com as respostas `tool_result` já escritas na
entrada, na ordem em que a célula as pede.
"""

import io
from typing import Any

from harness.sandbox.protocol import (
    Exec,
    ExecDone,
    Mensagem,
    ToolCall,
    ToolResult,
    decode,
    encode,
)
from harness.sandbox.runner import Repl
from harness.sandbox.tools_client import Ponte


def _mensagens(texto: str) -> list[Mensagem]:
    return [decode(linha + "\n") for linha in texto.splitlines()]


def _chamadas(texto: str) -> list[ToolCall]:
    return [m for m in _mensagens(texto) if isinstance(m, ToolCall)]


def _resultado(call_id: str, data: Any) -> str:
    return encode(ToolResult(call_id=call_id, ok=True, data=data))


def _erro(call_id: str, mensagem: str) -> str:
    return encode(ToolResult(call_id=call_id, ok=False, error=mensagem))


def _rodar_celula(codigo: str, *respostas: str) -> tuple[ExecDone, list[ToolCall]]:
    """Roda uma célula com o host simulado e devolve a mensagem final e as chamadas feitas."""
    saida = io.StringIO()
    repl = Repl(Ponte(io.StringIO("".join(respostas)), saida))
    resultado = repl.run_cell(1, codigo, timeout_s=5.0)
    return resultado.done, _chamadas(saida.getvalue())


def test_search_tools_chama_descoberta_buscar_com_query_e_limit() -> None:
    done, chamadas = _rodar_celula("print(search_tools('vendas', limit=3))", _resultado("c1", []))
    assert done.exception is None
    assert chamadas == [
        ToolCall(call_id="c1", name="descoberta.buscar", args={"query": "vendas", "limit": 3})
    ]


def test_search_tools_usa_limite_padrao_cinco() -> None:
    _, chamadas = _rodar_celula("search_tools('vendas')", _resultado("c1", []))
    assert chamadas == [
        ToolCall(call_id="c1", name="descoberta.buscar", args={"query": "vendas", "limit": 5})
    ]


def test_search_tools_devolve_a_lista_do_host_ao_codigo() -> None:
    lista = [{"nome": "catalogo.listar", "resumo": "Lista os arquivos."}]
    done, _ = _rodar_celula(
        "achadas = search_tools('listar')\nprint(achadas[0]['nome'])",
        _resultado("c1", lista),
    )
    assert done.exception is None
    assert done.stdout == "catalogo.listar\n"


def test_describe_tool_chama_descoberta_descrever_com_name() -> None:
    done, chamadas = _rodar_celula(
        "print(describe_tool('catalogo.listar')['nome'])",
        _resultado("c1", {"nome": "catalogo.listar"}),
    )
    assert done.exception is None
    assert chamadas == [
        ToolCall(call_id="c1", name="descoberta.descrever", args={"name": "catalogo.listar"})
    ]
    assert done.stdout == "catalogo.listar\n"


def test_erro_do_host_chega_como_toolerror_no_apelido() -> None:
    done, _ = _rodar_celula(
        "describe_tool('nao.existe')",
        _erro("c1", "a ferramenta 'nao.existe' não existe"),
    )
    assert done.exception is not None
    assert "ToolError" in done.exception
    assert "não existe" in done.exception


def test_apelidos_sem_ponte_levantam_ferramentas_indisponiveis() -> None:
    for codigo in ["search_tools('x')", "describe_tool('catalogo.listar')"]:
        resultado = Repl().run_cell(1, codigo, timeout_s=2.0)
        assert resultado.done.exception is not None, codigo
        assert "ferramentas indisponíveis" in resultado.done.exception, codigo


def test_apelidos_existem_em_repl_novo_e_funcionam() -> None:
    # Um reinício cria um Repl novo: os apelidos precisam estar no namespace dele também.
    saida = io.StringIO()
    repl = Repl(Ponte(io.StringIO(_resultado("c1", [])), saida))
    resultado = repl.run_cell(
        1,
        "print(callable(search_tools), callable(describe_tool))",
        timeout_s=5.0,
    )
    assert resultado.done.stdout == "True True\n"


def test_apelidos_sobrevivem_a_reatribuicao_de_tools_entre_celulas() -> None:
    saida = io.StringIO()
    respostas = _resultado("c1", []) + _resultado("c2", [])
    repl = Repl(Ponte(io.StringIO(respostas), saida))
    repl.run_cell(1, "tools = None", timeout_s=5.0)
    resultado = repl.run_cell(2, "search_tools('vendas')", timeout_s=5.0)
    assert resultado.done.exception is None
    assert [c.name for c in _chamadas(saida.getvalue())] == ["descoberta.buscar"]


def test_apelidos_persistem_entre_celulas_como_tools() -> None:
    saida = io.StringIO()
    respostas = _resultado("c1", []) + _resultado("c2", [])
    repl = Repl(Ponte(io.StringIO(respostas), saida))
    repl.run_cell(1, "x = 1", timeout_s=5.0)
    resultado = repl.run_cell(2, "describe_tool('catalogo.listar')", timeout_s=5.0)
    assert resultado.done.exception is None
    assert [c.name for c in _chamadas(saida.getvalue())] == ["descoberta.descrever"]


def test_apelido_conta_no_limite_de_chamadas_da_celula() -> None:
    respostas = "".join(_resultado(f"c{i}", []) for i in range(1, 51))
    done, chamadas = _rodar_celula(
        "for _ in range(51):\n    search_tools('x')\n",
        respostas,
    )
    assert len(chamadas) == 50
    assert done.exception is not None
    assert "ToolError" in done.exception


def test_exec_de_apelido_pelo_serve_completo() -> None:
    # Ida e volta pelo protocolo, como o sandbox recebe do host (serve com Exec).
    from harness.sandbox.runner import serve

    entrada = io.StringIO(
        encode(Exec(cell_id=1, code="print(len(search_tools('x', limit=1)))", timeout_s=5.0))
        + _resultado("c1", [{"nome": "a.b", "resumo": "r"}])
    )
    saida = io.StringIO()
    serve(entrada, saida)
    dones = [m for m in _mensagens(saida.getvalue()) if isinstance(m, ExecDone)]
    assert dones[0].stdout == "1\n"
