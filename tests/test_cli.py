"""Testes da CLI (`python -m harness`) com LLM falso e sandbox falso: sem rede e sem Docker.

Cobrem os códigos de saída (0 completo, 1 não completo, 2 uso ou configuração), a saída
impressa e a ausência de vazamento da chave da API quando a configuração falta.
"""

from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from harness.cli import main
from harness.llm.base import LLMFalso, RespostaLLM
from harness.orchestrator.loop import Orcamento
from harness.sandbox.docker_sandbox import ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

FINAL_OK = "```python\nFINAL('Resposta pronta', ['vendas/pedidos.csv'])\n```"
RESPOSTA_FINAL = RespostaFinal(answer="Resposta pronta", sources=["vendas/pedidos.csv"])
SEGREDO = "sk-or-SEGREDO-de-teste-123"


class SandboxRoteiro:
    """Sandbox falso: devolve as células na ordem e registra o ciclo de vida."""

    def __init__(self, celulas: Sequence[ExecDone] = ()) -> None:
        self._celulas = list(celulas)
        self.inicios = 0
        self.fechamentos = 0
        self.execucoes = 0

    def start(self) -> None:
        self.inicios += 1

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
        if self._celulas:
            pronta = self._celulas.pop(0)
            done = ExecDone(
                cell_id=cell_id,
                stdout=pronta.stdout,
                stderr="",
                exception=None,
                duration_ms=1,
                final=pronta.final,
            )
        else:
            done = ExecDone(
                cell_id=cell_id,
                stdout="",
                stderr="",
                exception=None,
                duration_ms=1,
                final=None,
            )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        pass

    def close(self) -> None:
        self.fechamentos += 1


def _celula_final() -> ExecDone:
    return ExecDone(
        cell_id=1, stdout="", stderr="", exception=None, duration_ms=1, final=RESPOSTA_FINAL
    )


class _Fabrica:
    """Fábrica falsa do sandbox: guarda os argumentos recebidos e devolve o sandbox dado."""

    def __init__(self, sandbox: SandboxRoteiro) -> None:
        self.sandbox = sandbox
        self.chamadas: list[tuple[str, dict[str, Path] | None]] = []

    def __call__(
        self, imagem: str, dados: dict[str, Path] | None = None, **_: Any
    ) -> SandboxRoteiro:
        self.chamadas.append((imagem, dados))
        return self.sandbox


@pytest.fixture
def dados(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "pedidos.csv").write_text("pedido,valor\n1,10\n", encoding="utf-8")
    (raiz / "comercial").mkdir()
    (raiz / "marketing").mkdir()
    return raiz


@pytest.fixture
def ambiente_limpo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Diretório de trabalho vazio, sem .env, e sem variáveis OPENROUTER no ambiente."""
    for nome in ("OPENROUTER_API_KEY", "OPENROUTER_MODELO", "OPENROUTER_TEMPERATURA"):
        monkeypatch.delenv(nome, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _argv(dados: Path, trace: Path, *extra: str) -> list[str]:
    return [
        "Quanto vendemos?",
        "--perfil",
        "Vendas",
        "--dados",
        str(dados),
        "--trace",
        str(trace),
        *extra,
    ]


# --- Pedido completo, incompleto e opções -------------------------------------------


def test_pedido_completo_imprime_resposta_fontes_e_sai_zero(dados: Path, tmp_path: Path) -> None:
    modelo = LLMFalso(["```python\nprint(1)\n```", FINAL_OK])
    sandbox = SandboxRoteiro(
        [
            ExecDone(cell_id=1, stdout="1\n", stderr="", exception=None, duration_ms=1, final=None),
            _celula_final(),
        ]
    )
    fabrica = _Fabrica(sandbox)
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces", "--imagem", "imagem-teste"),
        modelo=modelo,
        saida=saida,
        fabrica_sandbox=fabrica,
    )

    assert codigo == 0
    texto = saida.getvalue()
    assert "resposta: Resposta pronta" in texto
    assert "fontes: vendas/pedidos.csv" in texto
    assert "motivo: final (completo: sim)" in texto
    assert "iterações: 2" in texto
    assert "custo total: indisponível" in texto
    # O sandbox foi iniciado uma vez e fechado uma vez (pelo Orquestrador).
    assert sandbox.inicios == 1
    assert sandbox.fechamentos == 1
    # A imagem e as montagens vêm do perfil Vendas: só comercial e vendas.
    imagem, montagens = fabrica.chamadas[0]
    assert imagem == "imagem-teste"
    assert montagens == {"comercial": dados / "comercial", "vendas": dados / "vendas"}
    # A trace foi gravada na pasta pedida e o caminho aparece na saída.
    arquivos = list((tmp_path / "traces").glob("*.jsonl"))
    assert len(arquivos) == 1
    assert str(arquivos[0]) in texto


def test_custo_total_soma_os_turnos_do_modelo(dados: Path, tmp_path: Path) -> None:
    modelo = LLMFalso(
        [
            RespostaLLM(
                texto="```python\nprint(1)\n```", tokens_entrada=3, tokens_saida=2, custo=0.001
            ),
            RespostaLLM(texto=FINAL_OK, tokens_entrada=4, tokens_saida=2, custo=0.002),
        ]
    )
    sandbox = SandboxRoteiro(
        [
            ExecDone(cell_id=1, stdout="1\n", stderr="", exception=None, duration_ms=1, final=None),
            _celula_final(),
        ]
    )
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces"),
        modelo=modelo,
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 0
    assert "custo total: US$ 0.003000" in saida.getvalue()
    assert "tokens: entrada 7, saída 4" in saida.getvalue()


def test_pedido_incompleto_sai_um(dados: Path, tmp_path: Path) -> None:
    modelo = LLMFalso(["só texto", "só texto", "só texto"])
    sandbox = SandboxRoteiro()
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces"),
        modelo=modelo,
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 1
    texto = saida.getvalue()
    assert "motivo: erro_irrecuperavel (completo: não)" in texto
    assert "fontes: nenhuma" in texto
    # Sem código válido, nada chegou ao sandbox, mas ele ainda foi fechado.
    assert sandbox.execucoes == 0
    assert sandbox.fechamentos == 1


def test_max_iteracoes_limita_as_chamadas_ao_modelo(dados: Path, tmp_path: Path) -> None:
    modelo = LLMFalso(["```python\nprint(1)\n```", "Resumo do que obtive."])
    sandbox = SandboxRoteiro(
        [ExecDone(cell_id=1, stdout="1\n", stderr="", exception=None, duration_ms=1, final=None)]
    )
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces", "--max-iteracoes", "1"),
        modelo=modelo,
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 1
    assert "motivo: orcamento_iteracoes" in saida.getvalue()
    assert "resposta: Resumo do que obtive." in saida.getvalue()
    assert len(modelo.chamadas) == 2


def test_orcamento_padrao_nao_e_alterado_sem_a_opcao(dados: Path, tmp_path: Path) -> None:
    assert Orcamento().max_iteracoes == 15
    modelo = LLMFalso([FINAL_OK])
    sandbox = SandboxRoteiro([_celula_final()])
    assert (
        main(
            _argv(dados, tmp_path / "traces"),
            modelo=modelo,
            saida=StringIO(),
            fabrica_sandbox=_Fabrica(sandbox),
        )
        == 0
    )


def test_falha_ao_iniciar_sandbox_sai_dois_sem_detalhe_do_host(dados: Path, tmp_path: Path) -> None:
    class SandboxQueNaoSobe(SandboxRoteiro):
        def start(self) -> None:
            raise RuntimeError("erro interno com /caminho/do/host")

    sandbox = SandboxQueNaoSobe()
    saida = StringIO()

    codigo = main(
        _argv(dados, tmp_path / "traces"),
        modelo=LLMFalso([FINAL_OK]),
        saida=saida,
        fabrica_sandbox=_Fabrica(sandbox),
    )

    assert codigo == 2
    assert "não foi possível iniciar o sandbox" in saida.getvalue()
    assert "/caminho/do/host" not in saida.getvalue()
    assert sandbox.fechamentos == 1


# --- Erros de uso e configuração ----------------------------------------------------


def test_perfil_inexistente_sai_dois(dados: Path, tmp_path: Path) -> None:
    saida = StringIO()
    codigo = main(
        ["Pedido", "--perfil", "Inexistente", "--dados", str(dados)],
        modelo=LLMFalso([]),
        saida=saida,
        fabrica_sandbox=_Fabrica(SandboxRoteiro()),
    )
    assert codigo == 2
    assert "perfil desconhecido" in saida.getvalue()
    assert "Vendas" in saida.getvalue()


def test_dados_inexistentes_saem_dois(tmp_path: Path) -> None:
    saida = StringIO()
    codigo = main(
        ["Pedido", "--perfil", "Vendas", "--dados", str(tmp_path / "nao_existe")],
        modelo=LLMFalso([]),
        saida=saida,
        fabrica_sandbox=_Fabrica(SandboxRoteiro()),
    )
    assert codigo == 2
    assert "pasta de dados não encontrada" in saida.getvalue()


def test_max_iteracoes_invalido_sai_dois(dados: Path) -> None:
    codigo = main(
        ["Pedido", "--perfil", "Vendas", "--dados", str(dados), "--max-iteracoes", "0"],
        modelo=LLMFalso([]),
        saida=StringIO(),
        fabrica_sandbox=_Fabrica(SandboxRoteiro()),
    )
    assert codigo == 2


def test_configuracao_ausente_sai_dois_sem_vazar_a_chave(dados: Path, ambiente_limpo: Path) -> None:
    # A chave existe, mas falta OPENROUTER_MODELO: a mensagem cita o nome da variável e nunca o
    # valor da chave.
    (ambiente_limpo / ".env").write_text(f"OPENROUTER_API_KEY={SEGREDO}\n", encoding="utf-8")
    saida = StringIO()

    codigo = main(
        _argv(dados, ambiente_limpo / "traces"),
        saida=saida,
        fabrica_sandbox=_Fabrica(SandboxRoteiro()),
    )

    assert codigo == 2
    texto = saida.getvalue()
    assert "OPENROUTER_MODELO" in texto
    assert SEGREDO not in texto
    assert "sk-or" not in texto


def test_chave_ausente_sai_dois_e_cita_o_nome_da_variavel(
    dados: Path, ambiente_limpo: Path
) -> None:
    (ambiente_limpo / ".env").write_text(
        "OPENROUTER_MODELO=provedor/modelo-teste\n", encoding="utf-8"
    )
    saida = StringIO()

    codigo = main(
        _argv(dados, ambiente_limpo / "traces"),
        saida=saida,
        fabrica_sandbox=_Fabrica(SandboxRoteiro()),
    )

    assert codigo == 2
    assert "OPENROUTER_API_KEY não definida" in saida.getvalue()


def test_modelo_definido_so_no_ambiente_conta_para_a_configuracao(
    dados: Path, ambiente_limpo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Com o modelo só no ambiente do processo, a configuração passa e a falta de chave é o erro.
    monkeypatch.setenv("OPENROUTER_MODELO", "provedor/modelo-teste")
    saida = StringIO()

    codigo = main(
        _argv(dados, ambiente_limpo / "traces"),
        saida=saida,
        fabrica_sandbox=_Fabrica(SandboxRoteiro()),
    )

    assert codigo == 2
    assert "OPENROUTER_API_KEY não definida" in saida.getvalue()
