"""Testes das opções `--entregas` e `--prazo-aprovacao` da CLI."""

import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from harness.cli import _parser, main
from harness.llm.base import LLMFalso
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

RESPOSTA_FINAL = RespostaFinal(answer="Resposta pronta", sources=["catalogo.listar"])
FINAL_OK = "```python\nFINAL('Resposta pronta', ['catalogo.listar'])\n```"


class _SandboxDeEntregas:
    """Sandbox falso com `ler_saida` que executa uma lista de chamadas na primeira célula."""

    def __init__(self, chamadas: list[ToolCall], saidas: dict[str, bytes] | None = None) -> None:
        self._chamadas = chamadas
        self._saidas = saidas or {}
        self.resultados: list[ToolResult] = []
        self.execucoes = 0

    def start(self) -> None:
        pass

    def carregar_contexto(self, valor: Any) -> None:
        pass

    def ler_saida(self, nome: str, limite_bytes: int) -> bytes:
        if nome not in self._saidas:
            raise FileNotFoundError(nome)
        return self._saidas[nome][: limite_bytes + 1]

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        self.execucoes += 1
        if self.execucoes == 1 and despachante is not None:
            for chamada in self._chamadas:
                self.resultados.append(despachante(chamada))
            done = ExecDone(
                cell_id=cell_id, stdout="", stderr="", exception=None, duration_ms=1, final=None
            )
            return ResultadoExecucao(done=done, ferramentas={})
        done = ExecDone(
            cell_id=cell_id,
            stdout="",
            stderr="",
            exception=None,
            duration_ms=1,
            final=RESPOSTA_FINAL,
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        pass

    def close(self) -> None:
        pass


class _Fabrica:
    def __init__(self, sandbox: Any) -> None:
        self.sandbox = sandbox
        self.chamadas = 0

    def __call__(self, imagem: str, dados: dict[str, Path] | None = None, **_: Any) -> Any:
        self.chamadas += 1
        return self.sandbox


class _TerminalFalso(StringIO):
    """Stream de entrada que simula um TTY para `AprovadorDeTerminal`."""

    def isatty(self) -> bool:
        return True


@pytest.fixture
def dados(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido,valor\n1,10\n", encoding="utf-8")
    (raiz / "comercial").mkdir()
    (raiz / "marketing").mkdir()
    return raiz


def _argv(
    dados: Path,
    trace: Path,
    entregas: Path | None = None,
    prazo: str | None = None,
) -> list[str]:
    argv = ["Quanto vendemos?", "--perfil", "Vendas", "--dados", str(dados), "--trace", str(trace)]
    if entregas is not None:
        argv += ["--entregas", str(entregas)]
    if prazo is not None:
        argv += ["--prazo-aprovacao", prazo]
    return argv


def test_parser_aceita_entregas_e_prazo_aprovacao() -> None:
    padrao = _parser().parse_args(["pedido", "--perfil", "Vendas", "--dados", "d"])
    assert padrao.entregas is None
    assert padrao.prazo_aprovacao == 120

    custom = _parser().parse_args(
        [
            "pedido",
            "--perfil",
            "Vendas",
            "--dados",
            "d",
            "--entregas",
            "saida_entregas",
            "--prazo-aprovacao",
            "45",
        ]
    )
    assert custom.entregas == Path("saida_entregas")
    assert custom.prazo_aprovacao == 45


@pytest.mark.parametrize("prazo", ["0", "-5", "abc"])
def test_prazo_aprovacao_invalido_e_recusado(dados: Path, tmp_path: Path, prazo: str) -> None:
    fabrica = _Fabrica(_SandboxDeEntregas([]))
    codigo = main(
        _argv(dados, tmp_path / "traces", prazo=prazo),
        modelo=LLMFalso([FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=fabrica,
    )

    assert codigo == 2
    assert fabrica.chamadas == 0


def test_sem_entregas_ferramenta_nao_existe_no_registro_nem_no_perfil(
    dados: Path, tmp_path: Path
) -> None:
    chamadas = [
        ToolCall(call_id="b1", name="descoberta.buscar", args={"query": "entrega", "limit": 20}),
        ToolCall(call_id="d1", name="descoberta.descrever", args={"name": "entrega.publicar"}),
        ToolCall(
            call_id="c1",
            name="entrega.publicar",
            args={"caminho": "/saida/relatorio.csv", "destinatario": "vendas"},
        ),
    ]
    sandbox = _SandboxDeEntregas(chamadas)

    codigo = main(
        _argv(dados, tmp_path / "traces"),
        modelo=LLMFalso(["```python\nprint(1)\n```", FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    busca, descrever, publicar = sandbox.resultados
    assert busca.ok is True
    assert busca.data == []
    assert descrever.error == "a ferramenta 'entrega.publicar' não existe"
    assert publicar.error == "a ferramenta 'entrega.publicar' não existe"


def test_com_entregas_ferramenta_existe_e_perfil_a_concede(dados: Path, tmp_path: Path) -> None:
    entregas = tmp_path / "entregas"
    chamadas = [
        ToolCall(call_id="b1", name="descoberta.buscar", args={"query": "entrega", "limit": 20}),
        ToolCall(call_id="d1", name="descoberta.descrever", args={"name": "entrega.publicar"}),
    ]
    sandbox = _SandboxDeEntregas(chamadas)

    codigo = main(
        _argv(dados, tmp_path / "traces", entregas=entregas),
        modelo=LLMFalso(["```python\nprint(1)\n```", FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    busca, descrever = sandbox.resultados
    assert busca.ok is True
    assert [item["nome"] for item in busca.data] == ["entrega.publicar"]
    assert descrever.ok is True


def test_sem_terminal_aprovador_recusa_chamada_que_exige_aprovacao(
    dados: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO(""))
    entregas = tmp_path / "entregas"
    chamadas = [
        ToolCall(
            call_id="c1",
            name="entrega.publicar",
            args={"caminho": "/saida/relatorio.csv", "destinatario": "vendas"},
        ),
    ]
    sandbox = _SandboxDeEntregas(chamadas, saidas={"relatorio.csv": b"pedido,valor\n1,10\n"})

    codigo = main(
        _argv(dados, tmp_path / "traces", entregas=entregas),
        modelo=LLMFalso(["```python\nprint(1)\n```", FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    resultado = sandbox.resultados[0]
    assert resultado.ok is False
    assert resultado.error == "a chamada foi recusada pela pessoa responsável"


def test_com_terminal_aprovador_aprova_e_publica_entrega(
    dados: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", _TerminalFalso("s\n"))
    entregas = tmp_path / "entregas"
    chamadas = [
        ToolCall(
            call_id="c1",
            name="entrega.publicar",
            args={"caminho": "/saida/relatorio.csv", "destinatario": "vendas"},
        ),
    ]
    sandbox = _SandboxDeEntregas(chamadas, saidas={"relatorio.csv": b"pedido,valor\n1,10\n"})
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces", entregas=entregas, prazo="30"),
        modelo=LLMFalso(["```python\nprint(1)\n```", FINAL_OK]),
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    resultado = sandbox.resultados[0]
    assert resultado.ok is True
    assert "Aprovar? [s/N]" in saida.getvalue()
    assert (entregas / "vendas").exists()
