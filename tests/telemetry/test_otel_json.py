"""Testes da telemetria para o agente JSON (T18a): as chamadas viram spans `tool` do turno.

Nada aqui abre socket: o exportador é o `InMemorySpanExporter` do SDK.
"""

import itertools

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from harness.orchestrator.loop import Evento
from harness.telemetry.otel import ConfigTelemetria, TelemetriaOtel


def _telemetria() -> tuple[TelemetriaOtel, InMemorySpanExporter]:
    memoria = InMemorySpanExporter()
    contador = itertools.count(1_000_000_000, 1_000_000)
    config = ConfigTelemetria(
        ligada=True,
        chave_publica=SecretStr("pk-de-teste"),
        chave_secreta=SecretStr("sk-de-teste"),
        endpoint_base="https://langfuse.exemplo.invalido",
        conteudo="completo",
    )
    telemetria = TelemetriaOtel(config, exportador=memoria, relogio_ns=lambda: next(contador))
    return telemetria, memoria


def _evento(tipo: str, **dados: object) -> Evento:
    return Evento(tipo=tipo, dados=dict(dados))


def _pedido_json() -> list[Evento]:
    """Um pedido do agente JSON: dois turnos, uma chamada de ferramenta no primeiro."""
    return [
        _evento("pedido_iniciado", perfil="Vendas", hash_prompt="h", pedido="x", orcamento={}),
        _evento(
            "turno_modelo",
            iteracao=1,
            resumo=False,
            texto="",
            tokens_entrada=100,
            tokens_saida=10,
            modelo="m",
            provedor="p",
            custo=0.001,
            motivo_fim="tool_calls",
        ),
        _evento(
            "chamada_ferramenta",
            nome="catalogo.listar",
            args={"departamento": "vendas"},
            ok=True,
            tamanho_resultado=42,
            latencia_ms=5,
            erro=None,
        ),
        _evento("observacao", cell_id=None, texto="<saida_nao_confiavel>..."),
        _evento(
            "turno_modelo",
            iteracao=2,
            resumo=False,
            texto="",
            tokens_entrada=200,
            tokens_saida=8,
            modelo="m",
            provedor="p",
            custo=0.002,
            motivo_fim="tool_calls",
        ),
        _evento(
            "pedido_finalizado",
            motivo="final",
            completo=True,
            iteracoes=2,
            resposta="ok",
            fontes=["f"],
        ),
    ]


def _por_tipo(spans: list[ReadableSpan], tipo: str) -> list[ReadableSpan]:
    return [s for s in spans if (s.attributes or {}).get("langfuse.observation.type") == tipo]


def test_chamada_de_ferramenta_vira_span_tool_filho_do_turno_que_a_pediu() -> None:
    telemetria, memoria = _telemetria()
    for evento in _pedido_json():
        telemetria(evento)
    telemetria.exportar({"caso_id": "x"}, nome="eval/x")
    telemetria.fechar()
    spans = list(memoria.get_finished_spans())
    ferramentas = _por_tipo(spans, "tool")
    assert len(ferramentas) == 1
    turnos = [s for s in spans if s.name == "turno 1"]
    assert len(turnos) == 1
    assert ferramentas[0].parent is not None
    assert ferramentas[0].parent.span_id == turnos[0].context.span_id


def test_agente_json_nao_gera_span_de_celula() -> None:
    telemetria, memoria = _telemetria()
    for evento in _pedido_json():
        telemetria(evento)
    telemetria.exportar()
    telemetria.fechar()
    nomes = [s.name for s in memoria.get_finished_spans()]
    assert not any(nome.startswith("celula") for nome in nomes)
    assert "invoke_agent" in nomes


def test_tool_tem_nome_da_ferramenta_e_metadados_sem_texto_de_arquivo() -> None:
    telemetria, memoria = _telemetria()
    for evento in _pedido_json():
        telemetria(evento)
    telemetria.exportar()
    telemetria.fechar()
    ferramenta = _por_tipo(list(memoria.get_finished_spans()), "tool")[0]
    atributos = ferramenta.attributes or {}
    assert atributos.get("gen_ai.tool.name") == "catalogo.listar"
    assert atributos.get("langfuse.observation.metadata.tamanho_resultado") == 42
