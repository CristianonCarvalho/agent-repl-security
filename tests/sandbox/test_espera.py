import io
import os
import threading
import time

import pytest
from pydantic import ValidationError

from harness.sandbox.protocol import EsperaHumana, ExecDone, ToolResult, decode, encode
from harness.sandbox.runner import Repl
from harness.sandbox.tools_client import Ponte


def test_espera_s_validacao() -> None:
    assert ToolResult(call_id="c1", ok=True, espera_s=1.5).espera_s == 1.5
    for invalido in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            ToolResult(call_id="c1", ok=True, espera_s=invalido)


def test_encode_omite_espera_quando_none_e_inclui_quando_presente() -> None:
    assert "espera_s" not in encode(ToolResult(call_id="c1", ok=True))
    linha = encode(ToolResult(call_id="c1", ok=True, espera_s=2.0))
    assert '"espera_s":2.0' in linha
    assert decode(linha) == ToolResult(call_id="c1", ok=True, espera_s=2.0)


def test_espera_humana_ida_e_volta() -> None:
    msg = EsperaHumana(ativa=True)
    linha = encode(msg)
    assert decode(linha) == msg


def test_ponte_soma_credito_por_espera_humana_e_zera_na_celula_nova() -> None:
    instantes = iter([10.0, 12.5])
    mensagens = (
        encode(EsperaHumana(ativa=True))
        + encode(EsperaHumana(ativa=False))
        + encode(ToolResult(call_id="c1", ok=True, data=1))
    )
    ponte = Ponte(io.StringIO(mensagens), io.StringIO(), relogio=lambda: next(instantes))
    assert ponte.chamar("a.b", {}) == 1
    assert ponte.credito_s == 2.5
    ponte.nova_celula()
    assert ponte.credito_s == 0.0


def _executar_com_pipe(*, enviar_espera: bool) -> ExecDone:
    leitura, escrita = os.pipe()
    entrada = os.fdopen(leitura, "r", encoding="utf-8")
    saida = os.fdopen(escrita, "w", encoding="utf-8")

    def responder() -> None:
        with saida:
            if enviar_espera:
                saida.write(encode(EsperaHumana(ativa=True)))
                saida.flush()
            time.sleep(0.8)
            if enviar_espera:
                saida.write(encode(EsperaHumana(ativa=False)))
            saida.write(encode(ToolResult(call_id="c1", ok=True, data=1)))
            saida.flush()

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()
    try:
        return (
            Repl(Ponte(entrada, io.StringIO()))
            .run_cell(1, "tools.g.n()\nprint('ok')", timeout_s=0.4)
            .done
        )
    finally:
        thread.join(timeout=2.0)
        entrada.close()


def test_espera_humana_em_tempo_real_estende_o_prazo_da_celula() -> None:
    feito = _executar_com_pipe(enviar_espera=True)
    assert feito.exception is None and feito.stdout == "ok\n"


def test_sem_espera_humana_a_mesma_celula_estoura() -> None:
    feito = _executar_com_pipe(enviar_espera=False)
    assert feito.exception is not None and "TimeoutError" in feito.exception
