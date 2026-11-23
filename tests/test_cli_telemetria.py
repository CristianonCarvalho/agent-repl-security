"""Testes da telemetria na CLI (`python -m harness`): ligada, desligada e configuração inválida.

Usa LLM falso, sandbox falso e um exportador OTLP substituto que guarda os spans em memória.
Nada sai da máquina: o `OTLPSpanExporter` real é trocado por um substituto antes do teste.
"""

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import harness.cli as cli
import harness.telemetry.otel as otel
from harness.cli import CODIGO_COMPLETO, CODIGO_USO, main
from harness.llm.base import LLMFalso
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal

PK = "pk-lf-FALSA-cli-111"
SK = "sk-lf-FALSA-cli-222"
CHAVE_OPENROUTER = "sk-or-FALSA-cli-333444"
FINAL_COM_CHAVE = (
    f"```python\n# {CHAVE_OPENROUTER}\nFINAL('Resposta pronta', ['vendas/pedidos.csv'])\n```"
)


class _SandboxFalso:
    """Sandbox que responde com FINAL na primeira célula, sem Docker."""

    def start(self) -> None:
        return None

    def executar_detalhado(
        self, cell_id: int, code: str, timeout_s: float, despachante: Any = None
    ) -> ResultadoExecucao:
        done = ExecDone(
            cell_id=cell_id,
            stdout="",
            stderr="",
            exception=None,
            duration_ms=1,
            final=RespostaFinal(answer="Resposta pronta", sources=["vendas/pedidos.csv"]),
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        return None

    def carregar_contexto(self, valor: Any) -> None:
        return None

    def close(self) -> None:
        return None


class _ExportadorGravado(InMemorySpanExporter):
    """Substituto do OTLPSpanExporter: guarda os spans em memória e registra a criação."""

    criados: list["_ExportadorGravado"] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__()
        self.kwargs = kwargs
        _ExportadorGravado.criados.append(self)


class _ExportadorFalho(SpanExporter):
    """Recusa todo envio, como um Langfuse fora do ar. Não usa rede."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        return None


@pytest.fixture
def exportadores(monkeypatch: pytest.MonkeyPatch) -> list[_ExportadorGravado]:
    _ExportadorGravado.criados = []
    monkeypatch.setattr(otel, "OTLPSpanExporter", _ExportadorGravado)
    return _ExportadorGravado.criados


@pytest.fixture
def dados(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido,valor\n1,10\n", encoding="utf-8")
    (raiz / "comercial").mkdir()
    (raiz / "marketing").mkdir()
    return raiz


def _argv(dados: Path, trace: Path) -> list[str]:
    return ["Quanto vendemos?", "--perfil", "Vendas", "--dados", str(dados), "--trace", str(trace)]


def _ativar_telemetria(monkeypatch: pytest.MonkeyPatch, *, conteudo: str | None = None) -> None:
    monkeypatch.setenv("HARNESS_TELEMETRIA", "langfuse")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)
    monkeypatch.setenv("OPENROUTER_API_KEY", CHAVE_OPENROUTER)
    if conteudo is not None:
        monkeypatch.setenv("HARNESS_TELEMETRIA_CONTEUDO", conteudo)


def _executar(dados: Path, trace: Path, resposta: str = FINAL_COM_CHAVE) -> tuple[int, str]:
    fala = io.StringIO()
    codigo = main(
        _argv(dados, trace),
        modelo=LLMFalso([resposta]),
        saida=fala,
        fabrica_sandbox=lambda *_, **__: _SandboxFalso(),
    )
    return codigo, fala.getvalue()


def _spans(exportador: _ExportadorGravado) -> list[ReadableSpan]:
    return list(exportador.get_finished_spans())


def test_ligada_exporta_o_pedido_com_o_mesmo_id_do_trace_jsonl(
    tmp_path: Path,
    dados: Path,
    exportadores: list[_ExportadorGravado],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ativar_telemetria(monkeypatch)
    trace = tmp_path / "trace"

    codigo, saida = _executar(dados, trace)

    assert codigo == CODIGO_COMPLETO
    assert "Telemetria: Langfuse US, conteúdo completo" in saida
    (arquivo,) = trace.glob("*.jsonl")
    (exportador,) = exportadores
    assert (
        exportador.kwargs["endpoint"] == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"
    )
    raiz = next(s for s in _spans(exportador) if s.parent is None)
    assert raiz.attributes is not None
    assert raiz.attributes["langfuse.session.id"] == arquivo.stem
    assert raiz.attributes["langfuse.trace.tags"] == ("cli",)
    assert raiz.attributes["langfuse.trace.metadata.perfil"] == "Vendas"


def test_chaves_nunca_saem_nem_na_saida_nem_nos_spans(
    tmp_path: Path,
    dados: Path,
    exportadores: list[_ExportadorGravado],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ativar_telemetria(monkeypatch)

    _, saida = _executar(dados, tmp_path / "trace")

    textos = [saida]
    for span in _spans(exportadores[0]):
        textos.append(span.name)
        textos.extend(str(valor) for valor in (span.attributes or {}).values())
        for evento in span.events:
            textos.extend(str(valor) for valor in (evento.attributes or {}).values())
    for chave in (PK, SK, CHAVE_OPENROUTER):
        assert all(chave not in texto for texto in textos), chave
    assert any("[redigido]" in texto for texto in textos)


def test_modo_numeros_nao_leva_o_texto_do_modelo(
    tmp_path: Path,
    dados: Path,
    exportadores: list[_ExportadorGravado],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ativar_telemetria(monkeypatch, conteudo="numeros")

    _, saida = _executar(dados, tmp_path / "trace")

    assert "Telemetria: Langfuse US, conteúdo só números" in saida
    for span in _spans(exportadores[0]):
        assert span.attributes is not None
        assert "langfuse.observation.input" not in span.attributes
        assert "langfuse.observation.output" not in span.attributes


def test_desligada_nao_cria_exportador_nem_linha_informativa(
    tmp_path: Path, dados: Path, exportadores: list[_ExportadorGravado]
) -> None:
    codigo, saida = _executar(dados, tmp_path / "trace")

    assert codigo == CODIGO_COMPLETO
    assert "Telemetria" not in saida
    assert exportadores == []


def test_exportacao_sem_falha_nao_imprime_aviso(
    tmp_path: Path,
    dados: Path,
    exportadores: list[_ExportadorGravado],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ativar_telemetria(monkeypatch)

    codigo, saida = _executar(dados, tmp_path / "trace")

    assert codigo == CODIGO_COMPLETO
    assert "aviso" not in saida


def test_exportacao_com_falha_imprime_aviso_e_mantem_o_codigo(
    tmp_path: Path,
    dados: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ativar_telemetria(monkeypatch)
    monkeypatch.setattr(cli, "ARQUIVO_ENV", tmp_path / "sem-env")
    monkeypatch.setattr(otel, "OTLPSpanExporter", lambda **_: _ExportadorFalho())

    codigo, saida = _executar(dados, tmp_path / "trace")

    assert codigo == CODIGO_COMPLETO
    assert "aviso: 1 falha(s) ao enviar a telemetria ao Langfuse. " in saida
    assert "O trace JSONL local continua completo." in saida
    assert SK not in saida
    assert PK not in saida


def test_ligada_sem_chave_publica_sai_com_dois_sem_expor_a_secreta(
    tmp_path: Path,
    dados: Path,
    exportadores: list[_ExportadorGravado],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ativar_telemetria(monkeypatch)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY")

    codigo, saida = _executar(dados, tmp_path / "trace")

    assert codigo == CODIGO_USO
    assert "LANGFUSE_PUBLIC_KEY" in saida
    assert SK not in saida
    assert exportadores == []
