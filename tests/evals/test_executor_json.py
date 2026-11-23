"""Testes do executor com as variantes JSON e das métricas de contexto (T18a).

Os dados são gerados de verdade (semente 42), e o modelo é um LLM falso roteirizado. Nenhum teste
faz chamada de rede ou abre Docker: o sandbox do REPL é um falso, injetado pela fábrica.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from harness.evals.casos import (
    Caso,
    Categoria,
    CitaFontes,
    ContemNumero,
    Situacao,
    formatar_numero_br,
)
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.executor import ConfigExecucao, ModeloContabil, executar_avaliacao
from harness.evals.ferramentas_json import perfil_json, registro_json
from harness.evals.relatorio import Classificacao, relatorio_de_json, relatorio_para_json
from harness.host.perfil import perfil_por_nome
from harness.llm.base import ChamadaFerramenta, LLMFalso, Mensagem, RespostaLLM
from harness.llm.ferramentas import hash_dos_esquemas
from harness.orchestrator.loop_json import esquemas_do_registro
from harness.orchestrator.observation import formatar_observacao
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal

FONTE = "vendas/pedidos_2026-10.xlsx"


@pytest.fixture(scope="module")
def fatos(tmp_path_factory: pytest.TempPathFactory) -> FatosVerdadeiros:
    return gerar_dados(tmp_path_factory.mktemp("exec-json") / "dados", 42)


def _caso(fatos: FatosVerdadeiros) -> Caso:
    return Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o total válido? Responda só com o valor e cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[
            ContemNumero(valor=fatos.valor_total_validos),
            CitaFontes(fontes=[FONTE]),
        ],
        comportamento_esperado=None,
    )


def _chamada(identificador: str, nome: str, argumentos: dict[str, Any]) -> ChamadaFerramenta:
    return ChamadaFerramenta(id=identificador, nome=nome, argumentos_brutos=json.dumps(argumentos))


def _roteiro_json(resposta_final: str) -> list[RespostaLLM]:
    """Dois turnos: lista e lê dois arquivos num turno só; depois finaliza com a resposta dada."""
    return [
        RespostaLLM(
            texto="",
            tokens_entrada=100,
            tokens_saida=10,
            chamadas_ferramenta=(
                _chamada("c1", "catalogo__listar", {"departamento": "vendas"}),
                _chamada("c2", "arquivos__ler_planilha", {"arquivo": FONTE, "limite": 5}),
            ),
        ),
        RespostaLLM(
            texto="",
            tokens_entrada=250,
            tokens_saida=12,
            chamadas_ferramenta=(
                _chamada("f", "finalizar", {"resposta": resposta_final, "fontes": [FONTE]}),
            ),
        ),
    ]


class SandboxSimples:
    """Sandbox falso do REPL: cada célula imprime `dados` e, se o código chama FINAL, finaliza."""

    def __init__(self, resposta_final: str) -> None:
        self._resposta_final = resposta_final
        self.fechado = 0

    def start(self) -> None:
        return None

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[..., Any] | None = None,
    ) -> ResultadoExecucao:
        final = (
            RespostaFinal(answer=self._resposta_final, sources=[FONTE])
            if "FINAL(" in code
            else None
        )
        done = ExecDone(
            cell_id=cell_id, stdout="dados", stderr="", exception=None, duration_ms=1, final=final
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        return None

    def carregar_contexto(self, valor: Any) -> None:
        return None

    def close(self) -> None:
        self.fechado += 1


def _roteiro_repl(resposta_final: str) -> list[RespostaLLM]:
    """Dois turnos de código: o primeiro imprime `dados`, o segundo finaliza."""
    return [
        RespostaLLM(texto="```python\nprint('dados')\n```", tokens_entrada=100, tokens_saida=10),
        RespostaLLM(
            texto=f"```python\nFINAL({resposta_final!r}, ['{FONTE}'])\n```",
            tokens_entrada=250,
            tokens_saida=12,
        ),
    ]


def _executar(
    fatos: FatosVerdadeiros,
    modelo: LLMFalso,
    variante: str,
    resposta_certa: str,
    sandbox: Any = None,
) -> Any:
    config = ConfigExecucao(
        rodadas=1,
        teto_usd=None,
        teto_tokens=None,
        variante=variante,  # type: ignore[arg-type]
    )
    return executar_avaliacao(
        lambda _fatos: [_caso(fatos)],
        lambda: modelo,
        config,
        fabrica_sandbox=(lambda _montagens: sandbox) if sandbox is not None else None,
    )


def _numero(fatos: FatosVerdadeiros) -> str:
    return f"O total válido é R$ {formatar_numero_br(fatos.valor_total_validos)}."


def test_rodada_json_aprovada_com_metricas_e_procedencia(fatos: FatosVerdadeiros) -> None:
    modelo = LLMFalso(_roteiro_json(_numero(fatos)))
    relatorio = _executar(fatos, modelo, "json-agregacao", _numero(fatos))
    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.APROVADA
    assert relatorio.variante == "json-agregacao"
    assert relatorio.ferramentas_json == "basicas+agregacao"
    # Métricas conferidas à mão: entrada de cada chamada (100 e 250) e o pico (250).
    assert rodada.tokens_entrada_por_turno == (100, 250)
    assert rodada.pico_contexto == 250
    # Caracteres de observação: a soma do que o modelo recebeu nas mensagens `tool` do turno 2.
    recebidas = sum(len(m.content) for m in modelo.chamadas[1] if m.role == "tool")
    assert rodada.caracteres_observacao == recebidas > 0
    assert relatorio.pico_contexto_medio == 250.0
    assert relatorio.caracteres_observacao_medio == float(recebidas)
    # Procedência: o hash é o dos esquemas que o modelo recebeu.
    perfil = perfil_json(perfil_por_nome("Vendas"), "json-agregacao")
    esquemas = esquemas_do_registro(registro_json(Path("."), "json-agregacao"), perfil)
    assert relatorio.hash_ferramentas == hash_dos_esquemas(esquemas)
    enviados, _escolha = modelo.ferramentas_recebidas[0]
    assert [e.nome for e in enviados] == [e.nome for e in esquemas]


def test_rodada_json_reprovada_quando_a_resposta_erra(fatos: FatosVerdadeiros) -> None:
    modelo = LLMFalso(_roteiro_json("O total é R$ 1,00."))
    relatorio = _executar(fatos, modelo, "json", "")
    assert relatorio.casos[0].rodadas[0].classificacao is Classificacao.REPROVADA
    assert relatorio.ferramentas_json == "basicas"


def test_erro_do_modelo_na_variante_json_vira_erro_infra(fatos: FatosVerdadeiros) -> None:
    modelo = LLMFalso([])
    relatorio = _executar(fatos, modelo, "json", "")
    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == "erro_llm: RoteiroEsgotado"
    assert relatorio.erros_infra == 1


def test_repl_continua_sem_campos_json_e_com_metricas_de_contexto(fatos: FatosVerdadeiros) -> None:
    modelo = LLMFalso(_roteiro_repl(_numero(fatos)))
    relatorio = _executar(
        fatos, modelo, "repl", _numero(fatos), sandbox=SandboxSimples(_numero(fatos))
    )
    rodada = relatorio.casos[0].rodadas[0]
    assert rodada.classificacao is Classificacao.APROVADA
    assert relatorio.variante == "repl"
    assert relatorio.ferramentas_json is None
    assert relatorio.hash_ferramentas is None
    assert rodada.tokens_entrada_por_turno == (100, 250)
    assert rodada.pico_contexto == 250
    # O REPL devolve a observação do turno 1 ao modelo: a conta é a do formato de observação.
    esperado = formatar_observacao(
        1,
        ExecDone(cell_id=1, stdout="dados", stderr="", exception=None, duration_ms=1, final=None),
        {},
    )
    assert rodada.caracteres_observacao == len(esperado)


def test_modelo_contabil_repassa_a_resposta_com_ferramentas_sem_mudar_o_texto() -> None:
    resposta = RespostaLLM(texto="", tokens_entrada=3, tokens_saida=2, custo=0.5)
    modelo = LLMFalso([resposta])
    contabil = ModeloContabil(modelo)
    devolvida = contabil.responder_com_ferramentas([Mensagem(role="user", content="x")], [])
    assert devolvida == resposta
    assert contabil.tokens_entrada == 3 and contabil.tokens_saida == 2
    assert contabil.custo_total == 0.5


def test_relatorio_da_variante_json_vai_e_volta_em_json(fatos: FatosVerdadeiros) -> None:
    modelo = LLMFalso(_roteiro_json(_numero(fatos)))
    relatorio = _executar(fatos, modelo, "json", "")
    recarregado = relatorio_de_json(relatorio_para_json(relatorio))
    assert recarregado == relatorio
    assert recarregado.casos[0].rodadas[0].tokens_entrada_por_turno == (100, 250)


def test_variante_desconhecida_e_recusada_pela_configuracao() -> None:
    with pytest.raises(ValueError):
        ConfigExecucao(variante="xpto")  # type: ignore[arg-type]


def test_sandbox_nao_e_usado_na_variante_json(fatos: FatosVerdadeiros) -> None:
    class SandboxQueNaoPodeSerUsado:
        def start(self) -> None:
            raise AssertionError("a variante JSON não pode iniciar sandbox")

    modelo = LLMFalso(_roteiro_json(_numero(fatos)))
    relatorio = _executar(fatos, modelo, "json", "", sandbox=SandboxQueNaoPodeSerUsado())
    assert relatorio.casos[0].rodadas[0].classificacao is Classificacao.APROVADA
