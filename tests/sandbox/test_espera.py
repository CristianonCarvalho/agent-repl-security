import io

import pytest
from pydantic import ValidationError

from harness.sandbox.protocol import ToolResult, decode, encode
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


def test_ponte_soma_o_credito_e_zera_na_celula_nova() -> None:
    resposta = encode(ToolResult(call_id="c1", ok=True, data=1, espera_s=2.5))
    ponte = Ponte(io.StringIO(resposta), io.StringIO())
    assert ponte.chamar("a.b", {}) == 1
    assert ponte.credito_s == 2.5
    ponte.nova_celula()
    assert ponte.credito_s == 0.0


CELULA = "tools.g.n()\nimport time\ntime.sleep(0.6)\nprint('ok')"


def _repl(espera: float | None) -> Repl:
    resposta = encode(ToolResult(call_id="c1", ok=True, data=1, espera_s=espera))
    return Repl(Ponte(io.StringIO(resposta), io.StringIO()))


def test_credito_estende_o_prazo_da_celula() -> None:
    feito = _repl(1.0).run_cell(1, CELULA, timeout_s=0.4).done
    assert feito.exception is None and feito.stdout == "ok\n"


def test_sem_credito_a_mesma_celula_estoura() -> None:
    feito = _repl(None).run_cell(1, CELULA, timeout_s=0.4).done
    assert feito.exception is not None and "TimeoutError" in feito.exception
