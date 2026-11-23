"""Testes do cliente OpenRouter com ferramentas nativas (T18a), com um cliente falso do SDK.

Nenhum teste aqui usa rede: o `cliente` é injetado e `dormir` só registra as esperas.
"""

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from harness.llm.base import ChamadaFerramenta, ErroLLM, Mensagem
from harness.llm.config import ConfigModelo
from harness.llm.ferramentas import EsquemaFerramenta
from harness.llm.openrouter import ClienteOpenRouter

REQ = httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
CHAVE_FALSA = "chave-falsa-de-teste-123"
ESQUEMA = EsquemaFerramenta(
    nome="catalogo__listar",
    descricao="Lista os arquivos.",
    parametros={"type": "object", "properties": {}},
)
MENSAGENS = [Mensagem(role="user", content="oi")]


class ClienteFalso:
    """Imita `client.chat.completions.create`: devolve ou levanta o roteiro, em ordem."""

    def __init__(self, roteiro: Sequence[object]) -> None:
        self._roteiro = list(roteiro)
        self.chamadas: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._criar))

    def _criar(self, **kwargs: Any) -> Any:
        self.chamadas.append(kwargs)
        item = self._roteiro.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _completion(
    *,
    conteudo: str | None = None,
    chamadas: list[dict[str, Any]] | None = None,
    finish: str = "tool_calls",
    cost: float | None = 0.001,
    tokens_entrada: int = 10,
    tokens_saida: int = 5,
) -> ChatCompletion:
    mensagem: dict[str, Any] = {"role": "assistant", "content": conteudo}
    if chamadas is not None:
        mensagem["tool_calls"] = chamadas
    uso: dict[str, Any] = {
        "prompt_tokens": tokens_entrada,
        "completion_tokens": tokens_saida,
        "total_tokens": tokens_entrada + tokens_saida,
    }
    if cost is not None:
        uso["cost"] = cost
    dados: dict[str, Any] = {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek/deepseek-v4.1-flash-2026-10-17",
        "provider": "Wafer",
        "choices": [{"index": 0, "finish_reason": finish, "message": mensagem}],
        "usage": uso,
    }
    return ChatCompletion.model_validate(dados)


def _chamada(identificador: str, nome: str, arguments: object) -> dict[str, Any]:
    return {
        "id": identificador,
        "type": "function",
        "function": {"name": nome, "arguments": arguments},
    }


def _config(**kwargs: Any) -> ConfigModelo:
    return ConfigModelo(slug="modelo-de-teste", **kwargs)


def _cliente(roteiro: Sequence[object], dormidas: list[float], **config: Any) -> ClienteOpenRouter:
    return ClienteOpenRouter(
        _config(**config),
        CHAVE_FALSA,
        cliente=ClienteFalso(roteiro),
        dormir=dormidas.append,
        relogio=lambda: 0.0,
    )


def _sdk(cliente: ClienteOpenRouter) -> ClienteFalso:
    sdk = cliente._cliente  # noqa: SLF001 - o teste inspeciona o que foi enviado ao SDK
    assert isinstance(sdk, ClienteFalso)
    return sdk


def test_chamadas_sao_lidas_em_ordem_com_argumentos_em_texto() -> None:
    cliente = _cliente(
        [
            _completion(
                chamadas=[
                    _chamada("c1", "catalogo__listar", '{"departamento": "vendas"}'),
                    _chamada("c2", "arquivos__ler_planilha", "{}"),
                ]
            )
        ],
        [],
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    assert resposta.texto == ""
    assert resposta.chamadas_ferramenta == (
        ChamadaFerramenta(
            id="c1", nome="catalogo__listar", argumentos_brutos='{"departamento": "vendas"}'
        ),
        ChamadaFerramenta(id="c2", nome="arquivos__ler_planilha", argumentos_brutos="{}"),
    )
    assert resposta.motivo_fim == "tool_calls"


def test_content_none_com_chamadas_nao_e_resposta_vazia_e_nao_repete() -> None:
    dormidas: list[float] = []
    cliente = _cliente(
        [_completion(conteudo=None, chamadas=[_chamada("c1", "catalogo__listar", "{}")])], dormidas
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    assert len(resposta.chamadas_ferramenta) == 1
    assert resposta.tentativas_descartadas == 0
    assert len(_sdk(cliente).chamadas) == 1
    assert dormidas == []


def test_resposta_sem_texto_e_sem_chamadas_repete_como_antes() -> None:
    dormidas: list[float] = []
    cliente = _cliente(
        [
            _completion(conteudo=None, chamadas=None, finish="stop"),
            _completion(conteudo="ok", finish="stop"),
        ],
        dormidas,
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    assert resposta.texto == "ok"
    assert resposta.tentativas_descartadas == 1
    assert dormidas == [1.0]


def _resposta_crua(chamadas: list[dict[str, Any]]) -> dict[str, Any]:
    """Resposta em dict, como um provedor malformado devolve: o SDK tipado a recusaria antes."""
    return {
        "model": "modelo-de-teste",
        "provider": "Wafer",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": chamadas},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def test_argumentos_invalidos_nao_levantam_excecao() -> None:
    cliente = _cliente(
        [
            _resposta_crua(
                [
                    _chamada("c1", "catalogo__listar", "{nao e json"),
                    _chamada("c2", "catalogo__listar", {"departamento": "vendas"}),
                    _chamada("c3", "catalogo__listar", None),
                ]
            )
        ],
        [],
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    brutos = [chamada.argumentos_brutos for chamada in resposta.chamadas_ferramenta]
    assert brutos[0] == "{nao e json"
    assert json.loads(brutos[1]) == {"departamento": "vendas"}
    assert brutos[2] == ""


def test_id_ou_nome_ausente_ganham_valor_seguro() -> None:
    cliente = _cliente(
        [_resposta_crua([{"type": "function", "function": {"arguments": "{}"}}])], []
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    chamada = resposta.chamadas_ferramenta[0]
    assert chamada.id == "chamada_1"
    assert chamada.nome == ""


def test_requisicao_leva_tools_e_tool_choice_com_o_provedor_fixo() -> None:
    cliente = _cliente(
        [_completion(chamadas=[_chamada("c1", "catalogo__listar", "{}")])], [], provedor="Wafer"
    )
    cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA], escolha="auto")
    enviado = _sdk(cliente).chamadas[0]
    assert enviado["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "catalogo__listar",
                "description": "Lista os arquivos.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    assert enviado["tool_choice"] == "auto"
    provedor = enviado["extra_body"]["provider"]
    assert provedor == {"order": ["Wafer"], "allow_fallbacks": False, "require_parameters": True}


def test_escolha_none_vai_para_tool_choice() -> None:
    cliente = _cliente([_completion(conteudo="resumo", finish="stop")], [])
    cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA], escolha="none")
    assert _sdk(cliente).chamadas[0]["tool_choice"] == "none"


def test_responder_sem_ferramentas_nao_envia_tools() -> None:
    cliente = _cliente([_completion(conteudo="oi", finish="stop")], [])
    cliente.responder(MENSAGENS)
    enviado = _sdk(cliente).chamadas[0]
    assert "tools" not in enviado
    assert "tool_choice" not in enviado


def test_historico_com_chamadas_e_resultados_vai_no_formato_da_api() -> None:
    cliente = _cliente([_completion(conteudo="ok", finish="stop")], [])
    historico = [
        Mensagem(role="user", content="pedido"),
        Mensagem(
            role="assistant",
            content="",
            chamadas_ferramenta=(
                ChamadaFerramenta(id="c1", nome="catalogo__listar", argumentos_brutos="{}"),
            ),
        ),
        Mensagem(role="tool", content="resultado", tool_call_id="c1", name="catalogo__listar"),
    ]
    cliente.responder_com_ferramentas(historico, [ESQUEMA])
    mensagens = _sdk(cliente).chamadas[0]["messages"]
    assert mensagens[1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "catalogo__listar", "arguments": "{}"},
            }
        ],
    }
    assert mensagens[2] == {"role": "tool", "tool_call_id": "c1", "content": "resultado"}


def test_tokens_e_custo_das_tentativas_vazias_entram_na_resposta_final() -> None:
    dormidas: list[float] = []
    cliente = _cliente(
        [
            _completion(
                conteudo=None, finish="stop", tokens_entrada=10, tokens_saida=5, cost=0.001
            ),
            _completion(
                chamadas=[_chamada("c1", "catalogo__listar", "{}")],
                tokens_entrada=20,
                tokens_saida=7,
                cost=0.002,
            ),
        ],
        dormidas,
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    assert resposta.tokens_entrada == 30
    assert resposta.tokens_saida == 12
    assert resposta.custo == pytest.approx(0.003)
    assert resposta.tentativas_descartadas == 1


def test_erro_http_em_ferramentas_levanta_erro_llm_sem_corpo() -> None:
    resposta_http = httpx2.Response(500, request=REQ)
    erro = openai.InternalServerError("corpo secreto", response=resposta_http, body=None)
    dormidas: list[float] = []
    cliente = _cliente([erro, erro, erro], dormidas, max_tentativas=3)
    with pytest.raises(ErroLLM) as capturado:
        cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    assert "corpo secreto" not in str(capturado.value)
    assert "status 500" in str(capturado.value)


def test_limite_de_taxa_repete_com_as_mesmas_esperas_no_caminho_de_ferramentas() -> None:
    resposta_http = httpx2.Response(429, request=REQ)
    erro = openai.RateLimitError("limitado", response=resposta_http, body=None)
    dormidas: list[float] = []
    cliente = _cliente(
        [erro, _completion(chamadas=[_chamada("c1", "catalogo__listar", "{}")])], dormidas
    )
    resposta = cliente.responder_com_ferramentas(MENSAGENS, [ESQUEMA])
    assert len(resposta.chamadas_ferramenta) == 1
    assert dormidas == [2.0]
