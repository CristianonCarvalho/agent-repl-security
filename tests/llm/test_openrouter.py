"""Testes do ClienteOpenRouter com um cliente falso no formato do SDK `openai` (Tarefa 9).

Nenhum teste aqui usa rede: o `cliente` é injetado e `dormir` só registra as esperas.
"""

import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from harness.llm.base import ErroLLM, Mensagem, ModeloLLM
from harness.llm.config import ConfigModelo
from harness.llm.openrouter import ClienteOpenRouter

REQ = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
CHAVE_FALSA = "chave-falsa-de-teste-123"
CORPO_DO_ERRO = "corpo detalhado da resposta que não deve vazar"
MENSAGENS = [Mensagem(role="system", content="regras"), Mensagem(role="user", content="oi")]
NOME_THREAD_TENTATIVA = "openrouter-tentativa"
FOLGA_S = 1.0


@dataclass
class _Preso:
    """Item do roteiro que simula um provedor que não responde: a chamada espera `evento`.

    Depois de liberada, a chamada devolve (ou levanta) `depois`. Quem cria o evento deve
    liberá-lo no fim do teste, para a thread da tentativa terminar.
    """

    evento: threading.Event
    depois: object


class ClienteFalso:
    """Imita `client.chat.completions.create`: devolve ou levanta o roteiro, em ordem."""

    def __init__(self, roteiro: Sequence[object]) -> None:
        self._roteiro = list(roteiro)
        self.chamadas: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._criar))

    def _criar(self, **kwargs: Any) -> Any:
        self.chamadas.append(kwargs)
        item = self._roteiro.pop(0)
        if isinstance(item, _Preso):
            item.evento.wait(10)
            item = item.depois
        if isinstance(item, BaseException):
            raise item
        return item


class RelogioFalso:
    """Relógio controlado: só anda quando o teste (ou o `dormir` falso) manda."""

    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos


@pytest.fixture
def liberacoes() -> Iterator[list[threading.Event]]:
    """Guarda os eventos dos `_Preso` e, no fim, libera todos e espera as threads terminarem."""
    eventos: list[threading.Event] = []
    yield eventos
    for evento in eventos:
        evento.set()
    _esperar_threads_de_tentativa()


def _esperar_threads_de_tentativa() -> None:
    for thread in threading.enumerate():
        if thread.name == NOME_THREAD_TENTATIVA:
            thread.join(timeout=5)
    vivas = [t for t in threading.enumerate() if t.name == NOME_THREAD_TENTATIVA]
    assert vivas == [], "thread de tentativa ficou viva depois do teste"


def _completion(
    conteudo: str | None = "oi, tudo bem",
    *,
    usage: bool = True,
    provider: str | None = "Novita",
    cost: float | None = 0.00021,
) -> ChatCompletion:
    dados: dict[str, Any] = {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek/deepseek-v4.1-flash-2026-10-17",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": conteudo},
            }
        ],
    }
    if usage:
        dados["usage"] = {
            "prompt_tokens": 120,
            "completion_tokens": 8,
            "total_tokens": 128,
            **({"cost": cost} if cost is not None else {}),
        }
    if provider is not None:
        dados["provider"] = provider
    return ChatCompletion.model_validate(dados)


def _erro_http(
    classe: type[openai.APIStatusError],
    status: int,
    cabecalhos: dict[str, str] | None = None,
) -> openai.APIStatusError:
    resposta = httpx2.Response(status, headers=cabecalhos, request=REQ)
    return classe(CORPO_DO_ERRO, response=resposta, body={"error": CORPO_DO_ERRO})


def _config(**kwargs: Any) -> ConfigModelo:
    return ConfigModelo(slug="deepseek/deepseek-v4.1-flash", **kwargs)


def _cliente(
    roteiro: Sequence[object], **config: Any
) -> tuple[ClienteOpenRouter, ClienteFalso, list[float]]:
    falso = ClienteFalso(roteiro)
    esperas: list[float] = []
    cliente = ClienteOpenRouter(
        _config(**config), CHAVE_FALSA, cliente=falso, dormir=esperas.append
    )
    return cliente, falso, esperas


# --- Resposta ------------------------------------------------------------------------------


def test_cliente_implementa_o_protocolo_modelo_llm() -> None:
    modelo: ModeloLLM = ClienteOpenRouter(_config(), CHAVE_FALSA, cliente=ClienteFalso([]))
    assert modelo is not None


def test_resposta_normal_traz_texto_tokens_provedor_modelo_e_custo() -> None:
    cliente, _, esperas = _cliente([_completion()])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "oi, tudo bem"
    assert resposta.tokens_entrada == 120
    assert resposta.tokens_saida == 8
    assert resposta.provedor == "Novita"
    assert resposta.modelo == "deepseek/deepseek-v4.1-flash-2026-10-17"
    assert resposta.custo == pytest.approx(0.00021)
    assert esperas == []


def test_usage_ausente_zera_os_tokens_sem_falhar() -> None:
    cliente, _, _ = _cliente([_completion(usage=False, cost=None)])

    resposta = cliente.responder(MENSAGENS)

    assert (resposta.tokens_entrada, resposta.tokens_saida) == (0, 0)
    assert resposta.custo is None


def _resposta_em_dict(
    conteudo: object = "ok",
    *,
    motivo: object = "stop",
    reasoning: object = None,
    com_detalhes: bool = True,
) -> dict[str, Any]:
    """Resposta no formato de dicionário, como o SDK pode devolver os extras."""
    usage: dict[str, Any] = {"prompt_tokens": 1, "completion_tokens": 2}
    if com_detalhes:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return {
        "choices": [{"finish_reason": motivo, "message": {"content": conteudo}}],
        "usage": usage,
    }


def test_finish_reason_e_reasoning_tokens_lidos_de_objeto() -> None:
    cliente, _, _ = _cliente([_completion(conteudo="ok")])
    resposta = cliente.responder(MENSAGENS)
    assert resposta.motivo_fim == "stop"
    assert resposta.tokens_raciocinio is None
    assert resposta.tentativas_descartadas == 0


def test_finish_reason_e_reasoning_tokens_lidos_de_dict() -> None:
    cliente, _, _ = _cliente([_resposta_em_dict("ok", motivo="length", reasoning=5)])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "ok"
    assert resposta.motivo_fim == "length"
    assert resposta.tokens_raciocinio == 5


def test_reasoning_tokens_de_objeto_do_sdk_sao_lidos() -> None:
    usage = SimpleNamespace(
        prompt_tokens=3,
        completion_tokens=9,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=7),
    )
    resposta_sdk = SimpleNamespace(
        choices=[
            SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="texto final"))
        ],
        usage=usage,
    )
    cliente, _, _ = _cliente([resposta_sdk])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.tokens_raciocinio == 7
    assert resposta.motivo_fim == "stop"


def test_campos_ausentes_viram_none_sem_falhar() -> None:
    cliente, _, _ = _cliente([_resposta_em_dict("ok", motivo=None, com_detalhes=False)])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.motivo_fim is None
    assert resposta.tokens_raciocinio is None
    assert resposta.texto == "ok"


@pytest.mark.parametrize("invalido", ["muitos", -1, True, 1.5])
def test_reasoning_tokens_invalido_vira_none(invalido: object) -> None:
    cliente, _, _ = _cliente([_resposta_em_dict("ok", reasoning=invalido)])
    assert cliente.responder(MENSAGENS).tokens_raciocinio is None


def test_motivo_fim_longo_e_truncado_em_64_caracteres() -> None:
    cliente, _, _ = _cliente([_resposta_em_dict("ok", motivo="x" * 200)])
    assert cliente.responder(MENSAGENS).motivo_fim == "x" * 64


def test_provedor_e_custo_ausentes_ficam_none_sem_inventar_valor() -> None:
    cliente, _, _ = _cliente([_completion(provider=None, cost=None)])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.provedor is None
    assert resposta.custo is None
    assert resposta.texto == "oi, tudo bem"


# --- Repetição e erros ---------------------------------------------------------------------


def test_429_429_e_sucesso_repete_com_esperas_de_2_e_5_segundos() -> None:
    cliente, falso, esperas = _cliente(
        [
            _erro_http(openai.RateLimitError, 429),
            _erro_http(openai.RateLimitError, 429),
            _completion(),
        ]
    )

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "oi, tudo bem"
    assert len(falso.chamadas) == 3
    assert esperas == [2.0, 5.0]


def test_erro_500_esgota_as_tentativas_e_levanta_erro_llm_generico() -> None:
    cliente, falso, esperas = _cliente(
        [_erro_http(openai.InternalServerError, 500) for _ in range(3)]
    )

    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 3
    assert esperas == [1.0, 2.0]
    mensagem = str(erro.value)
    assert "InternalServerError" in mensagem
    assert "status 500" in mensagem
    assert "3 tentativas" in mensagem
    assert CORPO_DO_ERRO not in mensagem
    assert erro.value.__cause__ is None


def test_erro_de_conexao_e_timeout_sao_transitorios() -> None:
    cliente, falso, esperas = _cliente(
        [
            openai.APITimeoutError(REQ),
            openai.APIConnectionError(request=REQ),
            _completion(),
        ]
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert len(falso.chamadas) == 3
    assert esperas == [1.0, 2.0]


def test_max_tentativas_limita_as_repeticoes() -> None:
    cliente, falso, esperas = _cliente(
        [_erro_http(openai.InternalServerError, 500) for _ in range(2)], max_tentativas=2
    )

    with pytest.raises(ErroLLM, match="2 tentativas"):
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 2
    assert esperas == [1.0]


def test_erros_5xx_e_conexao_seguem_esperas_de_1_2_e_4_segundos() -> None:
    cliente, falso, esperas = _cliente(
        [_erro_http(openai.InternalServerError, 503) for _ in range(4)], max_tentativas=4
    )

    with pytest.raises(ErroLLM, match="4 tentativas"):
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 4
    assert esperas == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    ("classe", "status"),
    [(openai.AuthenticationError, 401), (openai.BadRequestError, 400), (openai.NotFoundError, 404)],
)
def test_erros_4xx_de_autenticacao_e_pedido_nao_repetem(
    classe: type[openai.APIStatusError], status: int
) -> None:
    cliente, falso, esperas = _cliente([_erro_http(classe, status), _completion()])

    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 1
    assert esperas == []
    assert classe.__name__ in str(erro.value)
    assert f"status {status}" in str(erro.value)
    assert CORPO_DO_ERRO not in str(erro.value)


def test_excecao_inesperada_vira_erro_llm_sem_repetir() -> None:
    cliente, falso, esperas = _cliente([RuntimeError("detalhe interno")])

    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 1
    assert esperas == []
    assert "RuntimeError" in str(erro.value)
    assert "detalhe interno" not in str(erro.value)


def test_resposta_sem_choices_levanta_erro_llm() -> None:
    cliente, _, _ = _cliente([SimpleNamespace(choices=[], usage=None)])

    with pytest.raises(ErroLLM, match="sem escolhas"):
        cliente.responder(MENSAGENS)


# --- Resposta vazia ------------------------------------------------------------------------


def test_resposta_vazia_seguida_de_boa_devolve_a_boa_com_tokens_e_custo_somados() -> None:
    cliente, falso, esperas = _cliente(
        [_completion(conteudo="", cost=0.0001), _completion(cost=0.0002)]
    )

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "oi, tudo bem"
    assert resposta.tentativas_descartadas == 1
    assert (resposta.tokens_entrada, resposta.tokens_saida) == (240, 16)
    assert resposta.custo == pytest.approx(0.0003)
    assert resposta.provedor == "Novita"
    assert len(falso.chamadas) == 2
    assert esperas == [1.0]


def test_vazio_em_todas_as_tentativas_devolve_a_ultima_vazia_sem_lancar() -> None:
    cliente, falso, esperas = _cliente([_completion(conteudo=None, cost=0.1) for _ in range(3)])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == ""
    assert resposta.tentativas_descartadas == 2
    assert resposta.motivo_fim == "stop"
    assert (resposta.tokens_entrada, resposta.tokens_saida) == (360, 24)
    assert resposta.custo == pytest.approx(0.3)
    assert len(falso.chamadas) == 3
    assert esperas == [1.0, 2.0]
    assert CHAVE_FALSA not in repr(resposta)


def test_vazio_sem_espera_que_caiba_no_prazo_devolve_a_vazia_sem_dormir() -> None:
    relogio = RelogioFalso()
    cliente, falso, esperas = _cliente_no_relogio(
        [_completion(conteudo=""), _completion(conteudo="")],
        relogio,
        timeout_s=0.5,
        prazo_total_s=1.5,
    )

    resposta = cliente.responder(MENSAGENS)

    # A primeira espera (1 s) cabe; a segunda (2 s) não cabe nos 0,5 s restantes.
    assert resposta.texto == ""
    assert resposta.tentativas_descartadas == 1
    assert len(falso.chamadas) == 2
    assert esperas == [1.0]


def test_vazio_com_espera_que_nao_cabe_nem_na_primeira_vez_nao_dorme() -> None:
    relogio = RelogioFalso()
    cliente, falso, esperas = _cliente_no_relogio(
        [_completion(conteudo="")], relogio, timeout_s=0.5, prazo_total_s=0.5
    )

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == ""
    assert resposta.tentativas_descartadas == 0
    assert len(falso.chamadas) == 1
    assert esperas == []


@pytest.mark.parametrize("vazio", ["", "   ", "\n\t ", "​", "﻿ ​"])
def test_conteudo_so_com_espacos_ou_invisiveis_conta_como_vazio(vazio: str) -> None:
    cliente, falso, _ = _cliente([_completion(conteudo=vazio), _completion()])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "oi, tudo bem"
    assert resposta.tentativas_descartadas == 1
    assert len(falso.chamadas) == 2


def test_finish_reason_length_com_texto_nao_repete() -> None:
    cliente, falso, esperas = _cliente([_resposta_em_dict("parcial", motivo="length")])

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "parcial"
    assert resposta.motivo_fim == "length"
    assert resposta.tentativas_descartadas == 0
    assert len(falso.chamadas) == 1
    assert esperas == []


def test_tokens_de_raciocinio_das_tentativas_vazias_sao_somados() -> None:
    cliente, _, _ = _cliente(
        [_resposta_em_dict("", reasoning=4), _resposta_em_dict("ok", reasoning=6)]
    )

    resposta = cliente.responder(MENSAGENS)

    assert resposta.tokens_raciocinio == 10
    assert (resposta.tokens_entrada, resposta.tokens_saida) == (2, 4)


def test_custo_fica_none_quando_nenhuma_tentativa_informou() -> None:
    cliente, _, _ = _cliente([_completion(conteudo="", cost=None), _completion(cost=None)])
    assert cliente.responder(MENSAGENS).custo is None


@pytest.mark.parametrize(
    ("custos", "esperado"),
    [((0.5, None), 0.5), ((None, 0.25), 0.25)],
)
def test_custo_soma_so_as_tentativas_que_informaram(
    custos: tuple[float | None, float | None], esperado: float
) -> None:
    cliente, _, _ = _cliente(
        [_completion(conteudo="", cost=custos[0]), _completion(cost=custos[1])]
    )
    assert cliente.responder(MENSAGENS).custo == pytest.approx(esperado)


def test_erro_transitorio_depois_de_vazio_continua_levantando_erro_llm() -> None:
    cliente, falso, esperas = _cliente(
        [
            _completion(conteudo=""),
            _erro_http(openai.InternalServerError, 500),
            _erro_http(openai.InternalServerError, 500),
        ]
    )

    with pytest.raises(ErroLLM, match="3 tentativas"):
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 3
    assert esperas == [1.0, 2.0]


# --- Parâmetros enviados -------------------------------------------------------------------


def test_parametros_enviados_ao_sdk() -> None:
    cliente, falso, _ = _cliente([_completion()], temperatura=0.2, seed=7, max_tokens_saida=256)

    cliente.responder(MENSAGENS)

    enviado = falso.chamadas[0]
    assert enviado["model"] == "deepseek/deepseek-v4.1-flash"
    assert enviado["temperature"] == 0.2
    assert enviado["seed"] == 7
    assert enviado["max_tokens"] == 256
    assert enviado["messages"] == [
        {"role": "system", "content": "regras"},
        {"role": "user", "content": "oi"},
    ]
    assert enviado["extra_body"] == {
        "provider": {"require_parameters": True, "allow_fallbacks": False}
    }


def test_fallback_desligado_envia_allow_fallbacks_falso() -> None:
    cliente, falso, _ = _cliente([_completion()], fallback_provedor=False)
    cliente.responder(MENSAGENS)
    assert falso.chamadas[0]["extra_body"]["provider"] == {
        "require_parameters": True,
        "allow_fallbacks": False,
    }


def test_fallback_ligado_envia_so_require_parameters() -> None:
    cliente, falso, _ = _cliente([_completion()], fallback_provedor=True)
    cliente.responder(MENSAGENS)
    assert falso.chamadas[0]["extra_body"]["provider"] == {"require_parameters": True}


def test_seed_e_max_tokens_so_vao_quando_definidos() -> None:
    cliente, falso, _ = _cliente([_completion()])
    cliente.responder(MENSAGENS)
    assert "seed" not in falso.chamadas[0]
    assert "max_tokens" not in falso.chamadas[0]
    assert falso.chamadas[0]["temperature"] == 0.0


# --- Chave de API --------------------------------------------------------------------------


def test_chave_nao_aparece_em_mensagem_de_erro_nem_em_repr() -> None:
    erro_com_chave = openai.AuthenticationError(
        f"chave {CHAVE_FALSA} recusada",
        response=httpx2.Response(401, request=REQ),
        body={"key": CHAVE_FALSA},
    )
    cliente, _, _ = _cliente([erro_com_chave])

    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)

    assert CHAVE_FALSA not in str(erro.value)
    assert CHAVE_FALSA not in repr(erro.value)
    assert CHAVE_FALSA not in repr(cliente)
    assert CHAVE_FALSA not in str(cliente)


def test_cliente_sdk_criado_internamente_nao_expoe_a_chave_no_repr() -> None:
    cliente = ClienteOpenRouter(_config(), CHAVE_FALSA)
    assert CHAVE_FALSA not in repr(cliente)
    assert CHAVE_FALSA not in str(cliente)


def test_chave_vazia_sem_cliente_injetado_e_recusada() -> None:
    with pytest.raises(ValueError, match="vazia"):
        ClienteOpenRouter(_config(), "")


# --- Prazo total por chamada ---------------------------------------------------------------


def _cliente_no_relogio(
    roteiro: Sequence[object], relogio: RelogioFalso, **config: Any
) -> tuple[ClienteOpenRouter, ClienteFalso, list[float]]:
    """Como `_cliente`, mas o relógio é falso e cada espera de repetição o faz andar."""
    falso = ClienteFalso(roteiro)
    esperas: list[float] = []

    def dormir(segundos: float) -> None:
        esperas.append(segundos)
        relogio.avancar(segundos)

    cliente = ClienteOpenRouter(
        _config(**config), CHAVE_FALSA, cliente=falso, dormir=dormir, relogio=relogio
    )
    return cliente, falso, esperas


def test_prazo_total_padrao_e_120_segundos() -> None:
    assert _config().prazo_total_s == 120.0


def test_chamada_presa_levanta_erro_de_prazo_dentro_do_prazo_e_sem_repetir(
    liberacoes: list[threading.Event],
) -> None:
    evento = threading.Event()
    liberacoes.append(evento)
    cliente, falso, esperas = _cliente(
        [_Preso(evento, _completion()), _completion()], timeout_s=0.2, prazo_total_s=0.3
    )

    inicio = time.monotonic()
    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)
    decorrido = time.monotonic() - inicio

    assert decorrido < 0.3 + FOLGA_S
    assert "passou do prazo total de 0.3 s" in str(erro.value)
    assert len(falso.chamadas) == 1
    assert esperas == []
    assert erro.value.__cause__ is None
    assert CORPO_DO_ERRO not in str(erro.value)
    assert CHAVE_FALSA not in str(erro.value)


def test_resposta_tardia_depois_do_prazo_e_descartada_sem_efeito(
    liberacoes: list[threading.Event],
) -> None:
    evento = threading.Event()
    liberacoes.append(evento)
    cliente, falso, _ = _cliente(
        [_Preso(evento, _completion(conteudo="tardia")), _completion(conteudo="nova")],
        timeout_s=0.2,
        prazo_total_s=0.3,
    )

    with pytest.raises(ErroLLM, match="prazo total"):
        cliente.responder(MENSAGENS)
    # A resposta tardia chega agora, numa thread já abandonada.
    evento.set()
    _esperar_threads_de_tentativa()

    resposta = cliente.responder(MENSAGENS)

    assert resposta.texto == "nova"
    assert len(falso.chamadas) == 2


def test_timeout_por_tentativa_seguido_de_resposta_dentro_do_prazo_funciona() -> None:
    relogio = RelogioFalso()
    cliente, falso, esperas = _cliente_no_relogio(
        [openai.APITimeoutError(REQ), _completion()],
        relogio,
        timeout_s=0.5,
        prazo_total_s=5.0,
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert len(falso.chamadas) == 2
    assert esperas == [1.0]


def test_timeout_enviado_ao_sdk_e_limitado_pelo_restante_do_prazo() -> None:
    relogio = RelogioFalso()
    cliente, falso, esperas = _cliente_no_relogio(
        [
            _erro_http(openai.RateLimitError, 429, {"retry-after": "2"}),
            _completion(),
        ],
        relogio,
        timeout_s=2.0,
        prazo_total_s=3.5,
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    # Primeira tentativa: prazo cheio. Depois da espera de 2 s, só 1,5 s restam: o timeout encolhe.
    assert [chamada["timeout"] for chamada in falso.chamadas] == [2.0, 1.5]
    assert esperas == [2.0]


def test_espera_de_repeticao_que_nao_cabe_no_prazo_falha_logo() -> None:
    relogio = RelogioFalso()
    cliente, falso, esperas = _cliente_no_relogio(
        [
            _erro_http(openai.RateLimitError, 429),
            _erro_http(openai.RateLimitError, 429),
            _completion(),
        ],
        relogio,
        timeout_s=0.5,
        prazo_total_s=2.5,
    )

    with pytest.raises(ErroLLM, match="passou do prazo total de 2.5 s") as erro:
        cliente.responder(MENSAGENS)

    # A espera de 5 s (terceira tentativa com 429) não cabe nos 0,5 s restantes: não dorme e não
    # tenta de novo. A primeira espera de 2 s coube.
    assert len(falso.chamadas) == 2
    assert esperas == [2.0]
    assert CORPO_DO_ERRO not in str(erro.value)


def test_erro_de_prazo_nao_cita_corpo_de_resposta_nem_chave(
    liberacoes: list[threading.Event],
) -> None:
    evento = threading.Event()
    liberacoes.append(evento)
    cliente, _, _ = _cliente(
        [_Preso(evento, _erro_http(openai.InternalServerError, 500))],
        timeout_s=0.2,
        prazo_total_s=0.3,
    )

    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)

    assert CORPO_DO_ERRO not in str(erro.value)
    assert CHAVE_FALSA not in str(erro.value)
    assert erro.value.__cause__ is None


def test_erro_inesperado_na_thread_chega_ao_chamador_como_erro_llm_sem_repetir() -> None:
    cliente, falso, esperas = _cliente([RuntimeError("detalhe interno")])

    with pytest.raises(ErroLLM, match="RuntimeError"):
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 1
    assert esperas == []


def test_prazo_total_menor_que_timeout_e_recusado() -> None:
    with pytest.raises(ValueError, match="prazo_total_s"):
        ConfigModelo(slug="x", timeout_s=60.0, prazo_total_s=30.0)


# --- Limite de taxa (429) -----------------------------------------------------------------


def test_429_sem_retry_after_segue_a_escada_e_cinco_tentativas_no_total() -> None:
    cliente, falso, esperas = _cliente(
        [_erro_http(openai.RateLimitError, 429) for _ in range(4)] + [_completion()]
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert len(falso.chamadas) == 5
    assert esperas == [2.0, 5.0, 10.0, 20.0]


def test_429_esgotado_levanta_erro_generico_com_cinco_tentativas() -> None:
    cliente, falso, esperas = _cliente([_erro_http(openai.RateLimitError, 429) for _ in range(5)])

    with pytest.raises(ErroLLM) as erro:
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 5
    assert esperas == [2.0, 5.0, 10.0, 20.0]
    mensagem = str(erro.value)
    assert "RateLimitError" in mensagem
    assert "status 429" in mensagem
    assert "5 tentativas" in mensagem
    assert CORPO_DO_ERRO not in mensagem
    assert CHAVE_FALSA not in mensagem
    assert erro.value.__cause__ is None


def test_429_nao_e_limitado_por_max_tentativas_mas_segue_o_teto_de_cinco() -> None:
    cliente, falso, esperas = _cliente(
        [_erro_http(openai.RateLimitError, 429), _completion()], max_tentativas=1
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert len(falso.chamadas) == 2
    assert esperas == [2.0]


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("3", 3.0), ("1.5", 1.5), (" 4 ", 4.0), ("0", 0.0)],
)
def test_429_respeita_retry_after_em_segundos(valor: str, esperado: float) -> None:
    cliente, falso, esperas = _cliente(
        [_erro_http(openai.RateLimitError, 429, {"retry-after": valor}), _completion()]
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert esperas == [esperado]


def test_429_retry_after_acima_de_30_segundos_e_limitado_a_30() -> None:
    relogio = RelogioFalso()
    cliente, _, esperas = _cliente_no_relogio(
        [_erro_http(openai.RateLimitError, 429, {"retry-after": "45"}), _completion()],
        relogio,
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert esperas == [30.0]


@pytest.mark.parametrize(
    "invalido",
    ["-1", "abc", "", "nan", "inf", "1e3", "1,5", "Wed, 21 Oct 2026 07:28:00 GMT"],
)
def test_429_retry_after_invalido_e_ignorado_e_usa_a_escada(invalido: str) -> None:
    cliente, _, esperas = _cliente(
        [_erro_http(openai.RateLimitError, 429, {"retry-after": invalido}), _completion()]
    )

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert esperas == [2.0]


def test_429_retry_after_que_nao_cabe_no_prazo_falha_logo_sem_dormir() -> None:
    relogio = RelogioFalso()
    cliente, falso, esperas = _cliente_no_relogio(
        [_erro_http(openai.RateLimitError, 429, {"retry-after": "10"}), _completion()],
        relogio,
        timeout_s=0.5,
        prazo_total_s=2.5,
    )

    with pytest.raises(ErroLLM, match="passou do prazo total de 2.5 s"):
        cliente.responder(MENSAGENS)

    assert len(falso.chamadas) == 1
    assert esperas == []


@pytest.mark.parametrize("resposta", [None, SimpleNamespace(headers=None), SimpleNamespace()])
def test_429_sem_cabecalhos_utilizaveis_nao_levanta_e_usa_a_escada(resposta: object) -> None:
    erro = _erro_http(openai.RateLimitError, 429)
    alvo: Any = erro
    alvo.response = resposta
    cliente, _, esperas = _cliente([erro, _completion()])

    assert cliente.responder(MENSAGENS).texto == "oi, tudo bem"
    assert esperas == [2.0]


# --- Provedor fixo -----------------------------------------------------------------------


def test_com_provedor_o_corpo_fixa_a_ordem_sem_fallback() -> None:
    cliente, falso, _ = _cliente([_completion()], provedor="Wafer")

    cliente.responder(MENSAGENS)

    assert falso.chamadas[0]["extra_body"] == {
        "provider": {
            "order": ["Wafer"],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }


def test_com_provedor_o_fallback_fica_desligado_mesmo_com_fallback_ligado() -> None:
    cliente, falso, _ = _cliente([_completion()], provedor="Wafer", fallback_provedor=True)

    cliente.responder(MENSAGENS)

    assert falso.chamadas[0]["extra_body"]["provider"] == {
        "order": ["Wafer"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_sem_provedor_o_roteamento_continua_o_de_antes() -> None:
    cliente, falso, _ = _cliente([_completion(), _completion()])
    cliente.responder(MENSAGENS)

    cliente_fallback, falso_fallback, _ = _cliente([_completion()], fallback_provedor=True)
    cliente_fallback.responder(MENSAGENS)

    assert falso.chamadas[0]["extra_body"] == {
        "provider": {"require_parameters": True, "allow_fallbacks": False}
    }
    assert falso_fallback.chamadas[0]["extra_body"] == {"provider": {"require_parameters": True}}


def test_provedor_efetivo_diferente_do_pedido_registra_aviso_sem_levantar(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cliente, _, _ = _cliente([_completion(provider="Tenstorrent")], provedor="Wafer")

    with caplog.at_level("WARNING", logger="harness.llm.openrouter"):
        resposta = cliente.responder(MENSAGENS)

    assert resposta.provedor == "Tenstorrent"
    assert "Tenstorrent" in caplog.text
    assert "Wafer" in caplog.text
    assert CHAVE_FALSA not in caplog.text


@pytest.mark.parametrize("efetivo", ["wafer", "WAFER"])
def test_provedor_igual_sem_diferenca_de_caixa_nao_registra_aviso(
    caplog: pytest.LogCaptureFixture, efetivo: str
) -> None:
    cliente, _, _ = _cliente([_completion(provider=efetivo)], provedor="Wafer")

    with caplog.at_level("WARNING", logger="harness.llm.openrouter"):
        cliente.responder(MENSAGENS)

    assert caplog.text == ""


def test_provedor_ausente_na_resposta_nao_registra_aviso(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cliente, _, _ = _cliente([_completion(provider=None)], provedor="Wafer")

    with caplog.at_level("WARNING", logger="harness.llm.openrouter"):
        resposta = cliente.responder(MENSAGENS)

    assert resposta.provedor is None
    assert caplog.text == ""
