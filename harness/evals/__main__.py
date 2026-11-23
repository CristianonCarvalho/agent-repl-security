"""Linha de comando da avaliação: `uv run python -m harness.evals` (T16).

Roda os casos ativos (ou só as categorias e grupos pedidos) com o modelo real do OpenRouter, grava o
relatório JSON e imprime um resumo em português. A configuração do modelo vem do `.env` do
diretório atual, com `os.environ` por cima, como na CLI do harness. A chave nunca é impressa.

Decisões deste módulo (não estão na spec):
- `main` recebe `executar` e `fabrica_modelo` para os testes. Sem eles, usa o executor real e o
  cliente do OpenRouter.
- Códigos de saída: 0 tudo certo; 1 houve `erro_infra` (ou o relatório não foi gravado); 2 uso
  ou configuração inválidos; 3 teto de gasto estourado, com precedência sobre o 1.
- A configuração do modelo é conferida antes de qualquer rodada: chave ou modelo ausentes saem
  com código 2 sem gastar nada. A mensagem cita só o nome da variável.
- Filtro de categoria: nome desconhecido sai com código 2. Categoria sem caso ativo (geração,
  alteração, injeção) é válida: se a seleção não tem nenhum caso ativo, a CLI não lê o `.env`,
  não confere a chave, não chama o modelo, lista os pendentes com o bloqueio de cada um e sai
  com 0.
- Os casos vêm de `conjunto_completo`: os 20 ativos e os 15 pendentes. Pendente não roda e não
  entra na taxa; só aparece em `pendentes` do relatório e no resumo.
- `--grupo` escolhe desenvolvimento, reservado ou todos (padrão). Como cada par de gêmeos está
  no mesmo grupo e na mesma categoria, nenhum filtro separa um negativo do seu controle.
- Erros de configuração do Pydantic mostram só o campo e a mensagem, nunca o valor recebido.
- Telemetria (T20a): ligada pelo mesmo ambiente da CLI. Config inválida sai com código 2 antes
  de qualquer rodada. O relatório ganha `telemetria` ("langfuse-us") só quando está ligada.
- Falhas de telemetria: o resumo mostra `Falhas de telemetria: N` quando a telemetria está ligada,
  com um aviso se N > 0. Falha de telemetria não muda o código de saída.
"""

import argparse
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TextIO

from pydantic import ValidationError

from harness.evals.casos import GRUPOS_DE_CASOS, Caso, Categoria
from harness.evals.conjunto import conjunto_completo
from harness.evals.dados import FatosVerdadeiros, gerar_dados
from harness.evals.executor import ConfigExecucao, executar_avaliacao
from harness.evals.relatorio import (
    Classificacao,
    ErroDeRelatorio,
    Relatorio,
    escrever_relatorio,
)
from harness.llm.base import ModeloLLM
from harness.llm.config import carregar_env, config_de_ambiente
from harness.llm.openrouter import ClienteOpenRouter
from harness.orchestrator.loop import Orcamento
from harness.sandbox.docker_sandbox import IMAGEM_PADRAO
from harness.telemetry.otel import (
    ConfigTelemetria,
    ErroDeTelemetria,
    TelemetriaOtel,
    criar_telemetria,
    descricao_telemetria,
    rotulo_telemetria,
)
from harness.telemetry.otel import (
    config_de_ambiente as config_telemetria,
)

ARQUIVO_ENV: Final = Path(".env")
PASTA_TRACE_PADRAO: Final = "traces/evals"
PASTA_SAIDA_PADRAO: Final = "avaliacoes"
CODIGO_OK: Final = 0
CODIGO_ERRO_INFRA: Final = 1
CODIGO_USO: Final = 2
CODIGO_TETO: Final = 3


class ErroDeUso(Exception):
    """Uso ou configuração inválidos. A mensagem nunca traz valores de chave."""


def _inteiro_positivo(texto: str) -> int:
    try:
        valor = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"esperado um inteiro, recebido {texto!r}") from None
    if valor < 1:
        raise argparse.ArgumentTypeError("deve ser pelo menos 1")
    return valor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.evals",
        description="Roda os casos de avaliação várias vezes e grava um relatório JSON.",
    )
    parser.add_argument("--rodadas", type=_inteiro_positivo, default=3, help="rodadas por caso")
    parser.add_argument("--semente", type=int, default=42, help="semente da geração dos dados")
    parser.add_argument(
        "--categoria",
        action="append",
        default=[],
        help="roda só esta categoria (pode repetir); padrão: todas",
    )
    parser.add_argument(
        "--grupo",
        choices=(*GRUPOS_DE_CASOS, "todos"),
        default="todos",
        help="roda só o grupo de casos (padrão: todos)",
    )
    parser.add_argument(
        "--teto-usd", type=float, default=2.0, help="teto de custo em dólares (padrão: 2.0)"
    )
    parser.add_argument(
        "--teto-tokens",
        type=int,
        default=2_000_000,
        help="teto de tokens somados (padrão: 2000000)",
    )
    parser.add_argument(
        "--imagem", default=IMAGEM_PADRAO, help=f"imagem do sandbox (padrão: {IMAGEM_PADRAO})"
    )
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path(PASTA_TRACE_PADRAO),
        help=f"pasta dos traces de cada rodada (padrão: {PASTA_TRACE_PADRAO})",
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=Path(PASTA_SAIDA_PADRAO),
        help=f"pasta do relatório (padrão: {PASTA_SAIDA_PADRAO})",
    )
    parser.add_argument(
        "--max-iteracoes",
        type=_inteiro_positivo,
        default=None,
        help="limite de chamadas ao modelo por pedido (padrão: o do Orcamento)",
    )
    return parser


def _categorias(valores: Sequence[str]) -> frozenset[Categoria]:
    """Categorias pedidas. Categoria sem caso ativo é válida: a seleção sai só com pendentes."""
    escolhidas: set[Categoria] = set()
    for valor in valores:
        try:
            escolhidas.add(Categoria(valor))
        except ValueError:
            nomes = ", ".join(sorted(c.value for c in Categoria))
            raise ErroDeUso(f"categoria desconhecida: {valor!r}. Categorias: {nomes}.") from None
    return frozenset(escolhidas)


def _config(args: argparse.Namespace) -> ConfigExecucao:
    try:
        orcamento = (
            Orcamento(max_iteracoes=args.max_iteracoes) if args.max_iteracoes is not None else None
        )
        return ConfigExecucao(
            rodadas=args.rodadas,
            semente=args.semente,
            teto_usd=args.teto_usd,
            teto_tokens=args.teto_tokens,
            imagem=args.imagem,
            pasta_trace=args.trace,
            orcamento=orcamento,
        )
    except ValidationError as erro:
        campos = "; ".join(
            f"{'.'.join(str(parte) for parte in e['loc'])}: {e['msg']}" for e in erro.errors()
        )
        raise ErroDeUso(f"configuração inválida: {campos}") from None


def _fabrica_de_modelo(ambiente: Mapping[str, str]) -> Callable[[], ModeloLLM]:
    """Confere a configuração do OpenRouter e devolve a fábrica de clientes (uma por rodada)."""
    try:
        configuracao = config_de_ambiente(ambiente)
    except ValueError as erro:
        raise ErroDeUso(f"configuração do modelo inválida: {erro}") from None
    chave = ambiente.get("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise ErroDeUso(
            f"OPENROUTER_API_KEY não definida: informe a chave no ambiente ou no arquivo "
            f"{ARQUIVO_ENV}."
        )
    return lambda: ClienteOpenRouter(configuracao, chave)


def _ambiente_do_processo() -> dict[str, str]:
    """`.env` do diretório atual, com `os.environ` por cima, como na CLI do harness."""
    return {**carregar_env(ARQUIVO_ENV), **os.environ}


def _config_telemetria(ambiente: Mapping[str, str]) -> ConfigTelemetria | None:
    """Configuração da telemetria, ou None quando está desligada. Erro vira ErroDeUso."""
    try:
        config = config_telemetria(ambiente)
    except ErroDeTelemetria as erro:
        raise ErroDeUso(str(erro)) from None
    return config if config.ligada else None


def _fabrica_de_telemetria(
    ambiente: Mapping[str, str],
) -> Callable[[], TelemetriaOtel | None]:
    """Uma telemetria por rodada, com a chave do OpenRouter na redação de segredos."""
    chave = ambiente.get("OPENROUTER_API_KEY", "").strip()
    segredos = [chave] if chave else []
    return lambda: criar_telemetria(ambiente, segredos=segredos)


def _selecionar(
    categorias: frozenset[Categoria], grupo: str
) -> Callable[[FatosVerdadeiros], list[Caso]]:
    """Função de casos para o executor: o conjunto completo, filtrado por categoria e por grupo.

    Pendentes entram na lista, mas o executor não os roda; ficam só em `pendentes` do relatório.
    """

    def selecionar(fatos: FatosVerdadeiros) -> list[Caso]:
        casos = conjunto_completo(fatos)
        if categorias:
            casos = [caso for caso in casos if caso.categoria in categorias]
        if grupo != "todos":
            casos = [caso for caso in casos if caso.grupo == grupo]
        return casos

    return selecionar


def _selecao_sem_modelo(categorias: frozenset[Categoria], grupo: str, semente: int) -> list[Caso]:
    """A seleção inteira, para decidir antes de qualquer configuração se há caso ativo.

    Gera os dados numa pasta temporária que some no fim. Não toca no modelo nem no `.env`.
    """
    with tempfile.TemporaryDirectory(prefix="selecao-") as temporaria:
        fatos = gerar_dados(Path(temporaria) / "dados", semente)
        return _selecionar(categorias, grupo)(fatos)


def _sem_casos_ativos(
    saida: TextIO, selecao: Sequence[Caso], categorias: frozenset[Categoria], grupo: str
) -> None:
    """Mensagem para uma seleção só com pendentes: lista cada um com o que o destrava."""
    filtro = ", ".join(sorted(c.value for c in categorias)) or "todas"
    linhas = [
        f"Não há caso ativo na seleção (categorias: {filtro}; grupo: {grupo}). "
        "Nenhum modelo foi chamado.",
        f"Pendentes selecionados: {len(selecao)}",
    ]
    for caso in selecao:
        bloqueios = ", ".join(caso.situacao.bloqueios)
        linhas.append(f"  {caso.id}: bloqueios: {bloqueios}")
    saida.write("\n".join(linhas) + "\n")


def _percentual(taxa: float | None) -> str:
    if taxa is None:
        return "sem dado"
    return f"{taxa * 100:.1f}%".replace(".", ",")


def _br(valor: float) -> str:
    return f"{valor:.4f}".replace(".", ",")


def _resumo(saida: TextIO, relatorio: Relatorio, caminho: Path | None) -> None:
    linhas = [
        "",
        f"Taxa total de sucesso: {_percentual(relatorio.taxa_total)}",
    ]
    if relatorio.desvio_entre_rodadas is not None:
        pontos = _br(relatorio.desvio_entre_rodadas * 100)
        linhas.append(f"Variação entre rodadas (desvio padrão): {pontos} p.p.")
    linhas.append("Por categoria:")
    for categoria in relatorio.categorias:
        linhas.append(
            f"  {categoria.categoria.value}: {_percentual(categoria.taxa)} "
            f"({categoria.casos_considerados} caso(s) com taxa)"
        )
    if relatorio.custo_total is None:
        linhas.append("Custo: indisponível (o provedor não informou)")
    else:
        linhas.append(f"Custo: US$ {_br(relatorio.custo_total)}")
    if relatorio.rodadas_sem_custo:
        linhas.append(f"  rodadas sem custo informado: {relatorio.rodadas_sem_custo}")
    if relatorio.grupos:
        linhas.append("Por grupo:")
        for grupo in relatorio.grupos:
            linhas.append(
                f"  {grupo.grupo}: {_percentual(grupo.taxa)} "
                f"({grupo.casos_considerados} caso(s) com taxa)"
            )
    linhas.append(
        f"Tokens: entrada {relatorio.tokens_entrada_total}, saída {relatorio.tokens_saida_total}"
    )
    linhas.append(f"Erros de infraestrutura: {relatorio.erros_infra}")
    linhas.append(f"Rodadas não executadas: {relatorio.nao_executadas}")
    if relatorio.pendentes:
        ids = ", ".join(relatorio.pendentes)
        linhas.append(f"Pendentes: {len(relatorio.pendentes)} ({ids})")
    if relatorio.teto_estourado:
        linhas.append(f"Teto de gasto: estourado ({relatorio.teto_motivo})")
    else:
        linhas.append("Teto de gasto: dentro do limite")
    linhas.append(f"Provedor(es) efetivo(s): {', '.join(relatorio.provedores) or 'nenhum'}")
    if relatorio.falhas_telemetria is not None:
        linhas.append(f"Falhas de telemetria: {relatorio.falhas_telemetria}")
        if relatorio.falhas_telemetria > 0:
            linhas.append(
                "aviso: houve falhas ao enviar a telemetria ao Langfuse. Confira as chaves, a "
                "região e a rede. O trace JSONL local continua completo."
            )
    linhas.append(f"Relatório: {caminho if caminho is not None else 'não gravado'}")
    saida.write("\n".join(linhas) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    saida: TextIO = sys.stdout,
    executar: Callable[..., Relatorio] | None = None,
    fabrica_modelo: Callable[[], ModeloLLM] | None = None,
) -> int:
    """Roda a avaliação pela linha de comando e devolve o código de saída.

    `executar` e `fabrica_modelo` existem para os testes: sem eles, usam o executor real e o
    cliente do OpenRouter (configurado pelo `.env` e pelo ambiente).
    """
    try:
        args = _parser().parse_args(argv)
    except SystemExit as erro:
        return erro.code if isinstance(erro.code, int) else CODIGO_USO

    try:
        categorias = _categorias(args.categoria)
        config = _config(args)
    except ErroDeUso as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO

    # Sem caso ativo na seleção, nada roda: não lê o `.env`, não confere a chave e não chama o
    # modelo. Só lista os pendentes, cada um com o que o destrava, e sai com zero.
    selecao = _selecao_sem_modelo(categorias, args.grupo, args.semente)
    if not any(caso.situacao.ativo for caso in selecao):
        _sem_casos_ativos(saida, selecao, categorias, args.grupo)
        return CODIGO_OK

    try:
        ambiente = _ambiente_do_processo()
        if fabrica_modelo is None:
            fabrica_modelo = _fabrica_de_modelo(ambiente)
        configuracao_telemetria = _config_telemetria(ambiente)
    except ErroDeUso as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO

    fabrica_telemetria: Callable[[], TelemetriaOtel | None] | None = None
    if configuracao_telemetria is not None:
        saida.write(f"Telemetria: {descricao_telemetria(configuracao_telemetria)}\n")
        fabrica_telemetria = _fabrica_de_telemetria(ambiente)

    def progresso(caso_id: str, numero: int, classificacao: Classificacao) -> None:
        saida.write(f"{caso_id} rodada {numero}/{config.rodadas}: {classificacao.value}\n")

    executor = executar if executar is not None else executar_avaliacao
    extra: dict[str, Any] = {}
    if fabrica_telemetria is not None:
        extra["fabrica_telemetria"] = fabrica_telemetria
    try:
        relatorio = executor(
            _selecionar(categorias, args.grupo),
            fabrica_modelo,
            config,
            aoprogresso=progresso,
            **extra,
        )
    except Exception as erro:
        saida.write(f"a avaliação não pôde ser concluída ({type(erro).__name__})\n")
        return CODIGO_ERRO_INFRA
    if configuracao_telemetria is not None:
        relatorio = relatorio.model_copy(
            update={"telemetria": rotulo_telemetria(configuracao_telemetria)}
        )

    caminho: Path | None = None
    try:
        caminho = escrever_relatorio(relatorio, args.saida)
    except ErroDeRelatorio as erro:
        saida.write(f"{erro}\n")
    _resumo(saida, relatorio, caminho)

    if relatorio.teto_estourado:
        return CODIGO_TETO
    if relatorio.erros_infra or caminho is None:
        return CODIGO_ERRO_INFRA
    return CODIGO_OK


if __name__ == "__main__":
    sys.exit(main())
