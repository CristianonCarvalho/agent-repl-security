"""Testes do cliente MCP stdio contra o servidor falso (sem rede, sem Docker)."""

import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from harness.host.mcp.cliente import ClienteMcp, ErroMcp
from harness.host.mcp.config import ServidorMcp
from tests.host.mcp.auxiliares import SERVIDOR_FALSO, ler_pid, processo_morto


def _servidor(
    modo: str,
    *,
    pid: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_chamada_s: float = 5.0,
    timeout_inicio_s: float = 5.0,
) -> ServidorMcp:
    args = [str(SERVIDOR_FALSO), modo]
    if pid is not None:
        args.append(str(pid))
    return ServidorMcp(
        nome="falso",
        comando=sys.executable,
        args=args,
        env=env or {},
        timeout_chamada_s=timeout_chamada_s,
        timeout_inicio_s=timeout_inicio_s,
    )


@pytest.fixture
def abrir() -> Iterator[Callable[..., ClienteMcp]]:
    """Abre clientes para o teste e garante que todos sejam fechados no fim."""
    abertos: list[ClienteMcp] = []

    def criar(modo: str, **kwargs: Any) -> ClienteMcp:
        cliente = ClienteMcp(_servidor(modo, **kwargs))
        abertos.append(cliente)
        return cliente

    yield criar
    for cliente in abertos:
        cliente.fechar()


# --- Caminho feliz ------------------------------------------------------------------


def test_handshake_e_listagem_das_ferramentas(abrir: Callable[..., ClienteMcp]) -> None:
    cliente = abrir("normal")
    nomes = [descritor.nome for descritor in cliente.listar_ferramentas()]
    assert nomes[:3] == ["eco", "ambiente", "erro"]
    eco = next(d for d in cliente.listar_ferramentas() if d.nome == "eco")
    assert eco.descricao == "Devolve os argumentos recebidos."
    assert eco.esquema is not None
    assert eco.esquema["required"] == ["texto"]


def test_listagem_segue_a_paginacao_por_next_cursor(abrir: Callable[..., ClienteMcp]) -> None:
    cliente = abrir("paginado")
    assert [d.nome for d in cliente.listar_ferramentas()] == ["eco", "ambiente", "erro"]


def test_cursor_repetido_falha_em_vez_de_girar_para_sempre(
    abrir: Callable[..., ClienteMcp],
) -> None:
    cliente = abrir("paginado_em_loop")
    with pytest.raises(ErroMcp, match="repetiu o cursor"):
        cliente.listar_ferramentas()


def test_chamada_devolve_o_resultado_e_ignora_notificacoes(
    abrir: Callable[..., ClienteMcp],
) -> None:
    cliente = abrir("normal")
    resultado = cliente.chamar("eco", {"texto": "oi"})
    # Só o que foi enviado chega ao servidor: o padrão `vezes` não aparece.
    assert resultado == {"content": [{"type": "text", "text": '{"texto": "oi"}'}]}


def test_is_error_volta_no_resultado_cru(abrir: Callable[..., ClienteMcp]) -> None:
    resultado = abrir("normal").chamar("erro", {})
    assert resultado["isError"] is True
    assert resultado["content"][0]["text"] == "falhou de propósito"


def test_pedido_do_servidor_e_recusado_com_method_not_found(
    abrir: Callable[..., ClienteMcp],
) -> None:
    resultado = abrir("pedido_do_servidor").chamar("qualquer", {})
    assert resultado["content"][0]["text"] == "resposta: -32601"


def test_ambiente_minimo_nao_herda_o_ambiente_do_harness(
    abrir: Callable[..., ClienteMcp], monkeypatch: pytest.MonkeyPatch
) -> None:
    chave_falsa = "chave-falsa-de-teste"
    monkeypatch.setenv("OPENROUTER_API_KEY", chave_falsa)
    monkeypatch.setenv("VARIAVEL_DO_HARNESS", "1")
    cliente = abrir("normal", env={"EXTRA_TESTE": "sim"})

    texto = cliente.chamar("ambiente", {})["content"][0]["text"]
    nomes = json.loads(texto)

    assert "OPENROUTER_API_KEY" not in nomes
    assert "VARIAVEL_DO_HARNESS" not in nomes
    assert chave_falsa not in texto
    assert "EXTRA_TESTE" in nomes
    assert "PATH" in nomes


# --- Violações do protocolo: falham e matam o processo -----------------------------


def test_linha_que_nao_e_json_falha_o_inicio_e_mata_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    with pytest.raises(ErroMcp, match="não é JSON"):
        ClienteMcp(_servidor("linha_nao_json", pid=pid))
    assert processo_morto(ler_pid(pid))


def test_resposta_com_id_errado_falha_e_mata_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    with pytest.raises(ErroMcp, match="id inesperado"):
        ClienteMcp(_servidor("id_errado", pid=pid))
    assert processo_morto(ler_pid(pid))


def test_eof_no_meio_da_chamada_falha_e_mata_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    cliente = ClienteMcp(_servidor("eof_na_chamada", pid=pid))
    with pytest.raises(ErroMcp, match="encerrou antes de responder"):
        cliente.chamar("eco", {"texto": "oi"})
    assert processo_morto(ler_pid(pid))


def test_servidor_que_sai_no_inicio_falha_sem_travar(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    with pytest.raises(ErroMcp):
        ClienteMcp(_servidor("falha_inicio", pid=pid))
    assert processo_morto(ler_pid(pid))


def test_lista_de_ferramentas_invalida_falha(abrir: Callable[..., ClienteMcp]) -> None:
    cliente = abrir("lista_invalida")
    with pytest.raises(ErroMcp, match="sem a lista de ferramentas"):
        cliente.listar_ferramentas()


def test_travamento_na_chamada_vira_timeout_e_mata_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    cliente = ClienteMcp(_servidor("trava", pid=pid, timeout_chamada_s=0.5))
    with pytest.raises(ErroMcp, match="não respondeu a tempo"):
        cliente.chamar("eco", {"texto": "oi"})
    assert processo_morto(ler_pid(pid))


def test_travamento_no_inicio_vira_timeout_e_mata_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    with pytest.raises(ErroMcp, match="não respondeu a tempo"):
        ClienteMcp(_servidor("trava_inicio", pid=pid, timeout_inicio_s=0.5))
    assert processo_morto(ler_pid(pid))


def test_linha_acima_de_4_mib_falha_e_mata_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    cliente = ClienteMcp(_servidor("linha_grande", pid=pid))
    with pytest.raises(ErroMcp, match="acima de"):
        cliente.chamar("eco", {"texto": "oi"})
    assert processo_morto(ler_pid(pid))


def test_erro_jsonrpc_numa_requisicao_valida_nao_fecha_o_cliente(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    cliente = ClienteMcp(_servidor("erro_jsonrpc", pid=pid))
    try:
        with pytest.raises(ErroMcp, match="recusou a requisição"):
            cliente.chamar("eco", {"texto": "oi"})
        assert not processo_morto(ler_pid(pid))
    finally:
        cliente.fechar()


def test_argumento_que_nao_vira_json_nao_chega_ao_servidor(
    abrir: Callable[..., ClienteMcp],
) -> None:
    cliente = abrir("normal")
    with pytest.raises(ErroMcp, match="não podem ser enviados"):
        cliente.chamar("eco", {"x": object()})


# --- Ciclo de vida -------------------------------------------------------------------


def test_fechar_e_idempotente_e_encerra_o_processo(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    cliente = ClienteMcp(_servidor("normal", pid=pid))
    assert cliente.pid == ler_pid(pid)
    cliente.fechar()
    cliente.fechar()
    assert processo_morto(ler_pid(pid))


def test_chamar_depois_de_fechar_levanta_erro_curto(tmp_path: Path) -> None:
    cliente = ClienteMcp(_servidor("normal", pid=tmp_path / "pid"))
    cliente.fechar()
    with pytest.raises(ErroMcp, match="não está mais disponível"):
        cliente.chamar("eco", {"texto": "oi"})


def test_context_manager_fecha_ao_sair(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    with ClienteMcp(_servidor("normal", pid=pid)) as cliente:
        cliente.chamar("eco", {"texto": "oi"})
    assert processo_morto(ler_pid(pid))


def test_comando_inexistente_falha_com_mensagem_curta() -> None:
    servidor = ServidorMcp(nome="falso", comando="/caminho/que/nao/existe/servidor")
    with pytest.raises(ErroMcp, match="não foi possível iniciar") as erro:
        ClienteMcp(servidor)
    assert "/caminho" not in str(erro.value)
