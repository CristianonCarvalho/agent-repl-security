"""Testes da telemetria na avaliação (Tarefa 20a): uma exportação por rodada, nome, tags e
metadados, falha da telemetria que não muda a classificação, e a linha de comando.

Sem Docker e sem rede: sandbox e LLM são falsos, e a telemetria da rodada é gravada numa
classe de teste que implementa o mesmo contrato de `TelemetriaOtel`.
"""

import io
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from harness.evals.__main__ import CODIGO_OK, CODIGO_USO, main
from harness.evals.casos import Caso, Categoria, CitaFontes, Situacao
from harness.evals.executor import ConfigExecucao, executar_avaliacao
from harness.evals.relatorio import Relatorio, agregar
from harness.llm.base import LLMFalso
from harness.orchestrator.loop import Evento
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

FONTE = "vendas/pedidos.csv"
RESPOSTA_BOA = f"```python\nFINAL('Resposta', ['{FONTE}'])\n```"
PK = "pk-lf-FALSA-eval-111"
SK = "sk-lf-FALSA-eval-222"


class _SandboxFalso:
    def __init__(self, *, falhar_start: bool = False) -> None:
        self._falhar_start = falhar_start

    def start(self) -> None:
        if self._falhar_start:
            raise RuntimeError("contêiner indisponível")

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        done = ExecDone(
            cell_id=cell_id,
            stdout="",
            stderr="",
            exception=None,
            duration_ms=1,
            final=RespostaFinal(answer="Resposta", sources=[FONTE]),
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        return None

    def carregar_contexto(self, valor: Any) -> None:
        return None

    def close(self) -> None:
        return None


class _TelemetriaGravada:
    """Implementa o contrato da telemetria da rodada e registra as chamadas."""

    def __init__(self) -> None:
        self.eventos: list[Evento] = []
        self.exportacoes: list[dict[str, Any]] = []
        self.fechamentos = 0

    def __call__(self, evento: Evento) -> None:
        self.eventos.append(evento)

    def exportar(
        self,
        metadados: Mapping[str, Any] | None = None,
        *,
        nome: str | None = None,
        tags: Sequence[str] = (),
        sessao: str | None = None,
    ) -> None:
        self.exportacoes.append(
            {"metadados": dict(metadados or {}), "nome": nome, "tags": list(tags), "sessao": sessao}
        )

    def fechar(self) -> None:
        self.fechamentos += 1


class _TelemetriaQuebrada(_TelemetriaGravada):
    """Falha em todos os métodos. Nada disso pode mudar a classificação."""

    def __call__(self, evento: Evento) -> None:
        raise RuntimeError("telemetria quebrada")

    def exportar(
        self,
        metadados: Mapping[str, Any] | None = None,
        *,
        nome: str | None = None,
        tags: Sequence[str] = (),
        sessao: str | None = None,
    ) -> None:
        raise RuntimeError("exportação quebrada")

    def fechar(self) -> None:
        raise RuntimeError("fechamento quebrado")


def _caso() -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o total? Cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[CitaFontes(fontes=[FONTE])],
    )


class _Relogio:
    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 0.5
        return self._t


def _executar(
    fabrica_telemetria: Callable[[], Any] | None,
    *,
    falhar_start: bool = False,
    rodadas: int = 2,
) -> Relatorio:
    def fabrica_sandbox(montagens: dict[str, Path]) -> _SandboxFalso:
        return _SandboxFalso(falhar_start=falhar_start)

    return executar_avaliacao(
        lambda _fatos: [_caso()],
        lambda: LLMFalso([RESPOSTA_BOA]),
        ConfigExecucao(rodadas=rodadas, pasta_trace=None),
        fabrica_sandbox=fabrica_sandbox,
        relogio=_Relogio(),
        fabrica_telemetria=fabrica_telemetria,
    )


def _classificacoes(relatorio: Relatorio) -> list[str]:
    return [r.classificacao.value for caso in relatorio.casos for r in caso.rodadas]


def test_cada_rodada_exporta_um_trace_com_nome_tags_e_metadados() -> None:
    criadas: list[_TelemetriaGravada] = []

    def fabrica() -> _TelemetriaGravada:
        telemetria = _TelemetriaGravada()
        criadas.append(telemetria)
        return telemetria

    _executar(fabrica)

    assert len(criadas) == 2
    for numero, telemetria in enumerate(criadas, start=1):
        (exportacao,) = telemetria.exportacoes
        assert exportacao["nome"] == "eval/consulta-01"
        assert exportacao["tags"] == [
            "eval",
            Categoria.CONSULTA.value,
            "grupo-desenvolvimento",
            f"rodada-{numero}",
        ]
        metadados = exportacao["metadados"]
        assert metadados["caso_id"] == "consulta-01"
        assert metadados["rodada"] == numero
        assert metadados["semente"] == 42
        assert metadados["classificacao"] == "aprovada"
        assert metadados["aprovado"] is True
        assert metadados["regras_reprovadas"] == []
        assert metadados["motivo_termino"] == "final"
        assert telemetria.fechamentos == 1


def test_rodada_erro_infra_tambem_exporta_com_a_classificacao() -> None:
    criadas: list[_TelemetriaGravada] = []

    def fabrica() -> _TelemetriaGravada:
        telemetria = _TelemetriaGravada()
        criadas.append(telemetria)
        return telemetria

    relatorio = _executar(fabrica, falhar_start=True, rodadas=1)

    (exportacao,) = criadas[0].exportacoes
    assert exportacao["metadados"]["classificacao"] == "erro_infra"
    assert exportacao["metadados"]["detalhe"] == "RuntimeError"
    assert relatorio.erros_infra == 1


def test_falha_da_telemetria_nao_muda_a_classificacao() -> None:
    sem_telemetria = _executar(None)
    com_telemetria_quebrada = _executar(_TelemetriaQuebrada)

    assert _classificacoes(com_telemetria_quebrada) == _classificacoes(sem_telemetria)
    assert com_telemetria_quebrada.taxa_total == sem_telemetria.taxa_total


def test_fabrica_de_telemetria_que_levanta_nao_derruba_a_avaliacao() -> None:
    def fabrica_quebrada() -> None:
        raise RuntimeError("não consegui criar a telemetria")

    relatorio = _executar(fabrica_quebrada)

    assert _classificacoes(relatorio) == _classificacoes(_executar(None))


def test_relatorio_antigo_carrega_sem_o_campo_telemetria() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1)
    texto = json.loads(relatorio.model_dump_json())
    del texto["telemetria"]

    carregado = Relatorio.model_validate(texto)

    assert carregado.telemetria is None


# --- linha de comando -----------------------------------------------------------------------


def _argumentos(saida: Path) -> list[str]:
    return ["--rodadas", "1", "--saida", str(saida), "--trace", str(saida / "trace")]


def _relatorio_vazio() -> Relatorio:
    return agregar([], {}, semente=42, rodadas_por_caso=1)


def test_cli_ligada_imprime_a_linha_e_marca_o_relatorio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TELEMETRIA", "langfuse")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", PK)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)
    chamadas: list[dict[str, Any]] = []

    def executar(
        casos: Callable[..., Any],
        fabrica: Callable[[], Any],
        config: Any,
        *,
        aoprogresso: Any = None,
        fabrica_telemetria: Callable[[], Any] | None = None,
    ) -> Relatorio:
        chamadas.append({"fabrica_telemetria": fabrica_telemetria})
        return _relatorio_vazio()

    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=executar,
        fabrica_modelo=lambda: LLMFalso([]),
    )

    texto = fala.getvalue()
    assert codigo == CODIGO_OK
    assert "Telemetria: Langfuse US, conteúdo completo" in texto
    assert chamadas[0]["fabrica_telemetria"] is not None
    assert SK not in texto
    (relatorio,) = (tmp_path / "saida").glob("*.json")
    assert json.loads(relatorio.read_text(encoding="utf-8"))["telemetria"] == "langfuse-us"


def test_cli_ligada_sem_chave_sai_com_dois_e_nao_roda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TELEMETRIA", "langfuse")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SK)
    chamadas: list[Any] = []

    def executar(*args: Any, **kwargs: Any) -> Relatorio:
        chamadas.append(args)
        return _relatorio_vazio()

    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=executar,
        fabrica_modelo=lambda: LLMFalso([]),
    )

    assert codigo == CODIGO_USO
    assert "LANGFUSE_PUBLIC_KEY" in fala.getvalue()
    assert SK not in fala.getvalue()
    assert chamadas == []


def test_cli_desligada_nao_mostra_linha_nem_marca_o_relatorio(
    tmp_path: Path,
) -> None:
    fala = io.StringIO()
    codigo = main(
        _argumentos(tmp_path / "saida"),
        saida=fala,
        executar=lambda *_, **__: _relatorio_vazio(),
        fabrica_modelo=lambda: LLMFalso([]),
    )

    assert codigo == CODIGO_OK
    assert "Telemetria" not in fala.getvalue()
    (relatorio,) = (tmp_path / "saida").glob("*.json")
    assert json.loads(relatorio.read_text(encoding="utf-8"))["telemetria"] is None
