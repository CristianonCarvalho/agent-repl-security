"""Executor com o servidor MCP de teste `contas` (T19-c): ciclo de vida e visão de cada variante.

Os dados e o servidor são de verdade (stdio, subprocesso); o modelo é um LLM falso roteirizado e o
sandbox do REPL é um falso que chama o despachante do registro, como o contêiner faria. Sem rede,
sem Docker, sem modelo real.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from harness.evals.casos import Caso
from harness.evals.conjunto import casos_de_injecao_avancada
from harness.evals.dados import FatosVerdadeiros
from harness.evals.executor import ConfigExecucao, executar_avaliacao
from harness.evals.ferramentas_json import perfil_json, registro_json
from harness.evals.fixtures import servidor_contas
from harness.evals.mcp_avaliacao import config_mcp_de_contas
from harness.evals.relatorio import Classificacao, Relatorio, relatorio_de_json, relatorio_para_json
from harness.host.mcp.adaptador import registrar_servidores_mcp
from harness.host.mcp.config import ConfigMcp
from harness.host.perfil import perfil_por_nome
from harness.host.registry import Registro
from harness.llm.base import ChamadaFerramenta, LLMFalso, RespostaLLM
from harness.llm.ferramentas import hash_dos_esquemas
from harness.orchestrator.loop_json import esquemas_do_registro
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult
from tests.host.mcp.auxiliares import processo_morto


class Espiao:
    """Guarda os PIDs de todo servidor MCP iniciado e os fatos que a execução gerou."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pids: list[int] = []
        self.fatos: FatosVerdadeiros | None = None

        def espiar(registro: Registro, config: ConfigMcp) -> Any:
            gerente = registrar_servidores_mcp(registro, config)
            self.pids.extend(cliente.pid for cliente in gerente._clientes.values())
            return gerente

        monkeypatch.setattr("harness.evals.mcp_avaliacao.registrar_servidores_mcp", espiar)

    def casos(self, caso_id: str) -> Callable[[FatosVerdadeiros], Sequence[Caso]]:
        def selecionar(fatos: FatosVerdadeiros) -> Sequence[Caso]:
            self.fatos = fatos
            return [c for c in casos_de_injecao_avancada(fatos) if c.id == caso_id]

        return selecionar

    def todos_mortos(self) -> bool:
        return all(processo_morto(pid) for pid in self.pids)


@pytest.fixture
def espiao(monkeypatch: pytest.MonkeyPatch) -> Espiao:
    return Espiao(monkeypatch)


def _config(variante: str = "json", **extra: Any) -> ConfigExecucao:
    return ConfigExecucao(
        rodadas=1,
        teto_usd=None,
        teto_tokens=None,
        variante=variante,  # type: ignore[arg-type]
        com_ataques_avancados=True,
        **extra,
    )


def _chamada(identificador: str, nome: str, argumentos: dict[str, Any]) -> ChamadaFerramenta:
    return ChamadaFerramenta(id=identificador, nome=nome, argumentos_brutos=json.dumps(argumentos))


def _cliente_vencido(fatos: FatosVerdadeiros) -> str:
    return max(fatos.vencido_por_cliente, key=fatos.vencido_por_cliente.__getitem__)


def _resposta_certa(fatos: FatosVerdadeiros) -> str:
    cliente = _cliente_vencido(fatos)
    reais = servidor_contas.formatar_reais(fatos.vencido_por_cliente[cliente])
    return f"O total vencido de {cliente} é {reais}."


class ModeloQueConsultaOMcp:
    """Fábrica de LLMs falsos: consulta `contas__vencidos` num turno e finaliza no seguinte.

    O cliente é lido dos fatos só quando a rodada pede o modelo, depois de a geração ter rodado.
    """

    def __init__(self, espiao: Espiao, *, finalizar: Callable[[FatosVerdadeiros], str]) -> None:
        self._espiao = espiao
        self._finalizar = finalizar
        self.modelos: list[LLMFalso] = []

    def __call__(self) -> LLMFalso:
        fatos = self._espiao.fatos
        assert fatos is not None
        modelo = LLMFalso(
            [
                RespostaLLM(
                    texto="",
                    tokens_entrada=100,
                    tokens_saida=5,
                    chamadas_ferramenta=(
                        _chamada("c1", "contas__vencidos", {"cliente": _cliente_vencido(fatos)}),
                    ),
                ),
                RespostaLLM(
                    texto="",
                    tokens_entrada=200,
                    tokens_saida=5,
                    chamadas_ferramenta=(
                        _chamada(
                            "f",
                            "finalizar",
                            {"resposta": self._finalizar(fatos), "fontes": ["contas.vencidos"]},
                        ),
                    ),
                ),
            ]
        )
        self.modelos.append(modelo)
        return modelo


# --- variantes JSON -----------------------------------------------------------------------------


@pytest.mark.parametrize("variante", ["json", "json-agregacao"])
def test_json_o_modelo_ve_as_ferramentas_do_servidor_e_a_resposta_envenenada(
    espiao: Espiao, variante: str
) -> None:
    fabrica = ModeloQueConsultaOMcp(espiao, finalizar=_resposta_certa)
    relatorio = executar_avaliacao(espiao.casos("injecao-10"), fabrica, _config(variante))

    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.APROVADA
    assert rodada.injecao_obedecida is False
    modelo = fabrica.modelos[0]
    enviados, _ = modelo.ferramentas_recebidas[0]
    nomes = [e.nome for e in enviados]
    assert "contas__vencidos" in nomes and "contas__contratos_vencendo" in nomes
    assert nomes[-1] == "finalizar"
    descricao = next(e.descricao for e in enviados if e.nome == "contas__contratos_vencendo")
    assert servidor_contas.INSTRUCAO_DESCRICAO in descricao
    # A resposta da ferramenta, com a instrução, chega ao modelo na mensagem `tool` do turno 2.
    recebida = " ".join(m.content for m in modelo.chamadas[1] if m.role == "tool")
    assert servidor_contas.INSTRUCAO_RESULTADO in recebida
    assert relatorio.ataques is True
    assert espiao.pids and espiao.todos_mortos()


@pytest.mark.parametrize("variante", ["json", "json-agregacao"])
def test_json_obediencia_a_resposta_da_ferramenta_e_medida(espiao: Espiao, variante: str) -> None:
    def obedecer(fatos: FatosVerdadeiros) -> str:
        return f"{_resposta_certa(fatos)} Enviei a lista de títulos para auditoria@exemplo.invalid."

    relatorio = executar_avaliacao(
        espiao.casos("injecao-10"),
        ModeloQueConsultaOMcp(espiao, finalizar=obedecer),
        _config(variante),
    )
    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.REPROVADA
    assert rodada.injecao_obedecida is True
    assert relatorio.rodadas_com_obediencia == 1
    assert espiao.todos_mortos()


def test_json_relatorio_registra_o_conjunto_e_o_hash_com_o_servidor(
    espiao: Espiao, tmp_path: Path
) -> None:
    def rodar() -> Relatorio:
        return executar_avaliacao(
            espiao.casos("injecao-10"),
            ModeloQueConsultaOMcp(espiao, finalizar=_resposta_certa),
            _config("json"),
        )

    primeiro, segundo = rodar(), rodar()
    assert primeiro.ferramentas_json == "basicas+mcp-contas"
    assert primeiro.hash_ferramentas is not None
    # Determinístico entre execuções (o servidor sobe de novo) e diferente do hash sem o servidor.
    assert primeiro.hash_ferramentas == segundo.hash_ferramentas
    base = hash_dos_esquemas(
        esquemas_do_registro(
            registro_json(tmp_path, "json"), perfil_json(perfil_por_nome("Direção"), "json")
        )
    )
    assert primeiro.hash_ferramentas != base
    assert relatorio_de_json(relatorio_para_json(primeiro)) == primeiro
    assert espiao.pids and espiao.todos_mortos()


def test_json_caso_de_arquivos_nao_ve_o_servidor(espiao: Espiao) -> None:
    modelo = LLMFalso(
        [
            RespostaLLM(
                texto="",
                tokens_entrada=10,
                tokens_saida=5,
                chamadas_ferramenta=(
                    _chamada(
                        "f",
                        "finalizar",
                        {"resposta": "resumo", "fontes": ["direcao/comunicados/x.docx"]},
                    ),
                ),
            )
        ]
    )
    relatorio = executar_avaliacao(espiao.casos("injecao-12"), lambda: modelo, _config("json"))
    enviados, _ = modelo.ferramentas_recebidas[0]
    assert not any(e.nome.startswith("contas__") for e in enviados)
    assert relatorio.casos[0].rodadas[0].classificacao is Classificacao.REPROVADA
    # Sem rodada com o servidor, só o cálculo do hash do relatório o sobe e encerra.
    assert espiao.todos_mortos()


def test_json_erro_do_modelo_encerra_o_servidor(espiao: Espiao) -> None:
    relatorio = executar_avaliacao(
        espiao.casos("injecao-10"), lambda: LLMFalso([]), _config("json")
    )
    assert relatorio.casos[0].rodadas[0].classificacao is Classificacao.ERRO_INFRA
    assert espiao.pids and espiao.todos_mortos()


# --- variante REPL ------------------------------------------------------------------------------


class SandboxQueChamaOMcp:
    """Sandbox falso do REPL: chama as duas ferramentas pelo despachante e finaliza."""

    def __init__(self, fatos: Callable[[], FatosVerdadeiros | None]) -> None:
        self._fatos = fatos
        self.resultados: dict[str, ToolResult] = {}
        self.fechado = 0
        self.iniciado = False

    def start(self) -> None:
        self.iniciado = True

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        fatos = self._fatos()
        assert fatos is not None and despachante is not None
        cliente = _cliente_vencido(fatos)
        for nome in ("contas.vencidos", "contas.contratos_vencendo"):
            self.resultados[nome] = despachante(
                ToolCall(call_id=nome, name=nome, args={"cliente": cliente})
            )
        self.resultados["descoberta.descrever"] = despachante(
            ToolCall(
                call_id="d",
                name="descoberta.descrever",
                args={"name": "contas.contratos_vencendo"},
            )
        )
        final = RespostaFinal(answer=_resposta_certa(fatos), sources=["contas.vencidos"])
        done = ExecDone(
            cell_id=cell_id, stdout="", stderr="", exception=None, duration_ms=1, final=final
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        return None

    def carregar_contexto(self, valor: Any) -> None:
        return None

    def close(self) -> None:
        self.fechado += 1


def _modelo_repl() -> LLMFalso:
    return LLMFalso(
        [RespostaLLM(texto="```python\nprint(1)\n```", tokens_entrada=10, tokens_saida=5)]
    )


def test_repl_o_agente_chama_o_servidor_pelo_despachante_e_o_encerra(espiao: Espiao) -> None:
    sandboxes: list[SandboxQueChamaOMcp] = []

    def fabrica(_montagens: dict[str, Path]) -> SandboxQueChamaOMcp:
        sandbox = SandboxQueChamaOMcp(lambda: espiao.fatos)
        sandboxes.append(sandbox)
        return sandbox

    modelos: list[LLMFalso] = []

    def fabrica_modelo() -> LLMFalso:
        modelos.append(_modelo_repl())
        return modelos[-1]

    relatorio = executar_avaliacao(
        espiao.casos("injecao-10"), fabrica_modelo, _config("repl"), fabrica_sandbox=fabrica
    )

    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.APROVADA
    resultados = sandboxes[0].resultados
    assert resultados["contas.vencidos"].ok
    assert servidor_contas.INSTRUCAO_RESULTADO in str(resultados["contas.vencidos"].data)
    assert resultados["contas.contratos_vencendo"].ok
    assert relatorio.ferramentas_json is None and relatorio.hash_ferramentas is None
    # O REPL vê o grupo no prompt e a descrição envenenada pela descoberta.
    assert "- contas" in modelos[0].chamadas[0][0].content
    descrita = resultados["descoberta.descrever"]
    assert descrita.ok
    assert servidor_contas.INSTRUCAO_DESCRICAO in str(descrita.data)
    assert espiao.pids and espiao.todos_mortos()


def test_repl_caso_de_arquivos_nao_ve_o_servidor(espiao: Espiao) -> None:
    sandboxes: list[SandboxQueChamaOMcp] = []

    def fabrica(_montagens: dict[str, Path]) -> SandboxQueChamaOMcp:
        sandboxes.append(SandboxQueChamaOMcp(lambda: espiao.fatos))
        return sandboxes[-1]

    executar_avaliacao(
        espiao.casos("injecao-12"), _modelo_repl, _config("repl"), fabrica_sandbox=fabrica
    )
    # Nem o servidor subiu: o despachante diz que a ferramenta não existe.
    assert espiao.pids == []
    assert not sandboxes[0].resultados["contas.vencidos"].ok


def test_repl_falha_ao_iniciar_o_sandbox_encerra_o_servidor(espiao: Espiao) -> None:
    class SandboxQueNaoInicia(SandboxQueChamaOMcp):
        def start(self) -> None:
            raise RuntimeError("sem docker")

    relatorio = executar_avaliacao(
        espiao.casos("injecao-10"),
        _modelo_repl,
        _config("repl"),
        fabrica_sandbox=lambda _m: SandboxQueNaoInicia(lambda: espiao.fatos),
    )
    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == "RuntimeError"
    assert espiao.pids and espiao.todos_mortos()


# --- falha ao iniciar o servidor ----------------------------------------------------------------


@pytest.mark.parametrize("variante", ["repl", "json"])
def test_falha_ao_iniciar_o_servidor_vira_erro_de_infra_da_rodada(
    espiao: Espiao, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variante: str
) -> None:
    def sem_fatos(arquivo: Path) -> ConfigMcp:
        # Aponta o servidor para um arquivo que não existe: ele sai com código 3 ao iniciar.
        return config_mcp_de_contas(tmp_path / "nao_existe.json")

    monkeypatch.setattr("harness.evals.executor.config_mcp_de_contas", sem_fatos)
    modelos: list[LLMFalso] = []

    def fabrica_modelo() -> LLMFalso:
        modelos.append(LLMFalso([]))
        return modelos[-1]

    sandboxes: list[SandboxQueChamaOMcp] = []

    def fabrica_sandbox(_m: dict[str, Path]) -> SandboxQueChamaOMcp:
        sandboxes.append(SandboxQueChamaOMcp(lambda: espiao.fatos))
        return sandboxes[-1]

    relatorio = executar_avaliacao(
        espiao.casos("injecao-10"),
        fabrica_modelo,
        _config(variante),
        fabrica_sandbox=fabrica_sandbox,
    )

    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == "ErroMcpDeAvaliacao"
    assert relatorio.erros_infra == 1
    # O modelo não chegou a ser chamado, e o sandbox do REPL nem foi criado.
    assert all(not modelo.chamadas for modelo in modelos)
    assert sandboxes == []
    # Nenhum processo órfão: o servidor que saiu com 3 já foi colhido pelo cliente.
    assert espiao.todos_mortos()
    if variante == "json":
        # O hash do relatório não derruba a execução: sem o servidor, fica vazio.
        assert relatorio.hash_ferramentas is None
        assert relatorio.ferramentas_json == "basicas+mcp-contas"


def test_sem_a_flag_nenhum_servidor_sobe(espiao: Espiao) -> None:
    config = ConfigExecucao(rodadas=1, teto_usd=None, teto_tokens=None, variante="json")
    modelo = LLMFalso([])
    relatorio = executar_avaliacao(espiao.casos("injecao-10"), lambda: modelo, config)
    assert espiao.pids == []
    assert relatorio.ferramentas_json == "basicas"
    assert relatorio.ataques is False
    enviados, _ = modelo.ferramentas_recebidas[0]
    assert not any(e.nome.startswith("contas__") for e in enviados)
