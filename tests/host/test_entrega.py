"""Testes da ferramenta `entrega.publicar` no host."""

import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from harness.host.aprovacao import (
    PedidoDeAprovacao,
    RespostaDeAprovacao,
    sempre_aprova,
    sempre_recusa,
)
from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import Nivel, Registro
from harness.host.tools.entrega import LIMITE_BYTES, registrar
from harness.sandbox.protocol import ToolCall, ToolResult

_DATA_FIXA = datetime(2026, 11, 27, 14, 30, 0, tzinfo=UTC)


def _perfil() -> Perfil:
    return perfil_por_nome("Vendas").com_concessoes(frozenset({"entrega.*"}))


def _ler_falso(arquivos: dict[str, bytes]) -> Callable[[str, int], bytes]:
    def ler_saida(nome: str, limite_bytes: int) -> bytes:
        assert limite_bytes == LIMITE_BYTES
        return arquivos[nome]

    return ler_saida


def _zip_em_bytes(membros: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for nome, conteudo in membros.items():
            zf.writestr(nome, conteudo)
    return buf.getvalue()


def _chamar(registro: Registro, **args: Any) -> ToolResult:
    return registro.despachar(
        ToolCall(call_id="c1", name="entrega.publicar", args=args),
        _perfil(),
    )


def test_entrega_feliz_csv(tmp_path: Path) -> None:
    conteudo = b"pedido,valor\n1,100\n"
    registro = Registro(aprovador=sempre_aprova())
    pasta = tmp_path / "entregas"
    registrar(registro, _ler_falso({"relatorio.csv": conteudo}), pasta, agora=lambda: _DATA_FIXA)

    ferramenta = registro.obter("entrega.publicar")
    assert ferramenta is not None
    assert ferramenta.nivel is Nivel.ESCRITA_EXTERNA
    assert ferramenta.canal_externo is True
    assert ferramenta.dado_privado is False
    assert ferramenta.conteudo_nao_confiavel is False

    resultado = _chamar(registro, caminho="/saida/relatorio.csv", destinatario="vendas")
    assert resultado.ok is True, resultado.error
    esperado_relativo = "vendas/20261127-143000-relatorio.csv"
    esperado_hash = hashlib.sha256(conteudo).hexdigest()
    assert resultado.data == {
        "entregue": True,
        "arquivo": esperado_relativo,
        "sha256": esperado_hash,
    }

    gravado = pasta / esperado_relativo
    assert gravado.read_bytes() == conteudo

    linhas = (pasta / "registro.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 1
    item = json.loads(linhas[0])
    assert item == {
        "data": _DATA_FIXA.isoformat(timespec="seconds"),
        "arquivo": esperado_relativo,
        "destinatario": "vendas",
        "sha256": esperado_hash,
        "perfil": "Vendas",
    }


@pytest.mark.parametrize(
    "caminho",
    [
        "/saida/../segredo.csv",
        "/saida/sub/relatorio.csv",
        "/dados/vendas/pedidos.csv",
        "/saida/",
        "/saida/relatorio final.csv",
        "/saida/relatorio@1.csv",
        "relatorio.csv",
    ],
)
def test_caminho_invalido_e_recusado(tmp_path: Path, caminho: str) -> None:
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({"relatorio.csv": b"ok"}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho=caminho, destinatario="vendas")
    assert resultado.ok is False


@pytest.mark.parametrize("nome_hostil", ["a\nb.csv", "a\x1b[31mb.csv"])
def test_nome_hostil_com_quebra_ou_escape_e_recusado(tmp_path: Path, nome_hostil: str) -> None:
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({nome_hostil: b"ok"}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho=f"/saida/{nome_hostil}", destinatario="vendas")
    assert resultado.ok is False


def test_destinatario_desconhecido_e_recusado(tmp_path: Path) -> None:
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({"a.csv": b"x"}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/a.csv", destinatario="rh")
    assert resultado.ok is False


def test_extensao_fora_da_lista_e_recusada(tmp_path: Path) -> None:
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({"virus.exe": b"MZ"}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/virus.exe", destinatario="vendas")
    assert resultado.ok is False


def test_arquivo_vazio_e_recusado(tmp_path: Path) -> None:
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({"vazio.csv": b""}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/vazio.csv", destinatario="vendas")
    assert resultado.ok is False


def test_xlsx_com_vba_project_e_recusado(tmp_path: Path) -> None:
    conteudo = _zip_em_bytes({"[Content_Types].xml": b"<ok/>", "xl/vbaProject.bin": b"macro"})
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({"planilha.xlsx": conteudo}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/planilha.xlsx", destinatario="vendas")
    assert resultado.ok is False


def test_xlsx_que_nao_e_zip_e_recusado(tmp_path: Path) -> None:
    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, _ler_falso({"falso.xlsx": b"nao sou zip"}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/falso.xlsx", destinatario="vendas")
    assert resultado.ok is False


def test_ler_saida_que_levanta_devolve_mensagem_curta_sem_detalhe(tmp_path: Path) -> None:
    def ler_quebrado(nome: str, limite_bytes: int) -> bytes:
        raise RuntimeError("segredo interno /var/lib/docker/xyz")

    registro = Registro(aprovador=sempre_aprova())
    registrar(registro, ler_quebrado, tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/a.csv", destinatario="vendas")
    assert resultado.ok is False
    assert resultado.error == "não foi possível ler o arquivo de saída"
    assert "segredo" not in resultado.error


def test_validacao_roda_antes_do_pedido_de_aprovacao(tmp_path: Path) -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro = Registro(aprovador=espiao)
    registrar(registro, _ler_falso({}), tmp_path / "entregas")
    resultado = _chamar(registro, caminho="/saida/ausente.csv", destinatario="vendas")
    assert resultado.ok is False
    assert pedidos == []


def test_conteudo_gravado_e_o_que_foi_validado(tmp_path: Path) -> None:
    chamadas = 0
    conteudos = [b"primeiro-conteudo", b"segundo-conteudo-adulterado"]

    def ler_mutavel(nome: str, limite_bytes: int) -> bytes:
        nonlocal chamadas
        valor = conteudos[chamadas]
        chamadas += 1
        return valor

    registro = Registro(aprovador=sempre_aprova())
    pasta = tmp_path / "entregas"
    registrar(registro, ler_mutavel, pasta, agora=lambda: _DATA_FIXA)

    resultado = _chamar(registro, caminho="/saida/rel.csv", destinatario="comercial")
    assert resultado.ok is True, resultado.error
    assert chamadas == 1
    gravado = pasta / "comercial" / "20261127-143000-rel.csv"
    assert gravado.read_bytes() == b"primeiro-conteudo"


def test_duas_entregas_no_mesmo_segundo_falham_na_segunda(tmp_path: Path) -> None:
    registro = Registro(aprovador=sempre_aprova())
    pasta = tmp_path / "entregas"
    registrar(registro, _ler_falso({"dup.csv": b"dados"}), pasta, agora=lambda: _DATA_FIXA)

    primeiro = _chamar(registro, caminho="/saida/dup.csv", destinatario="vendas")
    segundo = _chamar(registro, caminho="/saida/dup.csv", destinatario="vendas")
    assert primeiro.ok is True
    assert segundo.ok is False


def test_com_sempre_recusa_nada_e_gravado(tmp_path: Path) -> None:
    registro = Registro(aprovador=sempre_recusa())
    pasta = tmp_path / "entregas"
    registrar(registro, _ler_falso({"a.csv": b"dados"}), pasta, agora=lambda: _DATA_FIXA)

    resultado = _chamar(registro, caminho="/saida/a.csv", destinatario="vendas")
    assert resultado.ok is False
    assert not pasta.exists()
