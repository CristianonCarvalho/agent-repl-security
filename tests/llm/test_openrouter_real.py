"""Teste real do OpenRouter: UMA chamada mínima, excluída por padrão (marcador `real_api`).

Para rodar: `uv run pytest -m real_api`. Gasta tokens do OpenRouter. Pula se faltar
`OPENROUTER_API_KEY` ou `OPENROUTER_MODELO`, lidos do ambiente ou do `.env` da raiz (o ambiente
tem precedência). Nenhum valor de chave é impresso.
"""

import os
from pathlib import Path

import pytest

from harness.llm.base import Mensagem
from harness.llm.config import carregar_env, config_de_ambiente
from harness.llm.openrouter import ClienteOpenRouter

RAIZ = Path(__file__).resolve().parents[2]


@pytest.mark.real_api
def test_chamada_minima_ao_openrouter_devolve_texto_e_tokens() -> None:
    ambiente = {**carregar_env(RAIZ / ".env"), **os.environ}
    chave = ambiente.get("OPENROUTER_API_KEY", "")
    if not chave or not ambiente.get("OPENROUTER_MODELO", "").strip():
        pytest.skip("defina OPENROUTER_API_KEY e OPENROUTER_MODELO (ambiente ou .env)")

    config = config_de_ambiente(ambiente).model_copy(update={"max_tokens_saida": 32})
    cliente = ClienteOpenRouter(config, chave)

    resposta = cliente.responder([Mensagem(role="user", content="Responda apenas: ok")])

    assert resposta.texto.strip() != ""
    assert resposta.tokens_entrada > 0
    assert resposta.tokens_saida > 0
