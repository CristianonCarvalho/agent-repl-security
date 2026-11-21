"""Testes da opção `--mcp` da CLI: LLM falso e sandbox falso, sem rede e sem Docker.

Cobrem a configuração inválida (código 2, antes do sandbox), a chamada de uma ferramenta MCP
pelo despachante, o aviso de servidor que não sobe e o fechamento dos servidores em qualquer
saída, inclusive em erro.
"""

import json
import sys
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from harness.cli import main
from harness.llm.base import LLMFalso
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult
from harness.telemetry.trace import ler_trace
from tests.host.mcp.auxiliares import SERVIDOR_FALSO, ler_pid, processo_morto

RESPOSTA_FINAL = RespostaFinal(answer="Resposta pronta", sources=["falso.eco"])
FINAL_OK = "```python\nFINAL('Resposta pronta', ['falso.eco'])\n```"


class SandboxComMcp:
    """Sandbox falso. Na primeira célula chama `falso.eco` pelo despachante; na segunda, FINAL."""

    def __init__(self, chamar_ferramenta: bool = True, falha_no_start: bool = False) -> None:
        self._chamar = chamar_ferramenta
        self._falha = falha_no_start
        self.resultados: list[ToolResult] = []
        self.fechamentos = 0
        self.execucoes = 0

    def start(self) -> None:
        if self._falha:
            raise RuntimeError("falha de Docker com detalhe do host")

    def carregar_contexto(self, valor: Any) -> None:
        pass

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        self.execucoes += 1
        if self.execucoes == 1 and self._chamar and despachante is not None:
            resultado = despachante(ToolCall(call_id="c1", name="falso.eco", args={"texto": "oi"}))
            self.resultados.append(resultado)
            done = ExecDone(
                cell_id=cell_id,
                stdout=str(resultado.data),
                stderr="",
                exception=None,
                duration_ms=1,
                final=None,
            )
            return ResultadoExecucao(done=done, ferramentas={"falso.eco": 1})
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
        self.fechamentos += 1


class _Fabrica:
    def __init__(self, sandbox: Any) -> None:
        self.sandbox = sandbox
        self.chamadas = 0

    def __call__(self, imagem: str, dados: dict[str, Path] | None = None, **_: Any) -> Any:
        self.chamadas += 1
        return self.sandbox


@pytest.fixture
def dados(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido,valor\n1,10\n", encoding="utf-8")
    (raiz / "comercial").mkdir()
    (raiz / "marketing").mkdir()
    return raiz


def _config(tmp_path: Path, servidores: dict[str, Any]) -> Path:
    caminho = tmp_path / "mcp.json"
    caminho.write_text(json.dumps({"servidores": servidores}), encoding="utf-8")
    return caminho


def _servidor(pid: Path | None = None, modo: str = "normal") -> dict[str, Any]:
    args = [str(SERVIDOR_FALSO), modo]
    if pid is not None:
        args.append(str(pid))
    return {"comando": sys.executable, "args": args, "leitura": ["eco"]}


def _argv(dados: Path, trace: Path, mcp: Path | None) -> list[str]:
    argv = ["Quanto vendemos?", "--perfil", "Vendas", "--dados", str(dados), "--trace", str(trace)]
    if mcp is not None:
        argv += ["--mcp", str(mcp)]
    return argv


# --- Caminho feliz ---------------------------------------------------------------------


def test_ferramenta_mcp_e_chamada_pelo_despachante_e_o_servidor_fecha(
    dados: Path, tmp_path: Path
) -> None:
    pid = tmp_path / "pid"
    mcp = _config(tmp_path, {"falso": _servidor(pid)})
    modelo = LLMFalso(["```python\nprint(tools.falso.eco(texto='oi'))\n```", FINAL_OK])
    sandbox = SandboxComMcp()
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces", mcp),
        modelo=modelo,
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    assert sandbox.resultados[0].ok is True
    assert sandbox.resultados[0].data == '{"texto": "oi"}'
    # O prompt traz o servidor como grupo, sem as descrições das ferramentas.
    assert "- falso" in modelo.chamadas[0][0].content
    assert processo_morto(ler_pid(pid))
    arquivo = next((tmp_path / "traces").glob("*.jsonl"))
    chamadas = [
        linha["dados"]["nome"]
        for linha in ler_trace(arquivo)
        if linha["tipo"] == "chamada_ferramenta"
    ]
    assert chamadas == ["falso.eco"]


# --- Concessões: leitura visível e chamável, escrita externa invisível ------------------


class SandboxDeChamadas:
    """Sandbox falso: na primeira célula manda as chamadas dadas pelo despachante; depois, FINAL."""

    def __init__(self, chamadas: list[ToolCall]) -> None:
        self._chamadas = chamadas
        self.resultados: list[ToolResult] = []
        self.execucoes = 0

    def start(self) -> None:
        pass

    def carregar_contexto(self, valor: Any) -> None:
        pass

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


def test_mcp_de_leitura_aparece_na_busca_e_escrita_externa_fica_invisivel(
    dados: Path, tmp_path: Path
) -> None:
    # `eco` é de leitura e `ambiente` (fora da lista `leitura`) é de escrita externa.
    mcp = _config(tmp_path, {"falso": _servidor()})
    chamadas = [
        ToolCall(call_id="b1", name="descoberta.buscar", args={"query": "eco", "limit": 20}),
        ToolCall(call_id="b2", name="descoberta.buscar", args={"query": "ambiente", "limit": 20}),
        ToolCall(call_id="d1", name="descoberta.descrever", args={"name": "falso.ambiente"}),
        ToolCall(call_id="c1", name="falso.ambiente", args={}),
    ]
    sandbox = SandboxDeChamadas(chamadas)
    codigo = main(
        _argv(dados, tmp_path / "traces", mcp),
        modelo=LLMFalso(["```python\nprint(1)\n```", FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    busca_eco, busca_ambiente, descrever, executar = sandbox.resultados
    assert busca_eco.ok is True
    nomes_eco = [item["nome"] for item in busca_eco.data]
    assert "falso.eco" in nomes_eco
    assert busca_ambiente.ok is True
    assert "falso.ambiente" not in [item["nome"] for item in busca_ambiente.data]
    # Escrita externa não concedida responde como ferramenta inexistente, sem executar.
    # Como a inexistente: a sugestão vem só das ferramentas visíveis (`falso.eco`).
    assert descrever.error == (
        "a ferramenta 'falso.ambiente' não existe; talvez você quis dizer: falso.eco"
    )
    assert executar.error == "a ferramenta 'falso.ambiente' não existe"


# --- Configuração inválida: código 2 antes do sandbox -----------------------------------


@pytest.mark.parametrize(
    ("conteudo", "esperado"),
    [
        (None, "não encontrado"),
        ("{ isto não é json", "JSON inválido"),
        (json.dumps({"servidores": {"Eco": {"comando": "x"}}}), "configuração MCP inválida"),
        (json.dumps({"servidores": {"catalogo": {"comando": "x"}}}), "configuração MCP inválida"),
    ],
)
def test_configuracao_mcp_invalida_sai_dois_antes_do_sandbox(
    dados: Path, tmp_path: Path, conteudo: str | None, esperado: str
) -> None:
    caminho = tmp_path / "mcp.json"
    if conteudo is not None:
        caminho.write_text(conteudo, encoding="utf-8")
    fabrica = _Fabrica(SandboxComMcp())
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces", caminho),
        modelo=LLMFalso([FINAL_OK]),
        saida=saida,
        fabrica_sandbox=fabrica,
    )

    assert codigo == 2
    assert esperado in saida.getvalue()
    assert fabrica.chamadas == 0


def test_falha_do_sandbox_nao_sobe_os_servidores_mcp(dados: Path, tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    mcp = _config(tmp_path, {"falso": _servidor(pid)})
    sandbox = SandboxComMcp(falha_no_start=True)

    codigo = main(
        _argv(dados, tmp_path / "traces", mcp),
        modelo=LLMFalso([FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 2
    assert not pid.exists()


# --- Servidores: aviso e fechamento -----------------------------------------------------


def test_servidor_que_nao_inicia_gera_aviso_e_o_pedido_segue(dados: Path, tmp_path: Path) -> None:
    mcp = _config(tmp_path, {"quebrado": _servidor(modo="falha_inicio")})
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces", mcp),
        modelo=LLMFalso([FINAL_OK]),
        saida=saida,
        fabrica_sandbox=_Fabrica(SandboxComMcp(chamar_ferramenta=False)),
    )

    assert codigo == 0
    assert "aviso: o servidor MCP 'quebrado' não foi iniciado" in saida.getvalue()


def test_servidor_fecha_mesmo_quando_o_modelo_falha(dados: Path, tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    mcp = _config(tmp_path, {"falso": _servidor(pid)})
    sandbox = SandboxComMcp(chamar_ferramenta=False)
    saida = StringIO()

    # Roteiro vazio: o primeiro turno falha com ERRO_LLM e o pedido termina sem FINAL.
    codigo = main(
        _argv(dados, tmp_path / "traces", mcp),
        modelo=LLMFalso([]),
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 1
    assert "motivo: erro_llm" in saida.getvalue()
    assert processo_morto(ler_pid(pid))
    assert sandbox.fechamentos == 1


def test_sem_a_opcao_mcp_nenhum_servidor_e_iniciado(dados: Path, tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    _config(tmp_path, {"falso": _servidor(pid)})

    codigo = main(
        _argv(dados, tmp_path / "traces", None),
        modelo=LLMFalso([FINAL_OK]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(SandboxComMcp(chamar_ferramenta=False)),
    )

    assert codigo == 0
    assert not pid.exists()
