"""Testes do parser do turno do modelo (contrato 3 da spec)."""

import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from harness.orchestrator.turn import Turn, TurnParseError, parse_turn

# --- Casos unitários ----------------------------------------------------------


def test_turno_sem_bloco_nao_tem_codigo() -> None:
    turno = parse_turn("  Vou responder direto.  \n")
    assert turno == Turn(reasoning="Vou responder direto.", code=None)


def test_turno_com_um_bloco_python() -> None:
    texto = "Vou somar os valores.\n```python\nprint(1 + 1)\n```\nAguardo o resultado."
    turno = parse_turn(texto)
    assert turno.code == "print(1 + 1)"
    assert turno.reasoning == "Vou somar os valores.\nAguardo o resultado."


def test_bloco_com_varias_linhas_preserva_o_codigo() -> None:
    texto = "```python\nx = 1\n\ny = x + 1\n```"
    assert parse_turn(texto).code == "x = 1\n\ny = x + 1"


def test_dois_blocos_python_levantam_erro_em_portugues() -> None:
    texto = "```python\na = 1\n```\ndepois\n```python\nb = 2\n```"
    with pytest.raises(TurnParseError, match="2 blocos"):
        parse_turn(texto)


def test_bloco_python_nao_fechado_levanta_erro() -> None:
    with pytest.raises(TurnParseError, match="não foi fechado"):
        parse_turn("pensando\n```python\nprint(1)\n")


def test_bloco_de_outra_linguagem_nao_conta_como_codigo() -> None:
    texto = 'Exemplo:\n```json\n{"a": 1}\n```\nFim.'
    turno = parse_turn(texto)
    assert turno.code is None
    assert "```json" in turno.reasoning
    assert '{"a": 1}' in turno.reasoning


def test_bloco_sem_linguagem_nao_conta_como_codigo() -> None:
    turno = parse_turn("```\nprint(1)\n```")
    assert turno.code is None
    assert "print(1)" in turno.reasoning


def test_bloco_de_outra_linguagem_nao_bloqueia_o_bloco_python() -> None:
    texto = "```text\nnota\n```\n```python\nprint(2)\n```"
    turno = parse_turn(texto)
    assert turno.code == "print(2)"
    assert "nota" in turno.reasoning


def test_texto_vazio_nao_tem_codigo() -> None:
    assert parse_turn("") == Turn(reasoning="", code=None)


# --- Propriedades (Hypothesis) -----------------------------------------------


@given(st.text())
def test_parse_de_texto_arbitrario_devolve_turn_ou_erro_de_parse(texto: str) -> None:
    try:
        resultado = parse_turn(texto)
    except TurnParseError:
        return
    assert isinstance(resultado, Turn)


_sem_cercas = st.text().filter(lambda s: "```" not in s)


@given(raciocinio=_sem_cercas, codigo=_sem_cercas)
def test_turno_com_um_bloco_devolve_o_mesmo_codigo_e_raciocinio(
    raciocinio: str, codigo: str
) -> None:
    texto = f"{raciocinio}\n```python\n{codigo}\n```"
    turno = parse_turn(texto)
    assert turno.code == codigo
    assert turno.reasoning == raciocinio.strip()


@given(st.text())
def test_parse_nao_levanta_excecao_fora_de_turn_parse_error(texto: str) -> None:
    with contextlib.suppress(TurnParseError):
        parse_turn(texto)
