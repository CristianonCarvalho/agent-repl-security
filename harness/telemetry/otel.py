"""Telemetria OpenTelemetry para o Langfuse (Tarefa 20a): um trace por pedido, enviado por OTLP.

`TelemetriaOtel` é um consumidor de eventos com a mesma interface do `TraceJsonl`: o
orquestrador entrega cada `Evento` por `__call__`, que só acumula. No fim do pedido, `exportar`
monta a árvore de spans com os instantes que o consumidor viu, envia por OTLP/HTTP e limpa o
buffer. O JSONL local continua sendo gravado em paralelo.

Decisões deste módulo (não estão na spec):
- SDK e exportador OTLP/HTTP do OpenTelemetry, sem SDK proprietário do Langfuse. O
  `TracerProvider` é próprio, não o global, para não interferir em outras bibliotecas.
- `BatchSpanProcessor`, não o simples: o simples faria uma chamada HTTP síncrona para cada span,
  ao terminar cada um. O `force_flush` ao fim de `exportar` garante a entrega antes de o
  controle voltar, então o pedido sai em um envio só. Nos testes, o `InMemorySpanExporter`
  recebe os spans depois desse flush, então o resultado é determinístico.
- O exportador é embrulhado: erro dele vira contagem em `falhas`, nunca exceção. O
  `BatchSpanProcessor` engole erros de exportação, então sem o embrulho eles sumiriam.
- Instantes: cada evento é marcado quando chega (`relogio_ns`). A duração real de uma célula
  vem de `duracao_ms`, quando o evento traz. Todo filho é prendido à janela do pai, então a
  hierarquia de tempo é sempre válida, mesmo com eventos incompletos.
- Chaves: `SecretStr` no pydantic. Ele esconde a chave em `repr`, `str` e `model_dump`. Os
  valores também entram na redação de segredos, feita em toda string que sai.
- Redação e truncagem acontecem antes do JSON: cada texto tem os segredos trocados por
  `[redigido]` e é cortado no teto (8000 caracteres) com marcador. O JSON continua válido.
- Entrada da geração: o evento `turno_modelo` traz só a resposta do modelo, não as mensagens
  enviadas. Por isso a geração leva a saída e não a entrada.
- Árvore: turnos e células são filhos da raiz, na ordem do tempo. As chamadas de ferramenta são
  filhas da célula em que rodaram, porque acontecem antes do evento `execucao`.
- Session: um id por pedido, salvo quando quem chama passa `sessao`. A CLI e o executor passam
  o id do trace JSONL, para cruzar as duas saídas.
- `conteudo = "numeros"`: nenhum texto de pedido, código, observação, resposta ou exceção vai a
  nenhum atributo. Ficam só nomes fixos, contagens, tokens, custos, tempos e motivos.
"""

import base64
import json
import math
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode, Tracer, set_span_in_context
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from harness.orchestrator.loop import Evento

ENDPOINT_PADRAO: Final = "https://us.cloud.langfuse.com/api/public/otel"
NOME_SERVICO: Final = "agent-repl-security"
NOME_PADRAO: Final = "pedido"
TEXTO_MAXIMO: Final = 8000
MARCADOR_REDIGIDO: Final = "[redigido]"
_LIGADA: Final = "langfuse"
_LOCAIS: Final = frozenset({"localhost", "127.0.0.1", "::1"})

ValorAtributo = str | bool | int | float | tuple[str, ...]


class ErroDeTelemetria(Exception):
    """Configuração da telemetria inválida. A mensagem cita nomes de variáveis, nunca valores."""


def _validar_endpoint(valor: str) -> str:
    """Devolve o endpoint sem barra final. Levanta ValueError se não for https (ou http local)."""
    limpo = valor.strip()
    partes = urlsplit(limpo)
    if partes.scheme not in ("https", "http") or not partes.hostname:
        raise ValueError("precisa ser uma URL https (http só para localhost)")
    if partes.scheme == "http" and partes.hostname not in _LOCAIS:
        raise ValueError("http só é aceito para localhost")
    if partes.username is not None or partes.password is not None:
        raise ValueError("não pode trazer usuário ou senha na URL")
    return limpo.rstrip("/")


class ConfigTelemetria(BaseModel):
    """Parâmetros da telemetria. As chaves são `SecretStr`: não aparecem em repr nem em dump."""

    model_config = ConfigDict(frozen=True)

    ligada: bool = False
    chave_publica: SecretStr | None = None
    chave_secreta: SecretStr | None = None
    endpoint_base: str = ENDPOINT_PADRAO
    conteudo: Literal["completo", "numeros"] = "completo"
    timeout_exportacao_s: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    @field_validator("endpoint_base")
    @classmethod
    def _endpoint_valido(cls, valor: str) -> str:
        try:
            return _validar_endpoint(valor)
        except ValueError as erro:
            raise ValueError(f"endpoint {erro}") from None


def _valor(env: Mapping[str, str], nome: str) -> str | None:
    bruto = env.get(nome, "").strip()
    return bruto or None


def config_de_ambiente(env: Mapping[str, str]) -> ConfigTelemetria:
    """Monta a configuração a partir do ambiente. Desligada por padrão.

    Só `HARNESS_TELEMETRIA=langfuse` liga. Ligada sem as duas chaves, com conteúdo inválido ou
    com endpoint inválido levanta `ErroDeTelemetria`, que cita só o nome da variável.
    """
    if (_valor(env, "HARNESS_TELEMETRIA") or "").lower() != _LIGADA:
        return ConfigTelemetria()

    publica = _valor(env, "LANGFUSE_PUBLIC_KEY")
    secreta = _valor(env, "LANGFUSE_SECRET_KEY")
    if publica is None or secreta is None:
        faltando = [
            nome
            for nome, valor in (("LANGFUSE_PUBLIC_KEY", publica), ("LANGFUSE_SECRET_KEY", secreta))
            if valor is None
        ]
        raise ErroDeTelemetria(
            f"telemetria ligada (HARNESS_TELEMETRIA=langfuse), mas faltam: {', '.join(faltando)}. "
            "Defina as variáveis no ambiente ou no arquivo .env."
        )

    bruto_conteudo = (_valor(env, "HARNESS_TELEMETRIA_CONTEUDO") or "completo").lower()
    conteudo: Literal["completo", "numeros"]
    if bruto_conteudo == "completo":
        conteudo = "completo"
    elif bruto_conteudo == "numeros":
        conteudo = "numeros"
    else:
        raise ErroDeTelemetria("HARNESS_TELEMETRIA_CONTEUDO deve ser 'completo' ou 'numeros'")

    try:
        endpoint = _validar_endpoint(_valor(env, "LANGFUSE_OTLP_ENDPOINT") or ENDPOINT_PADRAO)
    except ValueError as erro:
        raise ErroDeTelemetria(f"LANGFUSE_OTLP_ENDPOINT inválido: {erro}") from None

    return ConfigTelemetria(
        ligada=True,
        chave_publica=SecretStr(publica),
        chave_secreta=SecretStr(secreta),
        endpoint_base=endpoint,
        conteudo=conteudo,
    )


def _chaves(config: ConfigTelemetria) -> tuple[str, str]:
    if config.chave_publica is None:
        raise ErroDeTelemetria("LANGFUSE_PUBLIC_KEY não definida")
    if config.chave_secreta is None:
        raise ErroDeTelemetria("LANGFUSE_SECRET_KEY não definida")
    return config.chave_publica.get_secret_value(), config.chave_secreta.get_secret_value()


def cabecalhos(config: ConfigTelemetria) -> dict[str, str]:
    """Cabeçalhos do exportador: Basic com `chavepublica:chavesecreta` e a versão 4 da ingestão."""
    publica, secreta = _chaves(config)
    credencial = base64.b64encode(f"{publica}:{secreta}".encode()).decode("ascii")
    return {"Authorization": f"Basic {credencial}", "x-langfuse-ingestion-version": "4"}


def url_de_traces(config: ConfigTelemetria) -> str:
    """Endpoint do exportador de traces: a base OTLP do Langfuse mais `/v1/traces`."""
    return f"{config.endpoint_base}/v1/traces"


def rotulo_telemetria(config: ConfigTelemetria) -> str:
    """Nome curto da telemetria, para o relatório. Não traz endpoint nem chaves."""
    return "langfuse-us" if config.endpoint_base == ENDPOINT_PADRAO else "langfuse-personalizado"


def descricao_telemetria(config: ConfigTelemetria) -> str:
    """Frase curta para a linha informativa da CLI. Não traz endpoint nem chaves."""
    regiao = "US" if config.endpoint_base == ENDPOINT_PADRAO else "endpoint personalizado"
    conteudo = "completo" if config.conteudo == "completo" else "só números"
    return f"Langfuse {regiao}, conteúdo {conteudo}"


def _criar_exportador(config: ConfigTelemetria) -> SpanExporter:
    return OTLPSpanExporter(
        endpoint=url_de_traces(config),
        headers=cabecalhos(config),
        timeout=config.timeout_exportacao_s,
    )


class _ExportadorProtegido(SpanExporter):
    """Embrulha o exportador: erro vira contagem e resultado de falha, nunca exceção."""

    def __init__(self, interno: SpanExporter, ao_falhar: Callable[[], None]) -> None:
        self._interno = interno
        self._ao_falhar = ao_falhar

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            resultado = self._interno.export(spans)
        except Exception:
            self._ao_falhar()
            return SpanExportResult.FAILURE
        if resultado is not SpanExportResult.SUCCESS:
            self._ao_falhar()
        return resultado

    def shutdown(self) -> None:
        try:
            self._interno.shutdown()
        except Exception:
            self._ao_falhar()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._interno.force_flush(timeout_millis)
        except Exception:
            self._ao_falhar()
            return False


# --- redação, truncagem e conversão para atributos ---------------------------------------


def _ordenar_segredos(valores: Iterable[str]) -> tuple[str, ...]:
    """Segredos não vazios e sem repetição, do maior para o menor (troca os longos primeiro)."""
    unicos = {valor for valor in valores if valor}
    return tuple(sorted(unicos, key=len, reverse=True))


def _redigir(texto: str, segredos: Sequence[str]) -> str:
    for segredo in segredos:
        texto = texto.replace(segredo, MARCADOR_REDIGIDO)
    return texto


def _truncar(texto: str) -> str:
    if len(texto) <= TEXTO_MAXIMO:
        return texto
    excedente = len(texto) - TEXTO_MAXIMO
    return texto[:TEXTO_MAXIMO] + f"... [truncado, {excedente} caracteres a mais]"


def _sanear(valor: Any, segredos: Sequence[str]) -> Any:
    """Cópia do valor com segredos trocados e textos truncados. Floats não finitos viram None."""
    if isinstance(valor, str):
        return _truncar(_redigir(valor, segredos))
    if valor is None or isinstance(valor, bool | int):
        return valor
    if isinstance(valor, float):
        return valor if math.isfinite(valor) else None
    if isinstance(valor, Mapping):
        return {_redigir(str(k), segredos): _sanear(v, segredos) for k, v in valor.items()}
    if isinstance(valor, list | tuple | set | frozenset):
        return [_sanear(item, segredos) for item in valor]
    return _truncar(_redigir(repr(valor), segredos))


def _json(valor: Any, segredos: Sequence[str]) -> str:
    return json.dumps(_sanear(valor, segredos), ensure_ascii=False, default=str)


def _atributo(valor: Any, segredos: Sequence[str]) -> ValorAtributo | None:
    """Valor seguro para atributo OTel. None quando não há o que gravar."""
    saneado = _sanear(valor, segredos)
    if saneado is None:
        return None
    if isinstance(saneado, str | bool | int | float):
        return saneado
    if isinstance(saneado, list) and all(isinstance(item, str) for item in saneado):
        return tuple(saneado)
    return json.dumps(saneado, ensure_ascii=False, default=str)


def _atributos_de(dados: Mapping[str, Any], segredos: Sequence[str]) -> dict[str, ValorAtributo]:
    atributos: dict[str, ValorAtributo] = {}
    for chave, valor in dados.items():
        atributo = _atributo(valor, segredos)
        if atributo is not None:
            atributos[chave] = atributo
    return atributos


def _inteiro(valor: Any) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _numero(valor: Any) -> int | float | None:
    """O valor se for número finito (sem bool). Qualquer outra coisa vira None."""
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        return None
    return valor if math.isfinite(valor) else None


def _ms_para_ns(milissegundos: float) -> int:
    """Milissegundos em nanossegundos, limitados a onze dias para não estourar o inteiro."""
    return int(min(max(milissegundos, 0.0), 1e9) * 1_000_000)


def _nivel(motivo: str) -> str:
    if motivo.startswith("erro"):
        return "ERROR"
    if motivo.startswith("orcamento"):
        return "WARNING"
    return "DEFAULT"


# --- árvore de spans ----------------------------------------------------------------------


@dataclass
class _No:
    """Um span já montado: nome, janela, atributos prontos e filhos. Nada aqui é texto cru."""

    nome: str
    inicio_ns: int
    fim_ns: int
    atributos: dict[str, ValorAtributo] = field(default_factory=dict)
    eventos: list[tuple[int, str, dict[str, ValorAtributo]]] = field(default_factory=list)
    erro: str | None = None
    cell_id: int | None = None
    filhos: list["_No"] = field(default_factory=list)


class _Montador:
    """Lê os eventos de um pedido, em ordem de chegada, e monta a árvore da raiz para baixo."""

    def __init__(self, raiz: _No, completo: bool, segredos: Sequence[str]) -> None:
        self.raiz = raiz
        self.meta: dict[str, Any] = {}
        self._completo = completo
        self._segredos = segredos
        self._cursor = raiz.inicio_ns
        self._turno: _No | None = None
        self._celula: _No | None = None
        self._ultima_celula: _No | None = None
        self._finalizado = False

    def processar(self, instante: int, evento: Evento) -> None:
        dados = evento.dados if isinstance(evento.dados, dict) else {}
        tipo = evento.tipo
        if tipo == "pedido_iniciado":
            self._pedido_iniciado(instante, dados)
        elif tipo == "turno_modelo":
            self._turno_modelo(instante, dados)
        elif tipo == "verificacao_estatica":
            self._verificacao(instante, dados)
        elif tipo == "chamada_ferramenta":
            self._ferramenta(instante, dados)
        elif tipo == "execucao":
            self._execucao(instante, dados)
        elif tipo == "observacao":
            self._observacao(instante, dados)
        elif tipo == "erro_llm":
            self._erro_llm(instante, dados)
        elif tipo == "pedido_finalizado":
            self._pedido_finalizado(instante, dados)
        # Tipo desconhecido é ignorado: a telemetria acompanha a trace, não valida o orquestrador.

    def finalizar(self, instante: int) -> None:
        self._fechar_pendente(instante)
        if not self._finalizado:
            self.raiz.fim_ns = max(self.raiz.fim_ns, instante)

    # --- eventos -----------------------------------------------------------------------

    def _pedido_iniciado(self, instante: int, dados: dict[str, Any]) -> None:
        self.raiz.inicio_ns = instante
        self._cursor = instante
        self.meta.update(
            perfil=dados.get("perfil"),
            hash_prompt=dados.get("hash_prompt"),
            orcamento=dados.get("orcamento"),
            contexto=dados.get("contexto"),
        )
        self._json_se_completo(self.raiz, "langfuse.observation.input", dados.get("pedido"))

    def _turno_modelo(self, instante: int, dados: dict[str, Any]) -> None:
        self._fechar_pendente(instante)
        iteracao = dados.get("iteracao")
        if dados.get("resumo") is True:
            nome = "resumo final"
        else:
            nome = f"turno {iteracao}" if _inteiro(iteracao) else "turno"
        turno = _No(nome=nome, inicio_ns=self._cursor, fim_ns=instante)
        turno.atributos["langfuse.observation.type"] = "generation"
        turno.atributos["gen_ai.operation.name"] = "chat"
        modelo = dados.get("modelo")
        self._definir(turno, "langfuse.observation.model.name", modelo)
        self._definir(turno, "gen_ai.response.model", modelo)
        self._uso(
            turno,
            _numero(dados.get("tokens_entrada")),
            _numero(dados.get("tokens_saida")),
            _numero(dados.get("custo")),
        )
        self._meta(turno, "provedor", dados.get("provedor"))
        self._meta(turno, "resumo", dados.get("resumo"))
        self._meta(turno, "motivo_fim", dados.get("motivo_fim"))
        self._meta(turno, "tokens_raciocinio", _numero(dados.get("tokens_raciocinio")))
        self._meta(turno, "tentativas_descartadas", _numero(dados.get("tentativas_descartadas")))
        self._json_se_completo(turno, "langfuse.observation.output", dados.get("texto"))
        self.raiz.filhos.append(turno)
        self._turno = turno
        self._cursor = instante

    def _verificacao(self, instante: int, dados: dict[str, Any]) -> None:
        self._fechar_pendente(instante)
        problemas = dados.get("problemas")
        lista = [p for p in problemas if isinstance(p, str)] if isinstance(problemas, list) else []
        if lista:
            # Reprovada na verificação estática: nada executou, então não há célula.
            alvo = self._turno if self._turno is not None else self.raiz
            atributos: dict[str, Any] = {"quantidade": len(lista)}
            if self._completo:
                atributos["problemas"] = lista
            alvo.eventos.append(
                (instante, "verificacao_reprovada", _atributos_de(atributos, self._segredos))
            )
            return
        celula = self._abrir_celula(instante)
        self._json_se_completo(celula, "langfuse.observation.input", dados.get("codigo"))

    def _ferramenta(self, instante: int, dados: dict[str, Any]) -> None:
        nome = dados.get("nome")
        nome_texto = nome if isinstance(nome, str) and nome else "ferramenta"
        latencia = _numero(dados.get("latencia_ms"))
        duracao_ns = _ms_para_ns(latencia or 0)
        ferramenta = _No(
            nome=_redigir(nome_texto, self._segredos),
            inicio_ns=instante - duracao_ns,
            fim_ns=instante,
        )
        ferramenta.atributos["langfuse.observation.type"] = "tool"
        self._definir(ferramenta, "gen_ai.tool.name", nome_texto)
        ok = dados.get("ok")
        if ok is False:
            ferramenta.atributos["langfuse.observation.level"] = "WARNING"
        self._meta(ferramenta, "ok", ok)
        self._meta(ferramenta, "tamanho_resultado", _numero(dados.get("tamanho_resultado")))
        self._meta(ferramenta, "latencia_ms", latencia)
        erro = dados.get("erro")
        if isinstance(erro, str) and erro:
            self._meta(ferramenta, "com_erro", True)
            if self._completo:
                self._definir(ferramenta, "langfuse.observation.status_message", erro)
        self._json_se_completo(ferramenta, "langfuse.observation.input", dados.get("args"))
        pai = self._celula if self._celula is not None else self.raiz
        pai.filhos.append(ferramenta)

    def _execucao(self, instante: int, dados: dict[str, Any]) -> None:
        celula = self._celula if self._celula is not None else self._abrir_celula(self._cursor)
        self._celula = None
        duracao = _numero(dados.get("duracao_ms"))
        if duracao is not None and duracao >= 0:
            # A duração real vem do sandbox: a célula começa quando a execução começou.
            celula.inicio_ns = max(celula.inicio_ns, instante - _ms_para_ns(duracao))
        celula.fim_ns = instante
        cell_id = dados.get("cell_id")
        celula.cell_id = cell_id if _inteiro(cell_id) else None
        celula.nome = f"celula {celula.cell_id}" if celula.cell_id is not None else "celula"
        self._meta(celula, "cell_id", celula.cell_id)
        self._meta(celula, "duracao_ms", duracao)
        self._meta(celula, "n_ferramentas", _contar(dados.get("ferramentas")))
        self._meta(celula, "final", dados.get("final"))
        excecao = dados.get("excecao")
        self._meta(celula, "tem_excecao", excecao is not None)
        if self._completo and excecao is not None:
            self._meta(celula, "excecao", excecao)
        if dados.get("reiniciado") is True:
            motivo = dados.get("motivo")
            celula.atributos["langfuse.observation.level"] = "WARNING"
            self._meta(celula, "reiniciado", True)
            self._meta(celula, "motivo_reinicio", motivo)
            celula.eventos.append(
                (instante, "reinicio_repl", _atributos_de({"motivo": motivo}, self._segredos))
            )
        self.raiz.filhos.append(celula)
        self._ultima_celula = celula
        self._cursor = instante

    def _observacao(self, instante: int, dados: dict[str, Any]) -> None:
        texto = dados.get("texto")
        cell_id = dados.get("cell_id")
        ultima = self._ultima_celula
        if ultima is not None and cell_id is not None and ultima.cell_id == cell_id:
            self._json_se_completo(ultima, "langfuse.observation.output", texto)
            self._meta(ultima, "tamanho_observacao", _tamanho(texto))
        else:
            # Observação de turno inválido (sem célula): vai como evento do último turno.
            alvo = self._turno if self._turno is not None else self.raiz
            atributos: dict[str, Any] = {"tamanho": _tamanho(texto)}
            if self._completo:
                atributos["texto"] = texto
            alvo.eventos.append((instante, "observacao", _atributos_de(atributos, self._segredos)))
        self._ultima_celula = None
        self._cursor = instante

    def _erro_llm(self, instante: int, dados: dict[str, Any]) -> None:
        self._fechar_pendente(instante)
        tipo = dados.get("tipo_excecao")
        tipo_texto = tipo if isinstance(tipo, str) and tipo else "erro"
        iteracao = dados.get("iteracao")
        nome = f"turno {iteracao} (falha)" if _inteiro(iteracao) else "turno (falha)"
        falha = _No(nome=nome, inicio_ns=self._cursor, fim_ns=instante, erro=tipo_texto)
        falha.atributos["langfuse.observation.type"] = "generation"
        falha.atributos["gen_ai.operation.name"] = "chat"
        falha.atributos["langfuse.observation.level"] = "ERROR"
        self._definir(falha, "langfuse.observation.status_message", tipo_texto)
        self._meta(falha, "tipo_excecao", tipo_texto)
        if self._completo:
            self._meta(falha, "erro_mensagem", dados.get("mensagem"))
        self.raiz.filhos.append(falha)
        self._turno = falha
        self._cursor = instante

    def _pedido_finalizado(self, instante: int, dados: dict[str, Any]) -> None:
        self._fechar_pendente(instante)
        self.raiz.fim_ns = instante
        self._finalizado = True
        motivo = dados.get("motivo")
        if isinstance(motivo, str):
            self._definir(self.raiz, "langfuse.observation.status_message", motivo)
            self.raiz.atributos["langfuse.observation.level"] = _nivel(motivo)
            if motivo.startswith("erro"):
                self.raiz.erro = motivo
        entrada = _numero(dados.get("tokens_entrada"))
        saida = _numero(dados.get("tokens_saida"))
        total = entrada + saida if entrada is not None and saida is not None else None
        fontes = dados.get("fontes")
        self.meta.update(
            motivo_termino=motivo,
            iteracoes=dados.get("iteracoes"),
            tokens_total=total,
            fontes_citadas=len(fontes) if isinstance(fontes, list) else None,
        )
        self._json_se_completo(self.raiz, "langfuse.observation.output", dados.get("resposta"))

    # --- apoio ---------------------------------------------------------------------------

    def _abrir_celula(self, instante: int) -> _No:
        celula = _No(nome="celula", inicio_ns=instante, fim_ns=instante)
        celula.atributos["langfuse.observation.type"] = "span"
        self._celula = celula
        return celula

    def _fechar_pendente(self, instante: int) -> None:
        """Fecha a célula aberta sem execução (ex.: falha do sandbox). Ela fica com erro."""
        celula = self._celula
        if celula is None:
            return
        self._celula = None
        celula.fim_ns = max(instante, celula.inicio_ns)
        celula.erro = "execução sem resultado"
        celula.atributos["langfuse.observation.level"] = "ERROR"
        self._meta(celula, "sem_execucao", True)
        self.raiz.filhos.append(celula)

    def _uso(
        self,
        no: _No,
        entrada: int | float | None,
        saida: int | float | None,
        custo: int | float | None,
    ) -> None:
        uso: dict[str, int | float] = {}
        if entrada is not None:
            uso["input"] = entrada
            no.atributos["gen_ai.usage.input_tokens"] = entrada
        if saida is not None:
            uso["output"] = saida
            no.atributos["gen_ai.usage.output_tokens"] = saida
        if uso:
            no.atributos["langfuse.observation.usage_details"] = json.dumps(uso)
        if custo is not None:
            no.atributos["langfuse.observation.cost_details"] = json.dumps({"total": custo})
            no.atributos["gen_ai.usage.cost"] = custo

    def _definir(self, no: _No, chave: str, valor: Any) -> None:
        atributo = _atributo(valor, self._segredos)
        if atributo is not None:
            no.atributos[chave] = atributo

    def _meta(self, no: _No, chave: str, valor: Any) -> None:
        self._definir(no, f"langfuse.observation.metadata.{chave}", valor)

    def _json_se_completo(self, no: _No, chave: str, valor: Any) -> None:
        if self._completo and valor is not None:
            no.atributos[chave] = _json(valor, self._segredos)


def _contar(ferramentas: Any) -> int:
    if not isinstance(ferramentas, dict):
        return 0
    return sum(int(quantidade) for quantidade in ferramentas.values() if _inteiro(quantidade))


def _tamanho(texto: Any) -> int | None:
    return len(texto) if isinstance(texto, str) else None


def _ajustar(no: _No, inicio_pai: int, fim_pai: int) -> None:
    """Prende a janela do nó à do pai e faz o mesmo com os filhos, em profundidade."""
    no.inicio_ns = min(max(no.inicio_ns, inicio_pai), fim_pai)
    no.fim_ns = min(max(no.fim_ns, no.inicio_ns), fim_pai)
    for filho in no.filhos:
        _ajustar(filho, no.inicio_ns, no.fim_ns)


def _atributos_do_trace(
    nome: str,
    tags: Sequence[str],
    sessao: str,
    metadados: Mapping[str, Any],
    segredos: Sequence[str],
) -> dict[str, ValorAtributo]:
    """Atributos de nível de trace. Vão em todos os spans, para filtrar no Langfuse."""
    atributos: dict[str, ValorAtributo] = {}
    nome_atributo = _atributo(nome, segredos)
    if nome_atributo is not None:
        atributos["langfuse.trace.name"] = nome_atributo
    atributos["langfuse.session.id"] = sessao
    etiquetas = _atributo(list(tags), segredos)
    if isinstance(etiquetas, tuple) and etiquetas:
        atributos["langfuse.trace.tags"] = etiquetas
    for chave, valor in metadados.items():
        atributo = _atributo(valor, segredos)
        if atributo is not None:
            atributos[f"langfuse.trace.metadata.{chave}"] = atributo
    return atributos


def _montar_arvore(
    eventos: Sequence[tuple[int, Evento]],
    *,
    completo: bool,
    segredos: Sequence[str],
    agora_ns: int,
    nome: str,
    tags: Sequence[str],
    sessao: str,
    metadados: Mapping[str, Any],
) -> tuple[_No, dict[str, ValorAtributo]]:
    """Árvore do pedido e atributos de trace. Não toca em rede: é pura, testável direto."""
    inicio, fim = (eventos[0][0], eventos[-1][0]) if eventos else (agora_ns, agora_ns)
    raiz = _No(
        nome="invoke_agent",
        inicio_ns=inicio,
        fim_ns=fim,
        atributos={"gen_ai.operation.name": "invoke_agent", "langfuse.observation.type": "agent"},
    )
    montador = _Montador(raiz, completo, segredos)
    for instante, evento in eventos:
        montador.processar(instante, evento)
    montador.finalizar(fim)
    raiz.fim_ns = max(raiz.fim_ns, raiz.inicio_ns)
    _ajustar(raiz, raiz.inicio_ns, raiz.fim_ns)
    atributos = _atributos_do_trace(nome, tags, sessao, {**montador.meta, **metadados}, segredos)
    return raiz, atributos


class TelemetriaOtel:
    """Consumidor de eventos que exporta um trace por pedido para o Langfuse, por OTLP/HTTP.

    Use `exportar` no fim de cada pedido e `fechar` no fim do processo. Nenhum método levanta
    exceção: falhas são contadas em `falhas`, e o pedido segue.
    """

    def __init__(
        self,
        config: ConfigTelemetria,
        *,
        exportador: SpanExporter | None = None,
        segredos: Sequence[str] = (),
        relogio_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        valores = list(segredos)
        if config.chave_publica is not None:
            valores.append(config.chave_publica.get_secret_value())
        if config.chave_secreta is not None:
            valores.append(config.chave_secreta.get_secret_value())
        self._segredos = _ordenar_segredos(valores)
        self._completo = config.conteudo == "completo"
        self._timeout_ms = int(config.timeout_exportacao_s * 1000)
        self._relogio_ns = relogio_ns
        self._eventos: list[tuple[int, Evento]] = []
        self._falhas = 0
        self._fechado = False
        interno = exportador if exportador is not None else _criar_exportador(config)
        self._provedor = TracerProvider(resource=Resource.create({"service.name": NOME_SERVICO}))
        self._provedor.add_span_processor(
            BatchSpanProcessor(_ExportadorProtegido(interno, self._registrar_falha))
        )
        self._tracer: Tracer = self._provedor.get_tracer("harness.telemetry")

    @property
    def falhas(self) -> int:
        """Quantas montagens, exportações ou feches falharam. O pedido segue em cada caso."""
        return self._falhas

    def __call__(self, evento: Evento) -> None:
        """Acumula o evento com o instante de chegada. Nunca levanta exceção nem faz I/O."""
        try:
            self._eventos.append((self._relogio_ns(), evento))
        except Exception:
            self._registrar_falha()

    def exportar(
        self,
        metadados: Mapping[str, Any] | None = None,
        *,
        nome: str | None = None,
        tags: Sequence[str] = (),
        sessao: str | None = None,
    ) -> None:
        """Monta e envia o trace do pedido acumulado e limpa o buffer. Nunca levanta exceção."""
        eventos, self._eventos = self._eventos, []
        if self._fechado:
            return
        try:
            raiz, atributos = _montar_arvore(
                eventos,
                completo=self._completo,
                segredos=self._segredos,
                agora_ns=self._relogio_ns(),
                nome=nome or NOME_PADRAO,
                tags=tags,
                sessao=sessao or uuid.uuid4().hex,
                metadados=dict(metadados or {}),
            )
            self._emitir(raiz, Context(), atributos)
            if not self._provedor.force_flush(self._timeout_ms):
                self._registrar_falha()
        except Exception:
            self._registrar_falha()

    def fechar(self) -> None:
        """Encerra o provedor e envia o que faltar. Idempotente e nunca levanta exceção."""
        if self._fechado:
            return
        self._fechado = True
        try:
            self._provedor.shutdown()
        except Exception:
            self._registrar_falha()

    def _emitir(
        self, no: _No, contexto_pai: Context, atributos_trace: Mapping[str, ValorAtributo]
    ) -> None:
        span = self._tracer.start_span(
            no.nome,
            context=contexto_pai,
            attributes={**atributos_trace, **no.atributos},
            start_time=no.inicio_ns,
        )
        for instante, nome_evento, atributos in no.eventos:
            span.add_event(nome_evento, attributes=atributos, timestamp=instante)
        if no.erro is not None:
            span.set_status(Status(StatusCode.ERROR, no.erro))
        contexto = set_span_in_context(span)
        for filho in no.filhos:
            self._emitir(filho, contexto, atributos_trace)
        span.end(end_time=no.fim_ns)

    def _registrar_falha(self) -> None:
        self._falhas += 1


def criar_telemetria(
    env: Mapping[str, str], *, segredos: Sequence[str] = ()
) -> TelemetriaOtel | None:
    """Telemetria pronta a partir do ambiente, ou None quando está desligada."""
    config = config_de_ambiente(env)
    if not config.ligada:
        return None
    return TelemetriaOtel(config, segredos=segredos)
