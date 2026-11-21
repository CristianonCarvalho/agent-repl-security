"""Linha de comando da avaliação: `uv run python -m harness.evals` (T16).

Roda os casos de exemplo (ou só as categorias pedidas) com o modelo real do OpenRouter, grava o
relatório JSON e imprime um resumo em português. A configuração do modelo vem do `.env` do
diretório atual, com `os.environ` por cima, como na CLI do harness. A chave nunca é impressa.

Decisões deste módulo (não estão na spec):
- `main` recebe `executar` e `fabrica_modelo` para os testes. Sem eles, usa o executor real e o
  cliente do OpenRouter.
- Códigos de saída: 0 tudo certo; 1 houve `erro_infra` (ou o relatório não foi gravado); 2 uso
  ou configuração inválidos; 3 teto de gasto estourado, com precedência sobre o 1.
- A configuração do modelo é conferida antes de qualquer rodada: chave ou modelo ausentes saem
  com código 2 sem gastar nada. A mensagem cita só o nome da variável.
- Filtro de categoria: nome desconhecido e categoria sem caso de exemplo saem com código 2.
- Erros de configuração do Pydantic mostram só o campo e a mensagem, nunca o valor recebido.
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final, TextIO

from pydantic import ValidationError

from harness.evals.casos import Caso, Categoria
from harness.evals.casos_exemplo import CATEGORIAS_COM_EXEMPLO, casos_de_exemplo
from harness.evals.dados import FatosVerdadeiros
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
        help="roda só esta categoria (pode repetir); padrão: todas com caso de exemplo",
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
    escolhidas: set[Categoria] = set()
    for valor in valores:
        try:
            categoria = Categoria(valor)
        except ValueError:
            nomes = ", ".join(sorted(c.value for c in Categoria))
            raise ErroDeUso(f"categoria desconhecida: {valor!r}. Categorias: {nomes}.") from None
        if categoria not in CATEGORIAS_COM_EXEMPLO:
            raise ErroDeUso(f"a categoria {valor!r} ainda não tem caso de exemplo")
        escolhidas.add(categoria)
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


def _selecionar(categorias: frozenset[Categoria]) -> Callable[[FatosVerdadeiros], list[Caso]]:
    """Função de casos para o executor: os exemplos, filtrados pelas categorias pedidas."""

    def selecionar(fatos: FatosVerdadeiros) -> list[Caso]:
        casos = casos_de_exemplo(fatos)
        if not categorias:
            return casos
        return [caso for caso in casos if caso.categoria in categorias]

    return selecionar


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
    linhas.append(
        f"Tokens: entrada {relatorio.tokens_entrada_total}, saída {relatorio.tokens_saida_total}"
    )
    linhas.append(f"Erros de infraestrutura: {relatorio.erros_infra}")
    linhas.append(f"Rodadas não executadas: {relatorio.nao_executadas}")
    linhas.append(f"Pendentes: {', '.join(relatorio.pendentes) or 'nenhum'}")
    if relatorio.teto_estourado:
        linhas.append(f"Teto de gasto: estourado ({relatorio.teto_motivo})")
    else:
        linhas.append("Teto de gasto: dentro do limite")
    linhas.append(f"Provedor(es) efetivo(s): {', '.join(relatorio.provedores) or 'nenhum'}")
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
        if fabrica_modelo is None:
            ambiente = {**carregar_env(ARQUIVO_ENV), **os.environ}
            fabrica_modelo = _fabrica_de_modelo(ambiente)
    except ErroDeUso as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO

    def progresso(caso_id: str, numero: int, classificacao: Classificacao) -> None:
        saida.write(f"{caso_id} rodada {numero}/{config.rodadas}: {classificacao.value}\n")

    executor = executar if executar is not None else executar_avaliacao
    try:
        relatorio = executor(_selecionar(categorias), fabrica_modelo, config, aoprogresso=progresso)
    except Exception as erro:
        saida.write(f"a avaliação não pôde ser concluída ({type(erro).__name__})\n")
        return CODIGO_ERRO_INFRA

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
