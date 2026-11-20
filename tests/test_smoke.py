"""Teste de fumaça: o pacote e seus subpacotes são importáveis."""

import importlib

import harness


def test_pacote_raiz_importavel() -> None:
    assert harness.__doc__ is not None


def test_subpacotes_importaveis() -> None:
    for nome in ("orchestrator", "sandbox", "host", "llm", "telemetry"):
        modulo = importlib.import_module(f"harness.{nome}")
        assert modulo.__doc__, f"harness.{nome} sem docstring"
