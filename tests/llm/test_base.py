"""Testes da interface de LLM e do LLMFalso (Tarefa 7)."""

import pytest

from harness.llm.base import LLMFalso, Mensagem, RespostaLLM, RoteiroEsgotado


def _mensagens() -> list[Mensagem]:
    # 40 + 20 = 60 caracteres no total: 60 // 4 = 15 tokens de entrada estimados.
    return [Mensagem(role="system", content="a" * 40), Mensagem(role="user", content="b" * 20)]


def test_roteiro_devolve_as_respostas_em_ordem() -> None:
    falso = LLMFalso(["primeira", "segunda"])
    assert falso.responder(_mensagens()).texto == "primeira"
    assert falso.responder(_mensagens()).texto == "segunda"


def test_texto_do_roteiro_estima_tokens_de_saida_e_de_entrada() -> None:
    resposta = LLMFalso(["x" * 9]).responder(_mensagens())
    # Saída: 9 // 4 + 1 = 3. Entrada: soma do conteúdo das mensagens // 4 = 15.
    assert resposta.tokens_saida == 3
    assert resposta.tokens_entrada == 15


def test_resposta_llm_do_roteiro_mantem_os_tokens_informados() -> None:
    roteiro = [RespostaLLM(texto="ok", tokens_entrada=7, tokens_saida=3)]
    resposta = LLMFalso(roteiro).responder(_mensagens())
    assert (resposta.tokens_entrada, resposta.tokens_saida) == (7, 3)


def test_chamadas_guardam_copia_das_mensagens_recebidas() -> None:
    falso = LLMFalso(["a", "b"])
    mensagens = _mensagens()
    falso.responder(mensagens)
    # Mudança posterior na lista do chamador não altera o registro da chamada.
    mensagens.append(Mensagem(role="user", content="depois"))
    falso.responder(mensagens)
    assert [m.content for m in falso.chamadas[0]] == ["a" * 40, "b" * 20]
    assert len(falso.chamadas[1]) == 3


def test_roteiro_esgotado_levanta_erro_e_ainda_registra_a_chamada() -> None:
    falso = LLMFalso([])
    with pytest.raises(RoteiroEsgotado):
        falso.responder(_mensagens())
    assert len(falso.chamadas) == 1


def test_mensagem_recusa_papel_desconhecido() -> None:
    with pytest.raises(ValueError):
        Mensagem(role="ferramenta", content="x")  # type: ignore[arg-type]


def test_resposta_recusa_tokens_negativos() -> None:
    with pytest.raises(ValueError):
        RespostaLLM(texto="x", tokens_entrada=-1, tokens_saida=0)
