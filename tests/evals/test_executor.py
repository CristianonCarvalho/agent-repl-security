"""Testes do executor da avaliação (T16).

A maioria usa um sandbox falso e um LLM falso, injetados pela fábrica. Só o último teste sobe
um contêiner real, com o marcador `docker` (pulado sem Docker).
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from harness.evals import executor as modulo_executor
from harness.evals.casos import (
    Caso,
    Categoria,
    CitaFontes,
    ContemNumero,
    Situacao,
    formatar_numero_br,
)
from harness.evals.conjunto import casos_ativos, conjunto_completo
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.executor import ConfigExecucao, ModeloContabil, executar_avaliacao
from harness.evals.relatorio import Classificacao, Relatorio, relatorio_para_json
from harness.llm.base import ErroLLM, LLMFalso, Mensagem, RespostaLLM
from harness.orchestrator.loop import Orcamento
from harness.sandbox.docker_sandbox import (
    ROTULO,
    ResultadoExecucao,
    SandboxMorreu,
)
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

FONTE: str = "vendas/pedidos_2026-10.xlsx"
CODIGO: str = "```python\nFINAL('Total', ['vendas/pedidos_2026-10.xlsx'])\n```"
MENSAGENS: list[Mensagem] = [Mensagem(role="user", content="oi")]


@pytest.fixture(scope="module")
def fatos(tmp_path_factory: pytest.TempPathFactory) -> FatosVerdadeiros:
    """Os fatos da semente padrão. Os testes usam a mesma semente que o executor."""
    return gerar_dados(tmp_path_factory.mktemp("referencia") / "dados", 42)


def _resposta_certa(fatos: FatosVerdadeiros) -> str:
    return f"O total é R$ {formatar_numero_br(fatos.valor_total_validos)}."


def _caso(fatos: FatosVerdadeiros, caso_id: str = "consulta-01", ativo: bool = True) -> Caso:
    situacao = (
        Situacao(ativo=True)
        if ativo
        else Situacao(
            ativo=False,
            motivo_pendente="gabarito ainda não revisado",
            bloqueios=("corretor_de_arquivos",),
        )
    )
    return Caso(
        id=caso_id,
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o total válido? Responda só com o valor e cite a fonte.",
        situacao=situacao,
        gabarito=[
            ContemNumero(valor=fatos.valor_total_validos),
            CitaFontes(fontes=[FONTE]),
        ],
        comportamento_esperado=None if ativo else "A resposta cita o total válido.",
    )


class SandboxFalso:
    """Sandbox sem Docker. Cada célula devolve o próximo texto de `respostas`.

    Um None em `respostas` significa célula sem FINAL.
    """

    def __init__(
        self,
        respostas: Sequence[str | None],
        *,
        falhar_start: bool = False,
        falhar_execucao: bool = False,
        falhar_reinicio: bool = False,
    ) -> None:
        self._respostas = list(respostas)
        self._falhar_start = falhar_start
        self._falhar_execucao = falhar_execucao
        self._falhar_reinicio = falhar_reinicio
        self.iniciado = False
        self.fechamentos = 0
        self.celulas = 0

    def start(self) -> None:
        if self._falhar_start:
            raise RuntimeError("falha interna: contêiner 10.0.0.7 recusou a conexão")
        self.iniciado = True

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        if self._falhar_execucao:
            raise SandboxMorreu("contêiner caiu")
        self.celulas += 1
        texto = self._respostas.pop(0) if len(self._respostas) > 1 else self._respostas[0]
        final = RespostaFinal(answer=texto, sources=[FONTE]) if texto is not None else None
        done = ExecDone(
            cell_id=cell_id,
            stdout="",
            stderr="",
            exception=None,
            duration_ms=1,
            final=final,
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        if self._falhar_reinicio:
            raise RuntimeError("reinício indisponível")

    def carregar_contexto(self, valor: Any) -> None:
        return None

    def close(self) -> None:
        self.fechamentos += 1


@dataclass
class Fabrica:
    """Fábrica de sandboxes falsos. Guarda cada sandbox criado e as montagens recebidas."""

    respostas: list[str | None]
    falhar_start: bool = False
    falhar_execucao: bool = False
    falhar_reinicio: bool = False
    criados: list[SandboxFalso] = field(default_factory=list)
    montagens: list[dict[str, Path]] = field(default_factory=list)

    def __call__(self, montagens: dict[str, Path]) -> SandboxFalso:
        sandbox = SandboxFalso(
            self.respostas,
            falhar_start=self.falhar_start,
            falhar_execucao=self.falhar_execucao,
            falhar_reinicio=self.falhar_reinicio,
        )
        self.criados.append(sandbox)
        self.montagens.append(montagens)
        return sandbox


class RelogioQueAvanca:
    """Relógio que anda meio segundo a cada leitura."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        self._t += 0.5
        return self._t


def _config(**kwargs: Any) -> ConfigExecucao:
    return ConfigExecucao(**kwargs)


def _modelo(*textos: str | RespostaLLM) -> Callable[[], LLMFalso]:
    """Fábrica que devolve um LLMFalso com o mesmo roteiro a cada rodada."""
    return lambda: LLMFalso(list(textos))


def _executar(
    fatos: FatosVerdadeiros,
    fabrica_sandbox: Fabrica | Callable[[dict[str, Path]], SandboxFalso],
    modelo: Callable[[], Any],
    config: ConfigExecucao,
    *,
    casos: Sequence[Caso] | None = None,
    relogio: Callable[[], float] | None = None,
    aoprogresso: Callable[[str, int, Classificacao], None] | None = None,
) -> Relatorio:
    lista = list(casos) if casos is not None else [_caso(fatos)]
    return executar_avaliacao(
        lambda _fatos: lista,
        modelo,
        config,
        fabrica_sandbox=fabrica_sandbox,
        relogio=relogio if relogio is not None else RelogioQueAvanca(),
        aoprogresso=aoprogresso,
    )


def _primeira_rodada(relatorio: Relatorio) -> Any:
    return relatorio.casos[0].rodadas[0]


# --- ModeloContabil ---------------------------------------------------------------------------


def test_modelo_contabil_soma_tokens_e_custos() -> None:
    modelo = LLMFalso(
        [
            RespostaLLM(
                texto="a", tokens_entrada=10, tokens_saida=2, custo=0.5, provedor="P1", modelo="M"
            ),
            RespostaLLM(
                texto="b", tokens_entrada=5, tokens_saida=1, custo=0.25, provedor="P1", modelo="M"
            ),
        ]
    )
    contabil = ModeloContabil(modelo)

    contabil.responder(MENSAGENS)
    contabil.responder(MENSAGENS)

    assert contabil.tokens_entrada == 15
    assert contabil.tokens_saida == 3
    assert contabil.respostas == 2
    assert contabil.custo_total == pytest.approx(0.75)
    assert contabil.provedores == frozenset({"P1"})
    assert contabil.modelos == frozenset({"M"})


def test_modelo_contabil_nao_inventa_custo() -> None:
    contabil = ModeloContabil(LLMFalso([RespostaLLM(texto="a", tokens_entrada=1, tokens_saida=1)]))

    contabil.responder(MENSAGENS)

    assert contabil.custo_total is None
    assert contabil.respostas_sem_custo == 1


def test_modelo_contabil_com_custo_parcial_soma_so_o_conhecido() -> None:
    contabil = ModeloContabil(
        LLMFalso(
            [
                RespostaLLM(texto="a", tokens_entrada=1, tokens_saida=1, custo=0.5),
                RespostaLLM(texto="b", tokens_entrada=1, tokens_saida=1),
            ]
        )
    )

    contabil.responder(MENSAGENS)
    contabil.responder(MENSAGENS)

    assert contabil.custo_total == pytest.approx(0.5)
    assert contabil.respostas_sem_custo == 1


def test_modelo_contabil_repassa_a_resposta_intacta() -> None:
    original = RespostaLLM(texto="resposta crua", tokens_entrada=3, tokens_saida=4)
    contabil = ModeloContabil(LLMFalso([original]))

    assert contabil.responder(MENSAGENS) is original


class _ModeloQueFalha:
    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        raise RuntimeError("segredo do provedor")


def test_modelo_contabil_repassa_a_excecao_sem_contar() -> None:
    contabil = ModeloContabil(_ModeloQueFalha())

    with pytest.raises(RuntimeError, match="segredo do provedor"):
        contabil.responder(MENSAGENS)
    assert contabil.respostas == 0
    assert contabil.tokens_entrada == 0


def test_modelo_contabil_provedores_sao_unicos() -> None:
    contabil = ModeloContabil(
        LLMFalso(
            [
                RespostaLLM(texto="a", tokens_entrada=1, tokens_saida=1, provedor="X"),
                RespostaLLM(texto="b", tokens_entrada=1, tokens_saida=1, provedor="X"),
            ]
        )
    )

    contabil.responder(MENSAGENS)
    contabil.responder(MENSAGENS)

    assert contabil.provedores == frozenset({"X"})


# --- configuração -----------------------------------------------------------------------------


def test_config_padrao_e_validacoes() -> None:
    config = ConfigExecucao()

    assert config.rodadas == 3
    assert config.semente == 42
    assert config.teto_usd == 2.0
    assert config.teto_tokens == 2_000_000
    assert config.imagem == "harness-sandbox:dev"
    with pytest.raises(ValidationError):
        ConfigExecucao(rodadas=0)
    with pytest.raises(ValidationError):
        ConfigExecucao(teto_usd=0)
    with pytest.raises(ValidationError):
        ConfigExecucao(teto_tokens=0)


# --- rodadas ----------------------------------------------------------------------------------


def test_rodada_aprovada(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=1))

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.APROVADA
    assert rodada.veredito is not None
    assert rodada.veredito.aprovado is True
    assert rodada.motivo_termino == "final"
    assert relatorio.taxa_total == 1.0
    assert relatorio.erros_infra == 0


def test_resposta_errada_e_reprovada(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=["O total é R$ 1,00."])

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=1))

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.REPROVADA
    assert rodada.veredito is not None
    assert rodada.veredito.aprovado is False
    assert relatorio.taxa_total == 0.0


def test_sem_final_por_orcamento_de_iteracoes_e_reprovada(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[None])
    config = _config(rodadas=1, orcamento=Orcamento(max_iteracoes=1))

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO, "Resumo do que obtive."), config)

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.REPROVADA
    assert rodada.motivo_termino == "orcamento_iteracoes"
    assert rodada.veredito is not None
    assert rodada.veredito.aprovado is False
    assert rodada.veredito.regras[0].descricao == "termina com FINAL"


def test_erro_do_modelo_vira_erro_infra_com_nome_do_estado(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    relatorio = _executar(fatos, fabrica, lambda: _ModeloQueFalha(), _config(rodadas=1))

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == "erro_llm: RuntimeError"
    assert rodada.veredito is None
    assert relatorio.erros_infra == 1
    assert relatorio.taxa_total is None
    assert "segredo do provedor" not in relatorio_para_json(relatorio)


def test_erro_llm_leva_a_mensagem_segura_de_errollm_ao_relatorio(fatos: FatosVerdadeiros) -> None:
    mensagem = "falha ao consultar o OpenRouter (InternalServerError, status 500) após 3 tentativas"

    class _ModeloComErroLLM:
        def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
            raise ErroLLM(mensagem)

    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    relatorio = _executar(fatos, fabrica, lambda: _ModeloComErroLLM(), _config(rodadas=1))

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == f"erro_llm: ErroLLM: {mensagem}"
    texto = relatorio_para_json(relatorio)
    assert json.loads(texto)["casos"][0]["rodadas"][0]["detalhe"] == rodada.detalhe
    assert "status 500" in texto


def test_erro_do_sandbox_sem_reinicio_vira_erro_infra(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[None], falhar_execucao=True, falhar_reinicio=True)

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=1))

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == "erro_sandbox"


def test_falha_ao_criar_sandbox_vira_erro_infra_sem_a_mensagem(fatos: FatosVerdadeiros) -> None:
    def fabrica_quebrada(montagens: dict[str, Path]) -> SandboxFalso:
        raise RuntimeError("segredo-do-host-xyz em /var/run/docker.sock")

    relatorio = _executar(fatos, fabrica_quebrada, _modelo(CODIGO), _config(rodadas=1))

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.ERRO_INFRA
    assert rodada.detalhe == "RuntimeError"
    assert "segredo" not in relatorio_para_json(relatorio)


def test_falha_ao_iniciar_sandbox_fecha_o_sandbox(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[None], falhar_start=True)

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=1))

    assert _primeira_rodada(relatorio).classificacao is Classificacao.ERRO_INFRA
    assert _primeira_rodada(relatorio).detalhe == "RuntimeError"
    assert len(fabrica.criados) == 1
    assert fabrica.criados[0].fechamentos == 1
    assert "10.0.0.7" not in relatorio_para_json(relatorio)


def test_falha_na_fabrica_do_modelo_nao_derruba_a_avaliacao(fatos: FatosVerdadeiros) -> None:
    def modelo_quebrado() -> LLMFalso:
        raise ValueError("chave ausente em algum lugar")

    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    relatorio = _executar(fatos, fabrica, modelo_quebrado, _config(rodadas=2))

    assert [r.classificacao for r in relatorio.casos[0].rodadas] == [Classificacao.ERRO_INFRA] * 2
    assert all(r.detalhe == "ValueError" for r in relatorio.casos[0].rodadas)
    assert "chave ausente" not in relatorio_para_json(relatorio)


def test_um_sandbox_novo_por_rodada_e_todos_fechados(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=3))

    assert len(fabrica.criados) == 3
    assert all(sandbox.iniciado for sandbox in fabrica.criados)
    assert all(sandbox.fechamentos == 1 for sandbox in fabrica.criados)
    assert all(montagem.keys() == {"vendas", "comercial"} for montagem in fabrica.montagens)


def test_caso_pendente_fica_fora_das_rodadas(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])
    casos = [_caso(fatos), _caso(fatos, caso_id="consulta-02", ativo=False)]

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=2), casos=casos)

    por_id = {resumo.caso_id: resumo for resumo in relatorio.casos}
    assert relatorio.pendentes == ("consulta-02",)
    assert por_id["consulta-02"].rodadas == ()
    assert len(por_id["consulta-01"].rodadas) == 2
    assert len(fabrica.criados) == 2


def test_aoprogresso_recebe_cada_rodada(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])
    vistos: list[tuple[str, int, Classificacao]] = []

    _executar(
        fatos,
        fabrica,
        _modelo(CODIGO),
        _config(rodadas=2),
        aoprogresso=lambda caso, numero, classe: vistos.append((caso, numero, classe)),
    )

    assert vistos == [
        ("consulta-01", 1, Classificacao.APROVADA),
        ("consulta-01", 2, Classificacao.APROVADA),
    ]


def test_duracao_vem_do_relogio_injetado(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=1))

    assert _primeira_rodada(relatorio).duracao_s > 0


def test_trace_por_rodada_quando_pasta_e_dada(fatos: FatosVerdadeiros, tmp_path: Path) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])
    pasta = tmp_path / "trace"

    relatorio = _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=2, pasta_trace=pasta))

    arquivos = {str(arquivo) for arquivo in pasta.glob("*.jsonl")}
    caminhos = [r.caminho_trace for r in relatorio.casos[0].rodadas]
    assert len(arquivos) == 2
    assert set(caminhos) == arquivos
    assert all(Path(caminho).exists() for caminho in caminhos if caminho is not None)


def test_teto_de_tokens_marca_o_resto_como_nao_executada(fatos: FatosVerdadeiros) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    relatorio = _executar(
        fatos, fabrica, _modelo(CODIGO), _config(rodadas=3, teto_tokens=1, teto_usd=None)
    )

    rodadas = relatorio.casos[0].rodadas
    assert [r.classificacao for r in rodadas][0] is Classificacao.APROVADA
    assert [r.classificacao for r in rodadas][1:] == [Classificacao.NAO_EXECUTADA] * 2
    assert len(fabrica.criados) == 1
    assert relatorio.teto_estourado is True
    assert relatorio.teto_motivo is not None
    assert "tokens" in relatorio.teto_motivo
    assert relatorio.nao_executadas == 2


def test_teto_de_usd_para_no_ponto_certo_e_marca_os_casos_seguintes(
    fatos: FatosVerdadeiros,
) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])
    casos = [_caso(fatos), _caso(fatos, caso_id="consulta-02")]
    # Cada rodada custa 1,5 dólar: a primeira passa do teto de 1 dólar e o resto para.
    cara = RespostaLLM(texto=CODIGO, tokens_entrada=1, tokens_saida=1, custo=1.5)

    relatorio = _executar(
        fatos, fabrica, _modelo(cara), _config(rodadas=2, teto_usd=1.0), casos=casos
    )

    assert len(fabrica.criados) == 1
    classificacoes = [r.classificacao for c in relatorio.casos for r in c.rodadas]
    assert classificacoes == [
        Classificacao.APROVADA,
        Classificacao.NAO_EXECUTADA,
        Classificacao.NAO_EXECUTADA,
        Classificacao.NAO_EXECUTADA,
    ]
    assert relatorio.custo_total == pytest.approx(1.5)
    assert relatorio.teto_motivo is not None
    assert "US$" in relatorio.teto_motivo
    assert relatorio.teto_estourado is True


def test_diretorio_temporario_some_no_fim(
    fatos: FatosVerdadeiros, monkeypatch: pytest.MonkeyPatch
) -> None:
    fabrica = Fabrica(respostas=[_resposta_certa(fatos)])

    _executar(fatos, fabrica, _modelo(CODIGO), _config(rodadas=1))

    raiz = fabrica.montagens[0]["vendas"].parent
    assert not raiz.exists()


def test_diretorio_temporario_some_mesmo_com_erro(
    fatos: FatosVerdadeiros, monkeypatch: pytest.MonkeyPatch
) -> None:
    destinos: list[Path] = []
    original = gerar_dados

    def espiao(destino: Path, semente: int = 42) -> FatosVerdadeiros:
        destinos.append(destino)
        return original(destino, semente)

    monkeypatch.setattr(modulo_executor, "gerar_dados", espiao)

    def casos_quebrados(_fatos: FatosVerdadeiros) -> list[Caso]:
        raise RuntimeError("casos inválidos")

    with pytest.raises(RuntimeError, match="casos inválidos"):
        executar_avaliacao(
            casos_quebrados,
            _modelo(CODIGO),
            _config(rodadas=1),
            fabrica_sandbox=Fabrica(respostas=[None]),
        )

    assert len(destinos) == 1
    assert not destinos[0].parent.exists()


def test_privacidade_o_relatorio_nao_leva_chave_nem_texto_de_excecao(
    fatos: FatosVerdadeiros, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "chave-falsa-987654")

    def fabrica_quebrada(montagens: dict[str, Path]) -> SandboxFalso:
        raise RuntimeError("detalhe com chave-falsa-987654 e /caminho/secreto")

    relatorio = _executar(fatos, fabrica_quebrada, _modelo(CODIGO), _config(rodadas=1))

    texto = relatorio_para_json(relatorio)
    assert "chave-falsa-987654" not in texto
    assert "caminho/secreto" not in texto
    assert json.loads(texto)["casos"][0]["rodadas"][0]["detalhe"] == "RuntimeError"


# --- Docker -----------------------------------------------------------------------------------


@pytest.mark.docker
def test_rodada_real_no_docker_aprovada_e_sem_conteiner_sobrando(
    fatos: FatosVerdadeiros, imagem: str, cliente_docker: Any
) -> None:
    cliente = fatos.maior_pedido.cliente
    total = fatos.total_por_cliente[cliente]
    # O modelo roteirizado lê a planilha de /dados e calcula o total válido, sem pedidos cancelados.
    codigo = (
        "import openpyxl\n"
        "planilha = openpyxl.load_workbook('/dados/vendas/pedidos_2026-10.xlsx', read_only=True)\n"
        "linhas = list(planilha['pedidos'].iter_rows(values_only=True))[1:]\n"
        f"total = sum(l[4] for l in linhas if l[1] == {cliente!r} and l[6] != 'cancelado')\n"
        "texto = f'{total:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')\n"
        "FINAL(f'O total válido é R$ {texto}.', ['vendas/pedidos_2026-10.xlsx'])\n"
    )
    roteiro = f"Vou somar os pedidos válidos.\n```python\n{codigo}```"
    caso = Caso(
        id="consulta-01",
        categoria=Categoria.CONSULTA,
        perfil="Vendas",
        pedido="Qual é o total válido do cliente? Responda só com o valor e cite a fonte.",
        situacao=Situacao(ativo=True),
        gabarito=[ContemNumero(valor=total), CitaFontes(fontes=[FONTE])],
    )

    relatorio = executar_avaliacao(
        lambda _fatos: [caso],
        _modelo(roteiro),
        _config(rodadas=1, imagem=imagem),
        relogio=RelogioQueAvanca(),
    )

    rodada = _primeira_rodada(relatorio)
    assert rodada.classificacao is Classificacao.APROVADA
    assert rodada.motivo_termino == "final"
    assert cliente_docker.containers.list(all=True, filters={"label": ROTULO}) == []


# --- conjunto completo (T17b): pendentes não rodam e não mexem nas taxas ----------------------


def test_pendentes_do_conjunto_completo_nao_rodam_e_nao_mexem_nas_taxas(
    fatos: FatosVerdadeiros,
) -> None:
    ativos_fabrica = Fabrica(respostas=[_resposta_certa(fatos)])
    so_ativos = _executar(
        fatos,
        ativos_fabrica,
        _modelo(CODIGO),
        _config(rodadas=2),
        casos=casos_ativos(fatos),
    )
    completo_fabrica = Fabrica(respostas=[_resposta_certa(fatos)])
    completo = _executar(
        fatos,
        completo_fabrica,
        _modelo(CODIGO),
        _config(rodadas=2),
        casos=conjunto_completo(fatos),
    )

    # Só os 20 ativos viram sandbox: 20 casos vezes 2 rodadas.
    assert len(completo_fabrica.criados) == len(ativos_fabrica.criados) == 40
    assert completo.taxa_total == so_ativos.taxa_total
    assert completo.categorias == so_ativos.categorias
    assert completo.grupos == so_ativos.grupos
    assert completo.pendentes == tuple(
        f"{categoria}-{numero:02d}"
        for categoria in ("geracao", "alteracao", "injecao")
        for numero in range(1, 6)
    )
    por_id = {resumo.caso_id: resumo for resumo in completo.casos}
    for id_pendente in completo.pendentes:
        assert por_id[id_pendente].rodadas == (), id_pendente
    assert sum(len(r.rodadas) for r in completo.casos if r.situacao == "ativo") == 40
