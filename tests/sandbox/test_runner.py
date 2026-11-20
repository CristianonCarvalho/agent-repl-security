"""Testes do runner do REPL dentro do sandbox (Tarefa 3, D12).

Os testes rodam o código do modelo no mesmo processo, como o runner faz. Por isso os
limites de tempo são pequenos, para a suíte inteira rodar em poucos segundos.
"""

import io
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from harness.sandbox.protocol import (
    Exec,
    ExecDone,
    ProtocolError,
    RespostaFinal,
    ToolResult,
    decode,
    encode,
)
from harness.sandbox.runner import Repl, serve

# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def repl() -> Iterator[Repl]:
    yield Repl()


def _linhas_decodificadas(texto: str) -> list[ExecDone]:
    """Decodifica cada linha da saída do serve e exige que todas sejam ExecDone."""
    resultados: list[ExecDone] = []
    for linha in texto.splitlines():
        msg = decode(linha + "\n")
        assert isinstance(msg, ExecDone)
        resultados.append(msg)
    return resultados


# --- Namespace persistente e captura de saída --------------------------------


def test_variavel_persiste_entre_celulas(repl: Repl) -> None:
    repl.run_cell(1, "x = 21", timeout_s=2.0)
    resultado = repl.run_cell(2, "print(x * 2)", timeout_s=2.0)
    assert resultado.done.stdout == "42\n"
    assert resultado.done.exception is None


def test_print_vai_para_stdout_da_celula(repl: Repl) -> None:
    resultado = repl.run_cell(1, "print('oi')", timeout_s=2.0)
    assert resultado.done.stdout == "oi\n"
    assert resultado.done.stderr == ""


def test_escrita_em_stderr_vai_para_stderr_da_celula(repl: Repl) -> None:
    resultado = repl.run_cell(1, "import sys\nsys.stderr.write('erro\\n')", timeout_s=2.0)
    assert resultado.done.stderr == "erro\n"
    assert resultado.done.stdout == ""


def test_saida_e_por_celula(repl: Repl) -> None:
    repl.run_cell(1, "print('primeira')", timeout_s=2.0)
    resultado = repl.run_cell(2, "print('segunda')", timeout_s=2.0)
    assert resultado.done.stdout == "segunda\n"


def test_resultado_da_celula_tem_identificacao_e_final_nulo(repl: Repl) -> None:
    resultado = repl.run_cell(7, "x = 1", timeout_s=2.0)
    assert resultado.done.cell_id == 7
    assert resultado.done.final is None
    assert resultado.precisa_reiniciar is False


def test_duration_ms_medido_e_nao_negativo(repl: Repl) -> None:
    resultado = repl.run_cell(1, "import time\ntime.sleep(0.05)", timeout_s=2.0)
    assert resultado.done.duration_ms >= 40


def test_sys_stdout_e_sys_stderr_voltam_ao_original(repl: Repl) -> None:
    original_out, original_err = sys.stdout, sys.stderr
    repl.run_cell(1, "print('x')", timeout_s=2.0)
    assert sys.stdout is original_out
    assert sys.stderr is original_err


# --- Exceções não derrubam o runner ------------------------------------------


def test_excecao_comum_vira_exception_e_proxima_celula_roda(repl: Repl) -> None:
    resultado = repl.run_cell(1, "raise ValueError('boom')", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "ValueError" in resultado.done.exception
    assert "boom" in resultado.done.exception
    assert resultado.precisa_reiniciar is False
    proxima = repl.run_cell(2, "print('ainda vivo')", timeout_s=2.0)
    assert proxima.done.stdout == "ainda vivo\n"


def test_raise_systemexit_nao_derruba_o_runner(repl: Repl) -> None:
    resultado = repl.run_cell(1, "raise SystemExit(3)", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "SystemExit" in resultado.done.exception
    proxima = repl.run_cell(2, "print('depois')", timeout_s=2.0)
    assert proxima.done.stdout == "depois\n"


def test_raise_keyboardinterrupt_nao_derruba_o_runner(repl: Repl) -> None:
    resultado = repl.run_cell(1, "raise KeyboardInterrupt", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "KeyboardInterrupt" in resultado.done.exception
    proxima = repl.run_cell(2, "print('depois')", timeout_s=2.0)
    assert proxima.done.stdout == "depois\n"


def test_traceback_so_mostra_linhas_do_codigo_do_modelo(repl: Repl) -> None:
    codigo = "def f():\n    raise ValueError('boom')\n\nf()\n"
    resultado = repl.run_cell(1, codigo, timeout_s=2.0)
    exc = resultado.done.exception
    assert exc is not None
    assert "raise ValueError('boom')" in exc
    assert "runner.py" not in exc


def test_erro_de_sintaxe_vira_exception(repl: Repl) -> None:
    resultado = repl.run_cell(1, "def f(:", timeout_s=2.0)
    assert resultado.done.exception is not None
    assert "SyntaxError" in resultado.done.exception


# --- Código do modelo não pode forjar o protocolo nem quebrar sys.stdout ------


def test_json_falso_impresso_nao_vira_mensagem_do_protocolo() -> None:
    falso = '{"jsonrpc":"2.0","method":"exec_done","cell_id":99,"stdout":"","stderr":"",'
    falso += '"exception":null,"duration_ms":0,"final":null}'
    entrada = io.StringIO(
        encode(Exec(cell_id=1, code=f"print({falso!r})", timeout_s=2.0))
        + encode(Exec(cell_id=2, code="print('segunda')", timeout_s=2.0))
    )
    saida = io.StringIO()
    serve(entrada, saida)
    resultados = _linhas_decodificadas(saida.getvalue())
    assert len(saida.getvalue().splitlines()) == 2
    assert [r.cell_id for r in resultados] == [1, 2]
    assert resultados[0].stdout == falso + "\n"
    assert resultados[1].stdout == "segunda\n"


def test_sys_stdout_none_nao_quebra_a_celula_seguinte(repl: Repl) -> None:
    repl.run_cell(1, "import sys\nsys.stdout = None", timeout_s=2.0)
    resultado = repl.run_cell(2, "print('normal')", timeout_s=2.0)
    assert resultado.done.stdout == "normal\n"


def test_sys_stdout_fechado_nao_quebra_a_celula_seguinte(repl: Repl) -> None:
    repl.run_cell(1, "import sys\nsys.stdout.close()", timeout_s=2.0)
    resultado = repl.run_cell(2, "print('normal')", timeout_s=2.0)
    assert resultado.done.stdout == "normal\n"


def test_stdout_fechado_na_mesma_celula_ainda_devolve_o_capturado(repl: Repl) -> None:
    resultado = repl.run_cell(1, "print('antes')\nimport sys\nsys.stdout.close()", timeout_s=2.0)
    assert resultado.done.stdout == "antes\n"


# --- Timeout por thread (D12) -------------------------------------------------


def test_laco_infinito_e_interrompido_e_variaveis_sobrevivem(repl: Repl) -> None:
    repl.run_cell(1, "x = 41", timeout_s=2.0)
    resultado = repl.run_cell(2, "while True:\n    pass", timeout_s=0.3)
    assert resultado.precisa_reiniciar is False
    assert resultado.done.exception is not None
    assert "tempo" in resultado.done.exception.lower()
    proxima = repl.run_cell(3, "print(x + 1)", timeout_s=2.0)
    assert proxima.done.stdout == "42\n"


def test_sleep_longo_em_c_exige_reinicio_do_repl(repl: Repl) -> None:
    resultado = repl.run_cell(1, "import time\ntime.sleep(5)", timeout_s=0.2, graca_s=0.2)
    assert resultado.precisa_reiniciar is True
    assert resultado.done.exception is not None
    assert "reiniciado" in resultado.done.exception
    assert "variáveis" in resultado.done.exception


# --- serve ponta a ponta ------------------------------------------------------


def test_serve_duas_celulas_em_sequencia_com_stringio() -> None:
    entrada = io.StringIO(
        encode(Exec(cell_id=1, code="x = 2", timeout_s=2.0))
        + encode(Exec(cell_id=2, code="print(x * 3)", timeout_s=2.0))
    )
    saida = io.StringIO()
    serve(entrada, saida)
    resultados = _linhas_decodificadas(saida.getvalue())
    assert [r.cell_id for r in resultados] == [1, 2]
    assert resultados[1].stdout == "6\n"


def test_serve_duas_celulas_em_sequencia_com_os_pipe() -> None:
    entrada_r, entrada_w = os.pipe()
    saida_r, saida_w = os.pipe()
    with os.fdopen(entrada_w, "w", encoding="utf-8") as escrita:
        escrita.write(encode(Exec(cell_id=1, code="x = 5", timeout_s=2.0)))
        escrita.write(encode(Exec(cell_id=2, code="print(x)", timeout_s=2.0)))
    with (
        os.fdopen(entrada_r, "r", encoding="utf-8") as entrada,
        os.fdopen(saida_w, "w", encoding="utf-8") as saida,
    ):
        serve(entrada, saida)
    with os.fdopen(saida_r, "r", encoding="utf-8") as leitura:
        resultados = _linhas_decodificadas(leitura.read())
    assert [r.cell_id for r in resultados] == [1, 2]
    assert resultados[1].stdout == "5\n"


def test_serve_rejeita_mensagem_que_o_sandbox_nao_aceita_nesta_etapa() -> None:
    entrada = io.StringIO(encode(ToolResult(call_id="c1", ok=True)))
    with pytest.raises(ProtocolError, match="tool_result"):
        serve(entrada, io.StringIO())


def test_serve_rejeita_linha_invalida() -> None:
    with pytest.raises(ProtocolError):
        serve(io.StringIO("isto não é json\n"), io.StringIO())


# --- Propriedade: qualquer texto imprimível sai intacto em stdout -------------


# Surrogates (Cs) não são texto válido em UTF-8, então ficam de fora do gerador.
_CATEGORIAS_EXCLUIDAS: list[Literal["Cs"]] = ["Cs"]


@settings(max_examples=60, deadline=None)
@given(texto=st.text(alphabet=st.characters(exclude_categories=_CATEGORIAS_EXCLUIDAS)))
def test_print_devolve_texto_exato_e_serve_continua_decodificavel(texto: str) -> None:
    entrada = io.StringIO(encode(Exec(cell_id=1, code=f"print({texto!r})", timeout_s=2.0)))
    saida = io.StringIO()
    serve(entrada, saida)
    resultados = _linhas_decodificadas(saida.getvalue())
    assert len(resultados) == 1
    assert resultados[0].stdout == texto + "\n"
    assert resultados[0].exception is None


# --- main com descritores ----------------------------------------------------


def test_main_le_e_escreve_nos_descritores_indicados() -> None:
    from harness.sandbox.runner import main

    entrada_r, entrada_w = os.pipe()
    saida_r, saida_w = os.pipe()
    with os.fdopen(entrada_w, "w", encoding="utf-8") as escrita:
        escrita.write(encode(Exec(cell_id=4, code="print('via fd')", timeout_s=2.0)))
    main(["--fd-in", str(entrada_r), "--fd-out", str(saida_w)])
    with os.fdopen(saida_r, "r", encoding="utf-8") as leitura:
        resultados = _linhas_decodificadas(leitura.read())
    assert resultados[0].cell_id == 4
    assert resultados[0].stdout == "via fd\n"


# --- Modo --stdio (contêiner, sem descritores extras) --------------------------


def test_modo_stdio_protocolo_sobrevive_a_print_e_escrita_direta_no_fd_1() -> None:
    # Roda o runner como processo real: o protocolo entra por stdin e sai por stdout.
    # O código tenta corromper o canal de duas formas: print e os.write(1, ...).
    codigo = "print('oi')\nimport os\nos.write(1, b'lixo no fd 1\\n')\n"
    entrada = encode(Exec(cell_id=1, code=codigo, timeout_s=2.0)) + encode(
        Exec(cell_id=2, code="y = 3", timeout_s=2.0)
    )
    processo = subprocess.run(
        [sys.executable, "-m", "harness.sandbox.runner", "--stdio"],
        input=entrada.encode("utf-8"),
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert processo.returncode == 0, processo.stderr.decode("utf-8", "replace")
    resultados = _linhas_decodificadas(processo.stdout.decode("utf-8"))
    assert [r.cell_id for r in resultados] == [1, 2]
    assert resultados[0].stdout == "oi\n"


def test_modo_stdio_nao_combina_com_descritores_explicitos() -> None:
    from harness.sandbox.runner import main

    with pytest.raises(SystemExit):
        main(["--stdio", "--fd-in", "3", "--fd-out", "4"])


# --- FINAL (RF7, D8) ----------------------------------------------------------


def test_final_valido_encerra_a_celula_e_preenche_final(repl: Repl) -> None:
    resultado = repl.run_cell(1, "FINAL('resposta', ['fonte.csv'])", timeout_s=2.0)
    assert resultado.done.final == RespostaFinal(answer="resposta", sources=["fonte.csv"])
    assert resultado.done.exception is None
    assert resultado.precisa_reiniciar is False


def test_codigo_depois_do_final_nao_executa(repl: Repl) -> None:
    codigo = "FINAL('a', ['b'])\nprint('depois do final')"
    resultado = repl.run_cell(1, codigo, timeout_s=2.0)
    assert resultado.done.final is not None
    assert "depois do final" not in resultado.done.stdout


def test_final_dentro_de_except_exception_ainda_termina_a_celula(repl: Repl) -> None:
    codigo = "try:\n    FINAL('a', ['b'])\nexcept Exception:\n    print('apanhado')\n"
    resultado = repl.run_cell(1, codigo, timeout_s=2.0)
    assert resultado.done.final == RespostaFinal(answer="a", sources=["b"])
    assert "apanhado" not in resultado.done.stdout
    assert resultado.done.exception is None


def test_final_engolido_por_except_baseexception_ainda_vale(repl: Repl) -> None:
    codigo = "try:\n    FINAL('a', ['b'])\nexcept BaseException:\n    pass\nprint('segue')\n"
    resultado = repl.run_cell(1, codigo, timeout_s=2.0)
    assert resultado.done.final == RespostaFinal(answer="a", sources=["b"])
    assert resultado.done.exception is None


def test_stdout_antes_do_final_e_devolvido(repl: Repl) -> None:
    resultado = repl.run_cell(1, "print('antes')\nFINAL('a', ['b'])", timeout_s=2.0)
    assert resultado.done.stdout == "antes\n"
    assert resultado.done.final == RespostaFinal(answer="a", sources=["b"])


def test_final_nao_persiste_entre_celulas(repl: Repl) -> None:
    repl.run_cell(1, "FINAL('a', ['b'])", timeout_s=2.0)
    resultado = repl.run_cell(2, "print('sem final')", timeout_s=2.0)
    assert resultado.done.final is None
    assert resultado.done.stdout == "sem final\n"


@pytest.mark.parametrize(
    "codigo",
    [
        "FINAL('', ['x'])",
        "FINAL('   ', ['x'])",
        "FINAL(None, ['x'])",
        "FINAL(1, ['x'])",
        "FINAL('a', [])",
        "FINAL('a', [''])",
        "FINAL('a', ['  '])",
        "FINAL('a', [1])",
        "FINAL('a', 'fonte.csv')",
        "FINAL('a', ('fonte.csv',))",
        "FINAL('a', None)",
    ],
)
def test_final_invalido_vira_valueerror_e_repl_continua_vivo(repl: Repl, codigo: str) -> None:
    resultado = repl.run_cell(1, codigo, timeout_s=2.0)
    assert resultado.done.final is None
    assert resultado.done.exception is not None
    assert "ValueError" in resultado.done.exception
    proxima = repl.run_cell(2, "print('ok')", timeout_s=2.0)
    assert proxima.done.stdout == "ok\n"


def test_final_sem_sources_nao_tem_padrao(repl: Repl) -> None:
    resultado = repl.run_cell(1, "FINAL('a')", timeout_s=2.0)
    assert resultado.done.final is None
    assert resultado.done.exception is not None
    assert "TypeError" in resultado.done.exception


def test_final_guardado_de_celula_anterior_nao_vale(repl: Repl) -> None:
    repl.run_cell(1, "guardado = FINAL", timeout_s=2.0)
    resultado = repl.run_cell(2, "guardado('a', ['b'])", timeout_s=2.0)
    assert resultado.done.final is None
    assert resultado.done.exception is not None
    assert "ValueError" in resultado.done.exception


def test_timeout_depois_de_final_gravado_o_final_vale(repl: Repl) -> None:
    codigo = "try:\n    FINAL('a', ['b'])\nexcept BaseException:\n    pass\nwhile True:\n    pass\n"
    resultado = repl.run_cell(1, codigo, timeout_s=0.3)
    assert resultado.done.final == RespostaFinal(answer="a", sources=["b"])
    assert resultado.done.exception is not None
    assert "tempo" in resultado.done.exception.lower()


def test_serve_devolve_final_dentro_de_exec_done() -> None:
    entrada = io.StringIO(
        encode(Exec(cell_id=1, code="FINAL('pronto', ['arquivo.xlsx'])", timeout_s=2.0))
    )
    saida = io.StringIO()
    serve(entrada, saida)
    (resultado,) = _linhas_decodificadas(saida.getvalue())
    assert resultado.final == RespostaFinal(answer="pronto", sources=["arquivo.xlsx"])
