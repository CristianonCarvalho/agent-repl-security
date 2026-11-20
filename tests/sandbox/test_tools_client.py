"""Testes do objeto `tools` no REPL do sandbox (Tarefa 5; contrato 1 e 2 da spec).

Sem Docker: o host é simulado com StringIO (respostas já escritas na entrada, na ordem em
que a célula as pede) ou com um pipe real, para o teste de timeout com bloqueio de leitura.
"""

import io
import json
import os
import threading
from typing import Any

import pytest

from harness.sandbox.protocol import (
    Exec,
    ExecDone,
    Mensagem,
    ProtocolError,
    ToolCall,
    ToolResult,
    decode,
    encode,
)
from harness.sandbox.runner import Repl, serve
from harness.sandbox.tools_client import Ponte


def _mensagens(texto: str) -> list[Mensagem]:
    return [decode(linha + "\n") for linha in texto.splitlines()]


def _chamadas(texto: str) -> list[ToolCall]:
    return [m for m in _mensagens(texto) if isinstance(m, ToolCall)]


def _ultimo_done(texto: str) -> ExecDone:
    done = [m for m in _mensagens(texto) if isinstance(m, ExecDone)]
    assert done, texto
    return done[-1]


def _rodar(*mensagens: str) -> tuple[ExecDone, list[ToolCall], str]:
    """Roda o serve com as mensagens do host já escritas na entrada e devolve o resultado."""
    saida = io.StringIO()
    serve(io.StringIO("".join(mensagens)), saida)
    texto = saida.getvalue()
    return _ultimo_done(texto), _chamadas(texto), texto


def _exec(cell_id: int, codigo: str) -> str:
    return encode(Exec(cell_id=cell_id, code=codigo, timeout_s=5.0))


def _resultado(call_id: str, data: Any) -> str:
    return encode(ToolResult(call_id=call_id, ok=True, data=data))


def _erro(call_id: str, mensagem: str) -> str:
    return encode(ToolResult(call_id=call_id, ok=False, error=mensagem))


# --- Ida e volta com o host ----------------------------------------------------


def test_chamada_escreve_tool_call_esperado_e_devolve_data() -> None:
    done, chamadas, _ = _rodar(
        _exec(1, "r = tools.catalogo.listar(departamento='vendas')\nprint(r['n'])"),
        _resultado("c1", {"n": 7}),
    )
    assert chamadas == [
        ToolCall(call_id="c1", name="catalogo.listar", args={"departamento": "vendas"})
    ]
    assert done.exception is None
    assert done.stdout == "7\n"


def test_chamada_sem_argumentos_envia_args_vazio() -> None:
    _, chamadas, _ = _rodar(_exec(1, "tools.catalogo.listar()"), _resultado("c1", []))
    assert chamadas == [ToolCall(call_id="c1", name="catalogo.listar", args={})]


def test_ok_falso_levanta_toolerror_com_a_mensagem_do_host() -> None:
    done, _, _ = _rodar(
        _exec(
            1,
            "try:\n    tools.catalogo.listar(departamento='x')\n"
            "except ToolError as erro:\n    print('capturado:', erro)",
        ),
        _erro("c1", "acesso negado: fora do perfil"),
    )
    assert done.stdout == "capturado: acesso negado: fora do perfil\n"


def test_ok_falso_sem_capturar_aparece_como_toolerror_na_excecao() -> None:
    done, _, _ = _rodar(_exec(1, "tools.x.y()"), _erro("c1", "a ferramenta não existe"))
    assert done.exception is not None
    assert "ToolError" in done.exception
    assert "a ferramenta não existe" in done.exception


def test_call_id_e_unico_por_chamada() -> None:
    _, chamadas, _ = _rodar(
        _exec(1, "tools.x.y()\ntools.x.y()"),
        _resultado("c1", 1),
        _resultado("c2", 2),
    )
    ids = [c.call_id for c in chamadas]
    assert len(ids) == 2
    assert len(set(ids)) == 2


# --- Forma da chamada ---------------------------------------------------------------


def test_argumento_posicional_e_recusado_sem_contactar_o_host() -> None:
    done, chamadas, _ = _rodar(
        _exec(
            1,
            "try:\n    tools.catalogo.listar('vendas')\nexcept ToolError as erro:\n    print(erro)",
        )
    )
    assert chamadas == []
    assert "nomeados" in done.stdout


@pytest.mark.parametrize("codigo", ["tools._privado.x()", "tools.grupo._oculto()"])
def test_nome_com_underscore_inicial_e_rejeitado(codigo: str) -> None:
    done, chamadas, _ = _rodar(
        _exec(1, f"try:\n    {codigo}\nexcept AttributeError:\n    print('negado')")
    )
    assert chamadas == []
    assert done.stdout == "negado\n"


def test_nome_sem_grupo_e_recusado_localmente() -> None:
    done, chamadas, _ = _rodar(
        _exec(1, "try:\n    tools.so_um()\nexcept ToolError as erro:\n    print(erro)")
    )
    assert chamadas == []
    assert "tools.<grupo>.<nome>" in done.stdout


@pytest.mark.parametrize("valor", ["object()", "float('nan')", "{1, 2}"])
def test_argumento_que_nao_vira_json_e_recusado_localmente(valor: str) -> None:
    done, chamadas, _ = _rodar(
        _exec(
            1, f"try:\n    tools.x.y(a={valor})\nexcept ToolError as erro:\n    print('recusado')"
        )
    )
    assert chamadas == []
    assert done.stdout == "recusado\n"


# --- Limite de 50 chamadas por célula -----------------------------------------------


def test_51a_chamada_na_mesma_celula_falha_sem_contactar_o_host() -> None:
    respostas = "".join(_resultado(f"c{i}", i) for i in range(1, 51))
    done, chamadas, _ = _rodar(
        _exec(
            1,
            "for _ in range(51):\n    tools.x.y()\n",
        ),
        respostas,
    )
    assert len(chamadas) == 50
    assert done.exception is not None
    assert "ToolError" in done.exception
    assert "50" in done.exception


def test_contador_zera_a_cada_celula() -> None:
    # A célula 1 usa exatamente 50 chamadas; a célula 2 precisa poder fazer mais uma.
    respostas = "".join(_resultado(f"c{i}", i) for i in range(1, 51))
    done, chamadas, _ = _rodar(
        _exec(1, "for _ in range(50):\n    tools.x.y()\n"),
        respostas,
        _exec(2, "print(tools.x.y())"),
        _resultado("c51", "ultima"),
    )
    assert len(chamadas) == 51
    assert done.cell_id == 2
    assert done.exception is None
    assert done.stdout == "ultima\n"


# --- Protocolo: o código do modelo não forja nem corrompe o canal -------------------


def test_saida_do_modelo_com_mensagem_falsa_nao_vira_mensagem_do_protocolo() -> None:
    falso = json.dumps(
        {"jsonrpc": "2.0", "method": "tool_call", "call_id": "falso", "name": "x.y", "args": {}}
    )
    codigo = f"print({falso!r})\ntools.catalogo.listar()"
    done, chamadas, texto = _rodar(_exec(1, codigo), _resultado("c1", []))
    assert [c.call_id for c in chamadas] == ["c1"]
    assert done.stdout == falso + "\n"
    assert all(linha.startswith("{") for linha in texto.splitlines())


def test_resposta_com_call_id_errado_e_erro_de_protocolo() -> None:
    done, _, _ = _rodar(_exec(1, "tools.x.y()"), _resultado("outro", 1))
    assert done.exception is not None
    assert "ProtocolError" in done.exception


def test_exec_recebido_enquanto_aguarda_ferramenta_e_erro_de_protocolo() -> None:
    saida = io.StringIO()
    serve(io.StringIO(_exec(1, "tools.x.y()") + _exec(2, "print('nao')")), saida)
    dones = [m for m in _mensagens(saida.getvalue()) if isinstance(m, ExecDone)]
    assert [d.cell_id for d in dones] == [1]
    assert dones[0].exception is not None
    assert "inesperada" in dones[0].exception


def test_eof_enquanto_aguarda_ferramenta_levanta_erro_claro() -> None:
    done, chamadas, _ = _rodar(_exec(1, "tools.x.y()"))
    assert len(chamadas) == 1
    assert done.exception is not None
    assert "fechou" in done.exception


def test_ponte_com_tool_result_fora_de_formato_levanta_protocolerror() -> None:
    entrada = io.StringIO("linha que não é json\n")
    ponte = Ponte(entrada, io.StringIO())
    with pytest.raises(ProtocolError):
        ponte.chamar("x.y", {})


# --- Sem ponte (testes antigos) -------------------------------------------------------


def test_sem_ponte_tools_levanta_ferramentas_indisponiveis() -> None:
    resultado = Repl().run_cell(1, "tools.x.y()", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "ferramentas indisponíveis" in resultado.done.exception


# --- Timeout com o host calado (D12) --------------------------------------------------


def test_celula_bloqueada_esperando_host_respeita_o_timeout() -> None:
    leitura, escrita = os.pipe()
    saida = io.StringIO()
    entrada = os.fdopen(leitura, "r", encoding="utf-8")
    try:
        resultado = Repl(Ponte(entrada, saida)).run_cell(
            1, "tools.x.y()", timeout_s=0.3, graca_s=0.2
        )
    finally:
        # Fecha a escrita: o readline bloqueado recebe EOF e a thread da célula termina.
        os.close(escrita)
        for thread in threading.enumerate():
            if thread.name == "celula-1":
                thread.join(timeout=2)
        entrada.close()
    assert resultado.precisa_reiniciar is True
    assert resultado.done.exception is not None
    assert "reiniciado" in resultado.done.exception
