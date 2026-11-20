"""Testes do trace JSONL (Tarefa 8): formato, sequência, falhas de I/O e leitura de volta.

Nenhum teste aqui usa Docker nem o orquestrador: o TraceJsonl recebe Evento direto.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pytest

from harness.orchestrator.loop import Evento
from harness.telemetry.trace import TraceJsonl, ler_trace

INSTANTE = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def _relogio_crescente() -> Callable[[], datetime]:
    passos = iter(range(10_000))

    def agora() -> datetime:
        return INSTANTE + timedelta(seconds=next(passos))

    return agora


def _eventos(quantidade: int) -> list[Evento]:
    return [Evento(tipo=f"tipo_{n}", dados={"n": n}) for n in range(quantidade)]


def test_cada_evento_vira_uma_linha_json_valida(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path, session_id="sessao")
    for evento in _eventos(3):
        trace(evento)

    linhas = trace.caminho.read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 3
    for linha in linhas:
        registro = json.loads(linha)
        assert set(registro) == {"seq", "ts", "session_id", "tipo", "dados"}


def test_caminho_fica_no_diretorio_com_o_session_id(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path / "traces", session_id="abc123")

    assert trace.caminho == tmp_path / "traces" / "abc123.jsonl"
    assert (tmp_path / "traces").is_dir()


def test_session_id_padrao_e_um_uuid_hex(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path)
    trace(Evento(tipo="x", dados={}))

    session_id = trace.caminho.stem
    assert len(session_id) == 32
    assert int(session_id, 16) >= 0
    assert ler_trace(trace.caminho)[0]["session_id"] == session_id


def test_seq_comeca_em_um_e_cresce_sem_buracos(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path, session_id="s")
    for evento in _eventos(5):
        trace(evento)

    registros = ler_trace(trace.caminho)
    assert [r["seq"] for r in registros] == [1, 2, 3, 4, 5]


def test_ts_e_iso_utc_e_session_id_e_igual_em_todas_as_linhas(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path, session_id="sessao-x", relogio=_relogio_crescente())
    for evento in _eventos(3):
        trace(evento)

    registros = ler_trace(trace.caminho)
    instantes = [datetime.fromisoformat(r["ts"]) for r in registros]
    assert all(instante.utcoffset() == timedelta(0) for instante in instantes)
    assert instantes == sorted(instantes)
    assert {r["session_id"] for r in registros} == {"sessao-x"}


def test_acentos_ficam_literais_no_arquivo(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path, session_id="s")
    trace(Evento(tipo="observacao", dados={"texto": "ação é ótimo"}))

    bruto = trace.caminho.read_text(encoding="utf-8")
    assert "ação é ótimo" in bruto
    assert "\\u" not in bruto
    assert ler_trace(trace.caminho)[0]["dados"]["texto"] == "ação é ótimo"


def test_falha_de_io_com_diretorio_que_virou_arquivo_nao_levanta(tmp_path: Path) -> None:
    bloqueio = tmp_path / "bloqueio"
    bloqueio.write_text("sou um arquivo", encoding="utf-8")
    trace = TraceJsonl(bloqueio / "traces", session_id="s")

    trace(Evento(tipo="x", dados={}))
    trace(Evento(tipo="y", dados={}))

    assert trace.falhas_de_escrita == 2


def test_falha_no_open_e_contada_e_o_seq_continua_avancando(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = TraceJsonl(tmp_path, session_id="s")
    abertura_original = Path.open
    chamadas = {"n": 0}

    def open_que_falha_na_primeira(self: Path, *args: Any, **kwargs: Any) -> IO[Any]:
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            raise OSError("disco cheio")
        arquivo: IO[Any] = abertura_original(self, *args, **kwargs)
        return arquivo

    monkeypatch.setattr(Path, "open", open_que_falha_na_primeira)
    trace(Evento(tipo="perdido", dados={}))
    trace(Evento(tipo="gravado", dados={}))

    assert trace.falhas_de_escrita == 1
    registros = ler_trace(trace.caminho)
    assert len(registros) == 1
    assert registros[0]["tipo"] == "gravado"
    assert registros[0]["seq"] == 2


def test_falha_de_io_em_todas_as_escritas_nunca_levanta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def open_quebrado(self: Path, *args: object, **kwargs: object) -> object:
        raise PermissionError("sem permissão")

    monkeypatch.setattr(Path, "open", open_quebrado)
    trace = TraceJsonl(tmp_path, session_id="s")
    for evento in _eventos(4):
        trace(evento)

    assert trace.falhas_de_escrita == 4


def test_objeto_nao_serializavel_vira_repr_sem_quebrar(tmp_path: Path) -> None:
    class Opaco:
        def __repr__(self) -> str:
            return "<Opaco>"

    trace = TraceJsonl(tmp_path, session_id="s")
    trace(Evento(tipo="x", dados={"objeto": Opaco(), "ok": 1}))

    registro = ler_trace(trace.caminho)[0]
    assert registro["dados"] == {"objeto": "<Opaco>", "ok": 1}
    assert trace.falhas_de_escrita == 0


def test_ler_trace_faz_round_trip_na_ordem_gravada(tmp_path: Path) -> None:
    trace = TraceJsonl(tmp_path, session_id="s")
    eventos = [
        Evento(tipo="pedido_iniciado", dados={"pedido": "quanto vendi?", "orcamento": {}}),
        Evento(tipo="execucao", dados={"cell_id": 1, "ferramentas": {"catalogo.listar": 1}}),
        Evento(tipo="pedido_finalizado", dados={"motivo": "final", "completo": True}),
    ]
    for evento in eventos:
        trace(evento)

    registros = ler_trace(trace.caminho)
    assert [r["tipo"] for r in registros] == [e.tipo for e in eventos]
    assert [r["dados"] for r in registros] == [e.dados for e in eventos]
