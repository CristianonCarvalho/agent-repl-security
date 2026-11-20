"""Testes do protocolo JSON-RPC entre host e sandbox (contrato 2 da spec, D12)."""

import contextlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from harness.sandbox.protocol import (
    Exec,
    ExecDone,
    ProtocolError,
    RespostaFinal,
    ToolCall,
    ToolResult,
    decode,
    encode,
)

# --- Ida e volta das quatro mensagens ---------------------------------------


def test_ida_e_volta_exec() -> None:
    msg = Exec(cell_id=1, code="print(1)", timeout_s=30.0)
    assert decode(encode(msg)) == msg


def test_ida_e_volta_tool_call() -> None:
    msg = ToolCall(call_id="c1", name="catalogo.listar", args={"departamento": "Vendas"})
    assert decode(encode(msg)) == msg


def test_ida_e_volta_tool_result_ok() -> None:
    msg = ToolResult(call_id="c1", ok=True, data={"arquivos": [1, 2, None]})
    assert decode(encode(msg)) == msg


def test_ida_e_volta_tool_result_erro() -> None:
    msg = ToolResult(call_id="c1", ok=False, error="acesso negado")
    assert decode(encode(msg)) == msg


def test_ida_e_volta_exec_done_com_resposta_final() -> None:
    msg = ExecDone(
        cell_id=1,
        stdout="ok\n",
        stderr="",
        exception=None,
        duration_ms=12,
        final=RespostaFinal(answer="Outubro fechou em R$ 10", sources=["pedidos_2026-10.xlsx"]),
    )
    assert decode(encode(msg)) == msg


def test_ida_e_volta_exec_done_sem_resposta_final() -> None:
    msg = ExecDone(
        cell_id=2,
        stdout="",
        stderr="aviso",
        exception="ZeroDivisionError: division by zero",
        duration_ms=0,
        final=None,
    )
    assert decode(encode(msg)) == msg


def test_exec_done_reiniciar_tem_padrao_falso() -> None:
    msg = ExecDone(cell_id=1, stdout="", stderr="", exception=None, duration_ms=0, final=None)
    assert msg.reiniciar is False
    # Mensagem antiga, sem o campo, continua válida e lida como sem reinício.
    antiga = (
        '{"jsonrpc":"2.0","method":"exec_done","cell_id":1,"stdout":"","stderr":"",'
        '"exception":null,"duration_ms":0,"final":null}'
    )
    lida = decode(antiga)
    assert isinstance(lida, ExecDone)
    assert lida.reiniciar is False


def test_ida_e_volta_exec_done_com_reiniciar_verdadeiro() -> None:
    msg = ExecDone(
        cell_id=3,
        stdout="",
        stderr="",
        exception="TimeoutError: ...",
        duration_ms=500,
        final=None,
        reiniciar=True,
    )
    assert decode(encode(msg)) == msg
    assert json.loads(encode(msg))["reiniciar"] is True


@pytest.mark.parametrize("valor", ['"true"', "1", "0", "null"])
def test_rejeita_reiniciar_que_nao_e_booleano(valor: str) -> None:
    linha = (
        '{"jsonrpc":"2.0","method":"exec_done","cell_id":1,"stdout":"","stderr":"",'
        f'"exception":null,"duration_ms":0,"final":null,"reiniciar":{valor}}}'
    )
    with pytest.raises(ProtocolError):
        decode(linha)


def test_exec_done_construido_com_reiniciar_nao_booleano_levanta_erro() -> None:
    with pytest.raises(ValidationError):
        ExecDone(
            cell_id=1,
            stdout="",
            stderr="",
            exception=None,
            duration_ms=0,
            final=None,
            reiniciar="sim",  # type: ignore[arg-type]
        )


# --- Formato da linha --------------------------------------------------------


def test_encode_gera_uma_unica_linha_terminada_em_quebra() -> None:
    linha = encode(Exec(cell_id=1, code="a = 1\nb = 2\n", timeout_s=5.0))
    assert linha.endswith("\n")
    assert linha.count("\n") == 1


def test_encode_escapa_separadores_de_linha_unicode() -> None:
    # str.splitlines() também quebra em U+2028 e U+2029; a linha não pode conter nenhum deles.
    linha = encode(
        ExecDone(cell_id=1, stdout="a b c", stderr="", exception=None, duration_ms=1, final=None)
    )
    assert len(linha.splitlines()) == 1


def test_encode_gera_json_com_jsonrpc_e_method() -> None:
    dados = json.loads(encode(ToolCall(call_id="c1", name="x", args={})))
    assert dados["jsonrpc"] == "2.0"
    assert dados["method"] == "tool_call"


# --- Rejeições ----------------------------------------------------------------


def test_rejeita_json_invalido() -> None:
    with pytest.raises(ProtocolError):
        decode("{não é json")


def test_rejeita_linha_vazia() -> None:
    with pytest.raises(ProtocolError):
        decode("")


def test_rejeita_metodo_desconhecido() -> None:
    linha = '{"jsonrpc":"2.0","method":"qualquer_coisa","cell_id":1}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_llm_call_removido_na_d11() -> None:
    linha = '{"jsonrpc":"2.0","method":"llm_call","call_id":"c1","prompt":"oi"}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_tool_result_ok_com_error() -> None:
    with pytest.raises(ValidationError):
        ToolResult(call_id="c1", ok=True, error="deveria estar ausente")
    linha = '{"jsonrpc":"2.0","method":"tool_result","call_id":"c1","ok":true,"error":"x"}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_tool_result_erro_sem_error() -> None:
    with pytest.raises(ValidationError):
        ToolResult(call_id="c1", ok=False)
    linha = '{"jsonrpc":"2.0","method":"tool_result","call_id":"c1","ok":false}'
    with pytest.raises(ProtocolError):
        decode(linha)


@pytest.mark.parametrize("timeout", [0, -1.5])
def test_rejeita_timeout_nao_positivo(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Exec(cell_id=1, code="", timeout_s=timeout)
    linha = f'{{"jsonrpc":"2.0","method":"exec","cell_id":1,"code":"","timeout_s":{timeout}}}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_duracao_negativa() -> None:
    linha = (
        '{"jsonrpc":"2.0","method":"exec_done","cell_id":1,"stdout":"","stderr":"",'
        '"exception":null,"duration_ms":-1,"final":null}'
    )
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_campo_extra() -> None:
    linha = '{"jsonrpc":"2.0","method":"tool_call","call_id":"c1","name":"x","args":{},"id":9}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_tipo_errado_sem_coercao() -> None:
    # ok como texto "true" não pode virar booleano por coerção.
    linha = '{"jsonrpc":"2.0","method":"tool_result","call_id":"c1","ok":"true","data":1}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_rejeita_quebra_de_linha_interna() -> None:
    linha = '{"jsonrpc":"2.0",\n"method":"tool_call","call_id":"c1","name":"x","args":{}}'
    with pytest.raises(ProtocolError):
        decode(linha)


def test_aceita_terminador_de_linha_no_fim() -> None:
    msg = ToolCall(call_id="c1", name="x", args={})
    assert decode(encode(msg)) == msg


# --- Propriedades (Hypothesis) -----------------------------------------------


@given(st.text())
def test_decode_de_texto_arbitrario_so_levanta_protocol_error(texto: str) -> None:
    with contextlib.suppress(ProtocolError):
        decode(texto)


_valores_json = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text()
)


@given(st.dictionaries(st.text(), _valores_json))
def test_decode_de_objetos_arbitrarios_so_levanta_protocol_error(
    dados: dict[str, object],
) -> None:
    with contextlib.suppress(ProtocolError):
        decode(json.dumps(dados))
