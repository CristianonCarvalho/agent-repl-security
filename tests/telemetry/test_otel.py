"""Testes da telemetria OpenTelemetry (Tarefa 20a): configuração, árvore de spans, modos de
conteúdo, redação de segredos, isolamento de falhas e propriedades.

Nada aqui abre socket: o exportador é o `InMemorySpanExporter` do SDK, injetado. A montagem do
exportador real é testada só com um substituto que não faz rede.
"""

import base64
import itertools
import json
from collections.abc import Callable, Sequence
from typing import Any, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

import harness.telemetry.otel as otel
from harness.orchestrator.loop import Evento
from harness.telemetry.otel import (
    ConfigTelemetria,
    ErroDeTelemetria,
    TelemetriaOtel,
    cabecalhos,
    config_de_ambiente,
    criar_telemetria,
    url_de_traces,
)

PK = "pk-lf-FALSA-111"
SK = "sk-lf-FALSA-222"
CHAVE_OPENROUTER = "sk-or-FALSA-333444"
PEDIDO = "Quanto vendemos em março? SENTINELA_PEDIDO_xyz"
CODIGO_1 = "print('SENTINELA_CODIGO_abc')"
CODIGO_2 = "FINAL('SENTINELA_RESPOSTA_qrs', ['vendas/pedidos.csv'])"
OBSERVACAO = "SENTINELA_OBSERVACAO_uvw resultado"
RESPOSTA = "SENTINELA_RESPOSTA_qrs"
ERRO_FERRAMENTA = "SENTINELA_ERRO_fer detalhe"
TEXTOS_SENTINELA = (
    "SENTINELA_PEDIDO_xyz",
    "SENTINELA_CODIGO_abc",
    "SENTINELA_RESPOSTA_qrs",
    "SENTINELA_OBSERVACAO_uvw",
    "SENTINELA_ERRO_fer",
)


def _relogio(passo_ns: int = 1_000_000) -> Callable[[], int]:
    """Relógio falso: cada leitura avança um passo. Determinístico."""
    contador = itertools.count(start=1_700_000_000_000_000_000, step=passo_ns)
    return lambda: next(contador)


def _ev(tipo: str, **dados: Any) -> Evento:
    return Evento(tipo=tipo, dados=dados)


def _config(conteudo: Literal["completo", "numeros"] = "completo") -> ConfigTelemetria:
    return ConfigTelemetria(
        ligada=True,
        chave_publica=SecretStr(PK),
        chave_secreta=SecretStr(SK),
        conteudo=conteudo,
    )


def _telemetria(
    conteudo: Literal["completo", "numeros"] = "completo",
    segredos: Sequence[str] = (),
    exportador: SpanExporter | None = None,
) -> tuple[TelemetriaOtel, InMemorySpanExporter]:
    memoria = InMemorySpanExporter()
    telemetria = TelemetriaOtel(
        _config(conteudo),
        exportador=exportador if exportador is not None else memoria,
        segredos=segredos,
        relogio_ns=_relogio(),
    )
    return telemetria, memoria


def _pedido(
    *,
    pedido: str = PEDIDO,
    codigo_1: str = CODIGO_1,
    codigo_2: str = CODIGO_2,
    observacao: str = OBSERVACAO,
    erro_ferramenta: str | None = ERRO_FERRAMENTA,
    reinicio: bool = False,
) -> list[Evento]:
    """Pedido sintético realista: 2 turnos, 1 célula com 2 chamadas de ferramenta e FINAL."""
    eventos = [
        _ev(
            "pedido_iniciado",
            hash_prompt="hash-prompt-1",
            perfil="Vendas",
            pedido=pedido,
            orcamento={"max_iteracoes": 8, "max_segundos": 60.0},
            contexto=None,
        ),
        _ev(
            "turno_modelo",
            iteracao=1,
            resumo=False,
            texto=f"```python\n{codigo_1}\n```",
            tokens_entrada=100,
            tokens_saida=20,
            modelo="modelo/teste",
            provedor="provedor-teste",
            custo=0.001,
            motivo_fim="stop",
            tokens_raciocinio=None,
            tentativas_descartadas=0,
        ),
        _ev("verificacao_estatica", iteracao=1, codigo=codigo_1, problemas=[]),
        _ev(
            "chamada_ferramenta",
            nome="ler_csv",
            args={"caminho": "vendas/pedidos.csv"},
            ok=True,
            tamanho_resultado=50,
            latencia_ms=2,
            erro=None,
        ),
        _ev(
            "chamada_ferramenta",
            nome="contar",
            args={"coluna": "valor"},
            ok=erro_ferramenta is None,
            tamanho_resultado=0,
            latencia_ms=1,
            erro=erro_ferramenta,
        ),
        _ev(
            "execucao",
            cell_id=1,
            duracao_ms=2,
            ferramentas={"ler_csv": 1, "contar": 1},
            excecao=None,
            final=False,
        ),
        _ev("observacao", cell_id=1, texto=observacao),
    ]
    if reinicio:
        eventos.append(
            _ev("execucao", cell_id=2, reiniciado=True, motivo="a célula passou do tempo limite")
        )
        eventos.append(_ev("observacao", cell_id=2, texto="[cell 2] REPL reiniciado"))
    eventos += [
        _ev(
            "turno_modelo",
            iteracao=2,
            resumo=False,
            texto=f"```python\n{codigo_2}\n```",
            tokens_entrada=150,
            tokens_saida=30,
            modelo="modelo/teste",
            provedor="provedor-teste",
            custo=0.002,
            motivo_fim="stop",
            tokens_raciocinio=5,
            tentativas_descartadas=1,
        ),
        _ev("verificacao_estatica", iteracao=2, codigo=codigo_2, problemas=[]),
        _ev(
            "execucao",
            cell_id=3 if reinicio else 2,
            duracao_ms=1,
            ferramentas={},
            excecao=None,
            final=True,
        ),
        _ev("observacao", cell_id=3 if reinicio else 2, texto="fim"),
        _ev(
            "pedido_finalizado",
            motivo="final",
            completo=True,
            iteracoes=2,
            tokens_entrada=250,
            tokens_saida=50,
            resposta=RESPOSTA,
            fontes=["vendas/pedidos.csv"],
        ),
    ]
    return eventos


def _exportar_pedido(
    eventos: Sequence[Evento],
    conteudo: Literal["completo", "numeros"] = "completo",
    segredos: Sequence[str] = (),
    **kwargs: Any,
) -> list[ReadableSpan]:
    telemetria, memoria = _telemetria(conteudo, segredos)
    for evento in eventos:
        telemetria(evento)
    telemetria.exportar({"perfil": "Vendas"}, tags=["cli"], sessao="sessao-teste", **kwargs)
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()
    return spans


def _por_tipo(spans: Sequence[ReadableSpan], tipo: str) -> list[ReadableSpan]:
    return [
        s for s in spans if s.attributes and s.attributes.get("langfuse.observation.type") == tipo
    ]


def _raiz(spans: Sequence[ReadableSpan]) -> ReadableSpan:
    (raiz,) = [s for s in spans if s.parent is None]
    return raiz


def _textos_de(spans: Sequence[ReadableSpan]) -> list[str]:
    """Todo texto que sai do módulo: nomes, atributos (inclusive de eventos)."""
    textos: list[str] = [s.name for s in spans]
    for span in spans:
        for valor in (span.attributes or {}).values():
            textos.append(str(valor))
        for evento in span.events:
            textos.append(evento.name)
            textos.extend(str(valor) for valor in (evento.attributes or {}).values())
    return textos


# --- configuração -------------------------------------------------------------------------


def test_desligada_por_padrao_e_com_outro_valor() -> None:
    assert config_de_ambiente({}).ligada is False
    assert config_de_ambiente({"HARNESS_TELEMETRIA": "outro"}).ligada is False


def test_ligada_sem_chave_publica_cita_so_o_nome_da_variavel() -> None:
    ambiente = {"HARNESS_TELEMETRIA": "langfuse", "LANGFUSE_SECRET_KEY": "sk-lf-NAO-PODE-APARECER"}

    with pytest.raises(ErroDeTelemetria) as info:
        config_de_ambiente(ambiente)

    assert "LANGFUSE_PUBLIC_KEY" in str(info.value)
    assert "NAO-PODE-APARECER" not in str(info.value)


def test_ligada_sem_as_duas_chaves_cita_as_duas_variaveis() -> None:
    with pytest.raises(ErroDeTelemetria) as info:
        config_de_ambiente({"HARNESS_TELEMETRIA": "langfuse"})

    assert "LANGFUSE_PUBLIC_KEY" in str(info.value)
    assert "LANGFUSE_SECRET_KEY" in str(info.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://us.cloud.langfuse.com/api/public/otel",
        "https://usuario:senha@us.cloud.langfuse.com/api/public/otel",
        "ftp://exemplo.com",
        "sem-esquema",
    ],
)
def test_endpoint_invalido_falha_sem_ecoar_o_valor(endpoint: str) -> None:
    ambiente = _ambiente_ligado() | {"LANGFUSE_OTLP_ENDPOINT": endpoint}

    with pytest.raises(ErroDeTelemetria) as info:
        config_de_ambiente(ambiente)

    assert "LANGFUSE_OTLP_ENDPOINT" in str(info.value)
    assert "usuario:senha" not in str(info.value)


def test_http_so_para_localhost() -> None:
    ambiente = _ambiente_ligado() | {"LANGFUSE_OTLP_ENDPOINT": "http://localhost:4318/"}

    config = config_de_ambiente(ambiente)

    assert config.endpoint_base == "http://localhost:4318"


def test_conteudo_invalido_cita_a_variavel() -> None:
    ambiente = _ambiente_ligado() | {"HARNESS_TELEMETRIA_CONTEUDO": "tudo"}

    with pytest.raises(ErroDeTelemetria, match="HARNESS_TELEMETRIA_CONTEUDO"):
        config_de_ambiente(ambiente)


def test_padroes_da_configuracao_ligada() -> None:
    config = config_de_ambiente(_ambiente_ligado())

    assert config.endpoint_base == "https://us.cloud.langfuse.com/api/public/otel"
    assert config.conteudo == "completo"
    assert config.timeout_exportacao_s == 10
    assert url_de_traces(config) == "https://us.cloud.langfuse.com/api/public/otel/v1/traces"


def test_repr_e_dump_nao_mostram_as_chaves() -> None:
    config = config_de_ambiente(_ambiente_ligado())

    for texto in (repr(config), str(config), str(config.model_dump())):
        assert PK not in texto
        assert SK not in texto


def test_cabecalhos_basic_e_versao_da_ingestao() -> None:
    config = config_de_ambiente(_ambiente_ligado())

    cabecalho = cabecalhos(config)

    esperado = base64.b64encode(f"{PK}:{SK}".encode()).decode("ascii")
    assert cabecalho == {
        "Authorization": f"Basic {esperado}",
        "x-langfuse-ingestion-version": "4",
    }


def _ambiente_ligado() -> dict[str, str]:
    return {
        "HARNESS_TELEMETRIA": "langfuse",
        "LANGFUSE_PUBLIC_KEY": PK,
        "LANGFUSE_SECRET_KEY": SK,
    }


def test_criar_telemetria_desligada_devolve_none() -> None:
    assert criar_telemetria({}) is None


def test_criar_telemetria_ligada_monta_exportador_sem_rede(monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas: list[dict[str, Any]] = []

    class ExportadorFalso(InMemorySpanExporter):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__()
            chamadas.append(kwargs)

    monkeypatch.setattr(otel, "OTLPSpanExporter", ExportadorFalso)

    telemetria = criar_telemetria(_ambiente_ligado(), segredos=[CHAVE_OPENROUTER])

    assert telemetria is not None
    assert chamadas == [
        {
            "endpoint": "https://us.cloud.langfuse.com/api/public/otel/v1/traces",
            "headers": cabecalhos(config_de_ambiente(_ambiente_ligado())),
            "timeout": 10.0,
        }
    ]
    telemetria.fechar()


# --- árvore de spans ----------------------------------------------------------------------


def test_arvore_tem_raiz_turnos_celulas_e_ferramentas_aninhadas() -> None:
    spans = _exportar_pedido(_pedido())

    raiz = _raiz(spans)
    assert raiz.name == "invoke_agent"
    assert raiz.attributes is not None
    assert raiz.attributes["langfuse.observation.type"] == "agent"
    geracoes = _por_tipo(spans, "generation")
    celulas = _por_tipo(spans, "span")
    ferramentas = _por_tipo(spans, "tool")
    assert len(geracoes) == 2
    assert len(celulas) == 2
    assert len(ferramentas) == 2
    assert all(g.parent is not None and g.parent.span_id == raiz.context.span_id for g in geracoes)
    celula_1 = next(c for c in celulas if c.name == "celula 1")
    assert all(
        f.parent is not None and f.parent.span_id == celula_1.context.span_id for f in ferramentas
    )


def test_spans_compartilham_o_trace_e_respeitam_a_ordem_do_tempo() -> None:
    spans = _exportar_pedido(_pedido())

    assert len({s.context.trace_id for s in spans}) == 1
    raiz = _raiz(spans)
    for span in spans:
        assert span.start_time is not None and span.end_time is not None
        assert span.start_time <= span.end_time
        if span is raiz:
            continue
        assert span.parent is not None
        pai = next(s for s in spans if s.context.span_id == span.parent.span_id)
        assert pai.start_time is not None and pai.end_time is not None
        assert pai.start_time <= span.start_time
        assert span.end_time <= pai.end_time


def test_duracao_da_celula_vem_do_sandbox() -> None:
    spans = _exportar_pedido(_pedido())

    celula_1 = next(s for s in spans if s.name == "celula 1")
    assert celula_1.end_time is not None and celula_1.start_time is not None
    assert celula_1.end_time - celula_1.start_time == 2_000_000  # duracao_ms=2


def test_atributos_de_modelo_tokens_e_custo_sao_json_valido() -> None:
    spans = _exportar_pedido(_pedido())

    primeira = sorted(_por_tipo(spans, "generation"), key=lambda s: s.name)[0]
    assert primeira.attributes is not None
    atributos = primeira.attributes
    assert atributos["langfuse.observation.model.name"] == "modelo/teste"
    assert atributos["gen_ai.response.model"] == "modelo/teste"
    assert json.loads(str(atributos["langfuse.observation.usage_details"])) == {
        "input": 100,
        "output": 20,
    }
    assert atributos["gen_ai.usage.input_tokens"] == 100
    assert atributos["gen_ai.usage.output_tokens"] == 20
    assert json.loads(str(atributos["langfuse.observation.cost_details"])) == {"total": 0.001}
    assert atributos["langfuse.observation.metadata.provedor"] == "provedor-teste"
    assert atributos["langfuse.observation.metadata.tentativas_descartadas"] == 0


def test_atributos_de_trace_aparecem_em_todos_os_spans() -> None:
    spans = _exportar_pedido(_pedido())

    for span in spans:
        assert span.attributes is not None
        assert span.attributes["langfuse.trace.name"] == "pedido"
        assert span.attributes["langfuse.session.id"] == "sessao-teste"
        assert span.attributes["langfuse.trace.tags"] == ("cli",)
        assert span.attributes["langfuse.trace.metadata.perfil"] == "Vendas"
        assert span.attributes["langfuse.trace.metadata.motivo_termino"] == "final"
        assert span.attributes["langfuse.trace.metadata.iteracoes"] == 2


def test_ferramenta_com_erro_fica_com_nivel_de_aviso() -> None:
    spans = _exportar_pedido(_pedido())

    contar = next(s for s in _por_tipo(spans, "tool") if s.name == "contar")
    assert contar.attributes is not None
    assert contar.attributes["langfuse.observation.level"] == "WARNING"
    assert contar.attributes["gen_ai.tool.name"] == "contar"


def test_reinicio_do_repl_vira_atributo_e_evento_da_celula() -> None:
    spans = _exportar_pedido(_pedido(reinicio=True))

    reinicio = next(s for s in spans if s.name == "celula 2")
    assert reinicio.attributes is not None
    assert reinicio.attributes["langfuse.observation.metadata.reiniciado"] is True
    assert reinicio.attributes["langfuse.observation.level"] == "WARNING"
    assert [e.name for e in reinicio.events] == ["reinicio_repl"]


def test_erro_do_modelo_vira_geracao_com_nivel_erro() -> None:
    telemetria, memoria = _telemetria()
    telemetria(_ev("pedido_iniciado", hash_prompt="h", perfil="Vendas", pedido=PEDIDO))
    telemetria(_ev("erro_llm", iteracao=1, tipo_excecao="ErroLLM", mensagem="falha de rede"))
    telemetria(
        _ev("pedido_finalizado", motivo="erro_llm", completo=False, iteracoes=1, resposta="")
    )
    telemetria.exportar()
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    falha = next(s for s in spans if s.name.endswith("(falha)"))
    assert falha.attributes is not None
    assert falha.attributes["langfuse.observation.level"] == "ERROR"
    assert falha.attributes["langfuse.observation.metadata.tipo_excecao"] == "ErroLLM"
    assert falha.status.status_code.name == "ERROR"
    assert _raiz(spans).status.status_code.name == "ERROR"


def test_pedido_sem_eventos_exporta_so_a_raiz_e_nao_levanta() -> None:
    spans = _exportar_pedido([])

    assert [s.name for s in spans] == ["invoke_agent"]


def test_evento_desconhecido_e_campos_ausentes_nao_levantam() -> None:
    eventos = [
        _ev("tipo_que_nao_existe", x=1),
        _ev("turno_modelo"),
        _ev("verificacao_estatica"),
        _ev("chamada_ferramenta"),
        _ev("execucao", duracao_ms="muito"),
        _ev("observacao"),
        _ev("pedido_finalizado"),
    ]

    spans = _exportar_pedido(eventos)

    assert _raiz(spans).name == "invoke_agent"


def test_celula_sem_execucao_fica_com_erro_e_fecha_no_fim() -> None:
    eventos = [
        _ev("pedido_iniciado", hash_prompt="h", perfil="Vendas", pedido=PEDIDO),
        _ev("turno_modelo", iteracao=1, resumo=False, texto="x", tokens_entrada=1, tokens_saida=1),
        _ev("verificacao_estatica", iteracao=1, codigo=CODIGO_1, problemas=[]),
        _ev("pedido_finalizado", motivo="erro_sandbox", completo=False, iteracoes=1, resposta=""),
    ]

    spans = _exportar_pedido(eventos)

    celula = next(s for s in spans if s.name == "celula")
    assert celula.status.status_code.name == "ERROR"
    assert celula.attributes is not None
    assert celula.attributes["langfuse.observation.metadata.sem_execucao"] is True


def test_verificacao_reprovada_nao_cria_celula() -> None:
    eventos = [
        _ev("pedido_iniciado", hash_prompt="h", perfil="Vendas", pedido=PEDIDO),
        _ev("turno_modelo", iteracao=1, resumo=False, texto="x", tokens_entrada=1, tokens_saida=1),
        _ev("verificacao_estatica", iteracao=1, codigo=CODIGO_1, problemas=["import proibido"]),
        _ev("observacao", cell_id=None, texto="código reprovado"),
        _ev(
            "pedido_finalizado",
            motivo="orcamento_iteracoes",
            completo=False,
            iteracoes=1,
            resposta="r",
        ),
    ]

    spans = _exportar_pedido(eventos)

    assert _por_tipo(spans, "span") == []
    turno = next(s for s in spans if s.name == "turno 1")
    assert [e.name for e in turno.events] == ["verificacao_reprovada", "observacao"]


# --- modos de conteúdo ----------------------------------------------------------------------


def test_modo_numeros_nao_leva_texto_nenhum() -> None:
    spans = _exportar_pedido(_pedido(reinicio=True), conteudo="numeros")

    textos = _textos_de(spans)
    for sentinela in TEXTOS_SENTINELA:
        assert all(sentinela not in texto for texto in textos), sentinela
    for span in spans:
        assert span.attributes is not None
        assert "langfuse.observation.input" not in span.attributes
        assert "langfuse.observation.output" not in span.attributes
        assert "langfuse.observation.output" not in span.attributes


def test_modo_completo_leva_o_texto_do_pedido_e_da_resposta() -> None:
    spans = _exportar_pedido(_pedido(), conteudo="completo")

    raiz = _raiz(spans)
    assert raiz.attributes is not None
    assert json.loads(str(raiz.attributes["langfuse.observation.input"])) == PEDIDO
    assert json.loads(str(raiz.attributes["langfuse.observation.output"])) == RESPOSTA
    textos = _textos_de(spans)
    assert any("SENTINELA_CODIGO_abc" in texto for texto in textos)
    assert any("SENTINELA_OBSERVACAO_uvw" in texto for texto in textos)


def test_modo_completo_trunca_cada_texto_no_teto_e_mantem_json_valido() -> None:
    enorme = "x" * 20_000
    spans = _exportar_pedido(_pedido(observacao=enorme), conteudo="completo")

    celula = next(s for s in spans if s.name == "celula 1")
    assert celula.attributes is not None
    valor = str(celula.attributes["langfuse.observation.output"])
    texto = json.loads(valor)
    assert "[truncado" in texto
    assert len(texto) <= otel.TEXTO_MAXIMO + 100


# --- redação de segredos ------------------------------------------------------------------


def test_chave_falsa_em_pedido_codigo_resposta_e_erro_nunca_sai() -> None:
    cheio = f"{CHAVE_OPENROUTER} {PK} {SK}"
    eventos = _pedido(
        pedido=f"pedido com {cheio}",
        codigo_1=f"print('{CHAVE_OPENROUTER}')",
        codigo_2=f"FINAL('{PK}', ['x'])",
        observacao=f"obs {SK}",
        erro_ferramenta=f"falhou com {CHAVE_OPENROUTER}",
    )
    telemetria, memoria = _telemetria(segredos=[CHAVE_OPENROUTER])
    for evento in eventos:
        telemetria(evento)
    telemetria.exportar({"extra": f"valor {CHAVE_OPENROUTER}"}, tags=[f"tag {PK}"], sessao="s")
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    textos = _textos_de(spans)
    for chave in (CHAVE_OPENROUTER, PK, SK):
        assert all(chave not in texto for texto in textos), chave
    assert any("[redigido]" in texto for texto in textos)


# --- isolamento de falhas -----------------------------------------------------------------


class _ExportadorQuebrado(SpanExporter):
    """Levanta em export e em force_flush. Simula um destino fora do ar."""

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        raise RuntimeError("destino fora do ar")

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        raise RuntimeError("destino fora do ar")


class _ExportadorInstavel(InMemorySpanExporter):
    """Falha na primeira exportação e funciona nas seguintes."""

    def __init__(self) -> None:
        super().__init__()
        self._falhou = False

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if not self._falhou:
            self._falhou = True
            return SpanExportResult.FAILURE
        return super().export(spans)


def test_exportador_que_levanta_nao_propaga_e_conta_falha() -> None:
    telemetria, _ = _telemetria(exportador=_ExportadorQuebrado())
    for evento in _pedido():
        telemetria(evento)

    telemetria.exportar()
    telemetria.fechar()

    assert telemetria.falhas >= 1


def test_proximo_pedido_funciona_depois_de_falha() -> None:
    memoria = _ExportadorInstavel()
    telemetria, _ = _telemetria(exportador=memoria)
    for evento in _pedido():
        telemetria(evento)
    telemetria.exportar(sessao="primeiro")
    assert telemetria.falhas >= 1

    for evento in _pedido(pedido="segundo pedido"):
        telemetria(evento)
    telemetria.exportar(sessao="segundo")
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    assert {s.attributes["langfuse.session.id"] for s in spans if s.attributes} == {"segundo"}
    assert len([s for s in spans if s.parent is None]) == 1


def test_exportar_limpa_o_buffer_e_pedidos_seguidos_geram_traces_distintos() -> None:
    telemetria, memoria = _telemetria()
    for evento in _pedido():
        telemetria(evento)
    telemetria.exportar()
    for evento in _pedido(pedido="outro"):
        telemetria(evento)
    telemetria.exportar()
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    raizes = [s for s in spans if s.parent is None]
    assert len(raizes) == 2
    assert raizes[0].context.trace_id != raizes[1].context.trace_id


def test_fechar_e_idempotente_e_exportar_depois_nao_quebra() -> None:
    telemetria, _ = _telemetria()

    telemetria.fechar()
    telemetria.fechar()
    telemetria.exportar()

    assert telemetria.falhas == 0


def test_falha_de_relogio_no_consumidor_nao_levanta() -> None:
    def relogio_quebrado() -> int:
        raise RuntimeError("relógio quebrado")

    telemetria = TelemetriaOtel(
        _config(), exportador=InMemorySpanExporter(), relogio_ns=relogio_quebrado
    )
    telemetria(_ev("pedido_iniciado", pedido="x"))

    assert telemetria.falhas == 1
    telemetria.fechar()


# --- propriedades -------------------------------------------------------------------------

_TIPOS = [
    "pedido_iniciado",
    "turno_modelo",
    "verificacao_estatica",
    "chamada_ferramenta",
    "execucao",
    "observacao",
    "erro_llm",
    "pedido_finalizado",
    "evento_inventado",
]
_CHAVES = [
    "iteracao",
    "resumo",
    "texto",
    "modelo",
    "custo",
    "tokens_entrada",
    "problemas",
    "codigo",
    "nome",
    "latencia_ms",
    "ok",
    "erro",
    "args",
    "cell_id",
    "duracao_ms",
    "ferramentas",
    "reiniciado",
    "motivo",
    "tipo_excecao",
    "resposta",
    "fontes",
]
_VALORES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40),
    st.lists(st.text(max_size=10), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
)


@settings(max_examples=40, deadline=None)
@given(
    eventos=st.lists(
        st.tuples(
            st.sampled_from(_TIPOS), st.dictionaries(st.sampled_from(_CHAVES), _VALORES, max_size=6)
        ),
        max_size=25,
    )
)
def test_sequencias_aleatorias_nunca_levantam_excecao(
    eventos: list[tuple[str, dict[str, Any]]],
) -> None:
    telemetria, memoria = _telemetria()
    for tipo, dados in eventos:
        telemetria(Evento(tipo=tipo, dados=dados))

    telemetria.exportar()
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    assert telemetria.falhas == 0
    assert [s for s in spans if s.parent is None]


def test_variante_do_pedido_iniciado_e_do_executor_viram_um_atributo_so() -> None:
    """O agente JSON manda `variante` no `pedido_iniciado` e o executor manda no metadado. Os dois
    são o mesmo valor e saem como um atributo só, em todos os spans do trace."""
    telemetria, memoria = _telemetria()
    telemetria(
        _ev(
            "pedido_iniciado",
            pedido="x",
            perfil="Vendas",
            hash_prompt=None,
            orcamento=None,
            contexto=None,
            variante="json",
        )
    )
    telemetria(_ev("pedido_finalizado", motivo="final", resposta="r", iteracoes=1))
    telemetria.exportar({"variante": "json"}, tags=["eval", "variante-json"], sessao="s")
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    raiz = _raiz(spans)
    assert raiz.attributes is not None
    assert raiz.attributes["langfuse.trace.metadata.variante"] == "json"
    assert raiz.attributes["langfuse.trace.tags"] == ("eval", "variante-json")
    atributos = [s.attributes or {} for s in spans]
    assert all(a["langfuse.trace.metadata.variante"] == "json" for a in atributos)


def test_variante_vem_do_evento_quando_o_chamador_nao_informa() -> None:
    telemetria, memoria = _telemetria()
    telemetria(
        _ev(
            "pedido_iniciado",
            pedido="x",
            perfil="Vendas",
            hash_prompt=None,
            orcamento=None,
            contexto=None,
            variante="json-agregacao",
        )
    )
    telemetria(_ev("pedido_finalizado", motivo="final", resposta="r", iteracoes=1))
    telemetria.exportar({}, tags=["cli"], sessao="s")
    spans = list(memoria.get_finished_spans())
    telemetria.fechar()

    atributos = _raiz(spans).attributes or {}
    assert atributos["langfuse.trace.metadata.variante"] == "json-agregacao"
