"""Testes das mensagens `contexto` e `contexto_ok` do protocolo (carga do contexto no REPL)."""

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from harness.sandbox.protocol import Contexto, ContextoOk, ProtocolError, decode, encode


@pytest.mark.parametrize("valor", ["texto do pedido", {"a": [1, "b"]}, [1, {"c": None}], ""])
def test_ida_e_volta_contexto(valor: Any) -> None:
    msg = Contexto(valor=valor)
    assert decode(encode(msg)) == msg


def test_ida_e_volta_contexto_ok() -> None:
    msg = ContextoOk(tipo="objeto", tamanho=3)
    assert decode(encode(msg)) == msg


def test_contexto_ok_ida_e_volta_com_tamanho_zero() -> None:
    msg = ContextoOk(tipo="lista", tamanho=0)
    assert decode(encode(msg)) == msg


def test_contexto_codifica_com_escapes_ascii_e_sem_quebra_de_linha_extra() -> None:
    linha = encode(Contexto(valor="a b\nc"))
    assert " " not in linha
    assert linha.endswith("\n")
    assert linha.count("\n") == 1
    assert decode(linha) == Contexto(valor="a b\nc")


@pytest.mark.parametrize("valor", [1, 1.5, True, False, None])
def test_contexto_recusa_numero_booleano_e_null(valor: object) -> None:
    with pytest.raises(ValidationError):
        Contexto(valor=valor)


@given(
    st.one_of(
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.booleans(),
        st.none(),
    )
)
def test_qualquer_escalar_json_nao_string_e_recusado_como_contexto(valor: Any) -> None:
    with pytest.raises(ValidationError):
        Contexto(valor=valor)


def test_decode_recusa_contexto_com_numero() -> None:
    with pytest.raises(ProtocolError):
        decode('{"jsonrpc":"2.0","method":"contexto","valor":5}\n')


def test_decode_recusa_contexto_sem_valor() -> None:
    with pytest.raises(ProtocolError):
        decode('{"jsonrpc":"2.0","method":"contexto"}\n')


def test_decode_recusa_campo_extra_em_contexto_ok() -> None:
    with pytest.raises(ProtocolError):
        decode('{"jsonrpc":"2.0","method":"contexto_ok","tipo":"texto","tamanho":1,"x":1}\n')


@pytest.mark.parametrize("tamanho", [-1, True, "3", 1.0])
def test_contexto_ok_recusa_tamanho_invalido(tamanho: Any) -> None:
    with pytest.raises(ValidationError):
        ContextoOk(tipo="texto", tamanho=tamanho)


def test_contexto_ok_recusa_tipo_que_nao_e_texto() -> None:
    dados: dict[str, Any] = {"tipo": 5, "tamanho": 1}
    with pytest.raises(ValidationError):
        ContextoOk.model_validate(dados)
