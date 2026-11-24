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
- Provedor (decisões desta CLI): `--provedor NOME` sobrescreve `OPENROUTER_PROVEDOR` nas
  avaliações e é validado antes de tudo (nome inválido sai com 2). O relatório grava o pedido em
  `provedor_solicitado`. `--salvar-linha-de-base` exige provedor fixado (pela opção ou pela
  variável), conferido antes de qualquer rodada, porque a linha de base precisa apontar sempre
  para o mesmo provedor.
- `--pausa-s SEGUNDOS` (padrão 0) espera entre uma rodada executada e a próxima. Não conta no
  tempo da rodada, e não há pausa antes da primeira nem depois da última.
- `--escala N` (T18c-a, padrão 1) multiplica os pedidos: a planilha tem 60 * N linhas. Com escala
  diferente de 1, o conjunto congelado v1 não vale: `versao_conjunto` do relatório vira
  "nao-congelado-escala-N" e `--salvar-linha-de-base` é recusado antes de qualquer rodada. O
  relatório registra `escala` e `linhas_pedidos`, e `comparar` recusa escalas diferentes.

Subcomandos (T17c), reconhecidos só quando são o primeiro argumento exato: `comparar BASE NOVO`
(0 ok, 1 regressão provável, 2 incomparável ou erro) e `congelar --versao vN` (cria o conjunto em
evals/conjuntos/, recusa sobrescrever). Sem subcomando, roda a avaliação, como antes.
`graficos DADOS.json [--saida PASTA]` (T17d) gera os SVG e a página HTML, sem modelo.
`graficos --de-relatorios A.json B.json ... [--saida PASTA]` (Ajuste 2) faz o mesmo a partir de
relatórios de execução, um por execução; o padrão de `--saida` é
docs/relatorios/comparacao-variantes.
DADOS.json e --de-relatorios são excludentes: sem um ou com os dois, sai com 2.
`--salvar-linha-de-base NOME` confere tudo antes de rodar (conjunto completo, 3+ rodadas, nome
válido, arquivo novo, árvore limpa) e só grava se a execução não teve teto, erro de infraestrutura
nem rodada não executada. A procedência (commit, árvore suja, versão do conjunto, orçamento e
início) vai para todo relatório.
"""

import argparse
import math
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TextIO

from pydantic import ValidationError

from harness.evals.casos import GRUPOS_DE_CASOS, Caso, Categoria
from harness.evals.comparacao import (
    FATOR_RUIDO_PADRAO,
    PISO_PP_PADRAO,
    comparar,
    formatar_json,
    formatar_texto,
)
from harness.evals.congelamento import ErroDeCongelamento, congelar
from harness.evals.conjunto import VERSAO_CONJUNTO, casos_de_calculo, conjunto_completo
from harness.evals.dados import ESCALA_MAXIMA, FatosVerdadeiros, gerar_dados
from harness.evals.executor import (
    VARIANTES,
    ConfigExecucao,
    executar_avaliacao,
    orcamento_usado,
)
from harness.evals.procedencia import (
    RAIZ_DO_REPOSITORIO,
    ErroDeLinhaDeBase,
    ObterProcedencia,
    caminho_da_linha_de_base,
    escrever_linha_de_base,
    obter_git,
    validar_nome_da_linha,
)
from harness.evals.relatorio import (
    Classificacao,
    ErroDeRelatorio,
    Relatorio,
    escrever_relatorio,
    relatorio_de_json,
)
from harness.llm.base import ModeloLLM
from harness.llm.config import (
    carregar_env,
    config_de_ambiente,
    provedor_de_ambiente,
    validar_provedor,
)
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
CODIGO_REGRESSAO: Final = 1
CONJUNTOS: Final = ("padrao", "calculo")


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


def _escala_argumento(texto: str) -> int:
    try:
        valor = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"esperado um inteiro, recebido {texto!r}") from None
    if not 1 <= valor <= ESCALA_MAXIMA:
        raise argparse.ArgumentTypeError(f"deve estar entre 1 e {ESCALA_MAXIMA}")
    return valor


def _versao_do_conjunto(escala: int, conjunto: str = "padrao") -> str:
    """`VERSAO_CONJUNTO` no conjunto padrão e escala 1.

    Fora disso o conjunto não é o congelado, e o rótulo diz isso para que `comparar` não
    confunda o relatório com a linha de base.
    """
    if conjunto == "calculo":
        return "calculo-nao-congelado" if escala == 1 else f"calculo-nao-congelado-escala-{escala}"
    return VERSAO_CONJUNTO if escala == 1 else f"nao-congelado-escala-{escala}"


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
    parser.add_argument(
        "--salvar-linha-de-base",
        metavar="NOME",
        default=None,
        help="grava o relatório em evals/linhas-de-base/NOME.json (conjunto completo, 3+ rodadas, "
        "árvore do git limpa)",
    )
    parser.add_argument(
        "--permitir-arvore-suja",
        action="store_true",
        help="com --salvar-linha-de-base, grava mesmo com alterações no git (registra commit_sujo)",
    )
    parser.add_argument(
        "--provedor",
        metavar="NOME",
        default=None,
        help="fixa o provedor do OpenRouter nas avaliações (sobrescreve OPENROUTER_PROVEDOR); "
        "obrigatório com --salvar-linha-de-base se a variável não estiver definida",
    )
    parser.add_argument(
        "--pausa-s",
        type=_nao_negativo,
        default=0.0,
        metavar="SEGUNDOS",
        help="espera entre uma rodada e a próxima, em segundos (padrão: 0)",
    )
    parser.add_argument(
        "--escala",
        type=_escala_argumento,
        default=1,
        metavar="N",
        help=f"multiplica os pedidos: a planilha tem 60 * N linhas, de 1 a {ESCALA_MAXIMA} "
        "(padrão: 1, o conjunto congelado; com N diferente de 1, não há linha de base)",
    )
    parser.add_argument(
        "--variante",
        choices=VARIANTES,
        default="repl",
        help="agente da avaliação: repl (Code Mode, padrão), json (tool calling em JSON) ou "
        "json-agregacao (JSON com agregação de planilhas)",
    )
    parser.add_argument(
        "--conjunto",
        choices=CONJUNTOS,
        default="padrao",
        help="conjunto de casos: padrao (conjunto congelado v1, padrão) ou calculo (4 casos de "
        "cálculo, não congelado; sem linha de base)",
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


def _provedor_argumento(texto: str | None) -> str | None:
    """`--provedor` validado, ou None se não foi dado. Nome inválido vira ErroDeUso."""
    if texto is None:
        return None
    try:
        return validar_provedor(texto.strip(), nome="--provedor")
    except ValueError as erro:
        raise ErroDeUso(str(erro)) from None


def _provedor_de(ambiente: Mapping[str, str]) -> str | None:
    """`OPENROUTER_PROVEDOR` validado, ou None. Valor inválido vira ErroDeUso sem o valor."""
    try:
        return provedor_de_ambiente(ambiente)
    except ValueError as erro:
        raise ErroDeUso(str(erro)) from None


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
            pausa_s=args.pausa_s,
            variante=args.variante,
            escala=args.escala,
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


def _ambiente_da_execucao(provedor: str | None) -> dict[str, str]:
    """Ambiente do processo, com `--provedor` (se dado) sobrescrevendo `OPENROUTER_PROVEDOR`."""
    ambiente = _ambiente_do_processo()
    if provedor is not None:
        ambiente = {**ambiente, "OPENROUTER_PROVEDOR": provedor}
    return ambiente


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
    categorias: frozenset[Categoria], grupo: str, conjunto: str = "padrao"
) -> Callable[[FatosVerdadeiros], list[Caso]]:
    """Função de casos para o executor: conjunto padrão ou de cálculo, por categoria e grupo.

    Pendentes entram na lista, mas o executor não os roda; ficam só em `pendentes` do relatório.
    """

    def selecionar(fatos: FatosVerdadeiros) -> list[Caso]:
        casos = casos_de_calculo(fatos) if conjunto == "calculo" else conjunto_completo(fatos)
        if categorias:
            casos = [caso for caso in casos if caso.categoria in categorias]
        if grupo != "todos":
            casos = [caso for caso in casos if caso.grupo == grupo]
        return casos

    return selecionar


def _selecao_sem_modelo(
    categorias: frozenset[Categoria],
    grupo: str,
    semente: int,
    escala: int = 1,
    conjunto: str = "padrao",
) -> list[Caso]:
    """A seleção inteira, para decidir antes de qualquer configuração se há caso ativo.

    Gera os dados numa pasta temporária que some no fim. Não toca no modelo nem no `.env`.
    """
    with tempfile.TemporaryDirectory(prefix="selecao-") as temporaria:
        fatos = gerar_dados(Path(temporaria) / "dados", semente, escala=escala)
        return _selecionar(categorias, grupo, conjunto)(fatos)


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


def _inteiro_br(valor: float) -> str:
    """Inteiro com separador de milhar com ponto, como no resto do resumo (ex.: 1.234)."""
    return f"{valor:,.0f}".replace(",", ".")


def _resumo(saida: TextIO, relatorio: Relatorio, caminho: Path | None) -> None:
    linhas = [
        "",
        f"Variante: {relatorio.variante}",
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
    if relatorio.pico_contexto_medio is not None:
        pico = _inteiro_br(relatorio.pico_contexto_medio)
        linhas.append(f"Pico de contexto médio: {pico} tokens por chamada")
    if relatorio.caracteres_observacao_medio is not None:
        linhas.append(
            "Caracteres de observação médios por rodada: "
            f"{_inteiro_br(relatorio.caracteres_observacao_medio)}"
        )
    if relatorio.custo_por_acerto is not None:
        linhas.append(f"Custo por acerto: US$ {_br(relatorio.custo_por_acerto)}")
    if relatorio.conjunto != "padrao":
        linhas.append(f"Conjunto: {relatorio.conjunto} (não congelado)")
    if relatorio.escala != 1:
        linhas.append(
            f"Escala dos dados: {relatorio.escala} (planilha de pedidos com "
            f"{relatorio.linhas_pedidos} linhas; não é o conjunto congelado)"
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
    linhas.append(f"Provedor solicitado: {relatorio.provedor_solicitado or 'não fixado'}")
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


def _nao_negativo(texto: str) -> float:
    try:
        valor = float(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"esperado um número, recebido {texto!r}") from None
    if not math.isfinite(valor) or valor < 0:
        raise argparse.ArgumentTypeError("deve ser um número finito e não negativo")
    return valor


def _ler_relatorio(caminho: str, rotulo: str) -> Relatorio:
    """Relatório de um arquivo JSON. Arquivo ausente, ilegível ou inválido vira ErroDeUso."""
    try:
        texto = Path(caminho).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ErroDeUso(f"{rotulo}: arquivo não encontrado: {caminho}") from None
    except (OSError, UnicodeDecodeError) as erro:
        motivo = getattr(erro, "strerror", None) or type(erro).__name__
        raise ErroDeUso(f"{rotulo}: não foi possível ler {caminho}: {motivo}") from None
    try:
        return relatorio_de_json(texto)
    except ValidationError:
        raise ErroDeUso(f"{rotulo}: {caminho} não é um relatório de avaliação válido") from None


def _parser_comparar() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m harness.evals comparar",
        description="Compara uma execução com a linha de base e diz se houve regressão.",
    )
    parser.add_argument("base", help="relatório JSON da linha de base")
    parser.add_argument("novo", help="relatório JSON da execução nova")
    parser.add_argument(
        "--piso-pp",
        type=_nao_negativo,
        default=PISO_PP_PADRAO,
        help=f"margem mínima em pontos percentuais (padrão: {PISO_PP_PADRAO:g})",
    )
    parser.add_argument(
        "--fator-ruido",
        type=_nao_negativo,
        default=FATOR_RUIDO_PADRAO,
        help=f"vezes o desvio entre rodadas da base (padrão: {FATOR_RUIDO_PADRAO:g})",
    )
    parser.add_argument(
        "--formato", choices=("texto", "json"), default="texto", help="formato da saída"
    )
    return parser


def _comparar_cli(argumentos: Sequence[str], saida: TextIO) -> int:
    """`comparar BASE NOVO`: 0 ok, 1 regressão provável, 2 incomparável ou erro de leitura."""
    try:
        args = _parser_comparar().parse_args(argumentos)
    except SystemExit as erro:
        return erro.code if isinstance(erro.code, int) else CODIGO_USO
    try:
        base = _ler_relatorio(args.base, "linha de base")
        novo = _ler_relatorio(args.novo, "execução nova")
    except ErroDeUso as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO
    comparacao = comparar(base, novo, piso_pp=args.piso_pp, fator_ruido=args.fator_ruido)
    saida.write(formatar_json(comparacao) if args.formato == "json" else formatar_texto(comparacao))
    if comparacao.veredito == "incomparavel":
        return CODIGO_USO
    if comparacao.veredito == "regressao_provavel":
        return CODIGO_REGRESSAO
    return CODIGO_OK


def _congelar_cli(argumentos: Sequence[str], saida: TextIO, raiz: Path) -> int:
    """`congelar --versao vN [--semente 42]`: cria o arquivo do conjunto. Recusa sobrescrever."""
    parser = argparse.ArgumentParser(
        prog="python -m harness.evals congelar",
        description="Congela o conjunto de casos em evals/conjuntos/. Não sobrescreve.",
    )
    parser.add_argument("--versao", required=True, help="versão do conjunto, no formato vN")
    parser.add_argument("--semente", type=int, default=42, help="semente da geração (padrão: 42)")
    try:
        args = parser.parse_args(argumentos)
    except SystemExit as erro:
        return erro.code if isinstance(erro.code, int) else CODIGO_USO
    try:
        caminho = congelar(args.versao, args.semente, raiz)
    except ErroDeCongelamento as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO
    relativo = caminho.relative_to(raiz) if caminho.is_relative_to(raiz) else caminho
    saida.write(f"Conjunto congelado: {relativo}\n")
    return CODIGO_OK


def _graficos_cli(argumentos: Sequence[str], saida: TextIO, raiz: Path) -> int:
    """`graficos DADOS.json` ou `graficos --de-relatorios A.json B.json ...`, com `--saida PASTA`.

    Gera os SVG e a página HTML. Não chama o modelo. Os dois modos são excludentes: sem um ou com
    os dois, sai com 2. O de relatórios grava por padrão em docs/relatorios/comparacao-variantes,
    para não sobrescrever os gráficos da evolução.
    """
    from harness.evals.graficos import (
        PASTA_COMPARACAO,
        PASTA_GRAFICOS,
        ErroDeGraficos,
        carregar_dados,
        dados_de_relatorios,
        gerar_todos,
    )

    parser = argparse.ArgumentParser(
        prog="python -m harness.evals graficos",
        description="Gera os gráficos SVG e a página HTML a partir do arquivo de métricas, ou de "
        "relatórios de execução (--de-relatorios).",
    )
    parser.add_argument(
        "dados", nargs="?", type=Path, default=None, help="arquivo JSON de métricas das execuções"
    )
    parser.add_argument(
        "--de-relatorios",
        nargs="+",
        type=Path,
        default=None,
        metavar="RELATORIO",
        help="relatórios de execução, um por execução, nesta ordem (em vez de DADOS.json)",
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=None,
        help=f"pasta dos gráficos (padrão: {PASTA_GRAFICOS.as_posix()}, ou "
        f"{PASTA_COMPARACAO.as_posix()} com --de-relatorios)",
    )
    try:
        args = parser.parse_args(argumentos)
    except SystemExit as erro:
        return erro.code if isinstance(erro.code, int) else CODIGO_USO
    if args.dados is not None and args.de_relatorios is not None:
        saida.write("use DADOS.json ou --de-relatorios, não os dois\n")
        return CODIGO_USO
    if args.dados is None and args.de_relatorios is None:
        saida.write("informe DADOS.json ou --de-relatorios RELATORIO...\n")
        return CODIGO_USO
    try:
        if args.de_relatorios is not None:
            dados = dados_de_relatorios(args.de_relatorios)
            pasta = args.saida if args.saida is not None else raiz / PASTA_COMPARACAO
        else:
            dados = carregar_dados(args.dados)
            pasta = args.saida if args.saida is not None else raiz / PASTA_GRAFICOS
        caminhos = gerar_todos(dados, pasta)
    except ErroDeGraficos as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO
    for caminho in caminhos:
        relativo = caminho.relative_to(raiz) if caminho.is_relative_to(raiz) else caminho
        saida.write(f"Gráfico gerado: {relativo}\n")
    return CODIGO_OK


def _validar_linha_de_base(
    args: argparse.Namespace,
    categorias: frozenset[Categoria],
    raiz: Path,
    obter: ObterProcedencia,
    provedor: str | None,
) -> tuple[str | None, bool | None]:
    """Tudo que `--salvar-linha-de-base` exige, antes de qualquer execução. Devolve (commit, sujo).

    `provedor` é o de `--provedor`, se foi dado. Sem ele, a linha exige `OPENROUTER_PROVEDOR` no
    `.env` ou no ambiente; essa checagem fica por último, depois das outras. Não chama o modelo
    e não gera dados.
    """
    nome: str = args.salvar_linha_de_base
    try:
        validar_nome_da_linha(nome)
    except ErroDeLinhaDeBase as erro:
        raise ErroDeUso(str(erro)) from None
    if args.conjunto != "padrao":
        raise ErroDeUso(
            f"linha de base exige o conjunto padrão (congelado), e o conjunto pedido é "
            f"{args.conjunto!r}. Use --conjunto padrao ou não use --salvar-linha-de-base."
        )
    if args.escala != 1:
        raise ErroDeUso(
            f"linha de base exige a escala 1 (o conjunto congelado), e a escala pedida é "
            f"{args.escala}. Use --escala 1 ou não use --salvar-linha-de-base."
        )
    if categorias or args.grupo != "todos":
        raise ErroDeUso("linha de base exige o conjunto completo: não use --categoria nem --grupo")
    if args.rodadas < 3:
        raise ErroDeUso(
            "linha de base exige pelo menos 3 rodadas por caso (use --rodadas 3 ou mais)"
        )
    if caminho_da_linha_de_base(nome, raiz).exists():
        raise ErroDeUso(f"a linha de base {nome!r} já existe: não sobrescrevo")
    commit, sujo = obter(raiz)
    if commit is None or sujo is None:
        if not args.permitir_arvore_suja:
            raise ErroDeUso(
                "não foi possível confirmar a árvore do git (sem git ou fora de repositório). "
                "Use --permitir-arvore-suja para gravar mesmo assim."
            )
        return (None, None)
    if sujo and not args.permitir_arvore_suja:
        raise ErroDeUso(
            "a árvore do git tem alterações: faça commit antes de gravar a linha de base "
            "(ou use --permitir-arvore-suja, e o relatório registra commit_sujo)"
        )
    # Por último: só aqui se lê o `.env` (a variável pode estar lá).
    if provedor is None:
        provedor = _provedor_de(_ambiente_do_processo())
    if provedor is None:
        raise ErroDeUso(
            "linha de base exige provedor fixado: a linha de base precisa apontar sempre para o "
            "mesmo provedor. Use --provedor NOME ou defina OPENROUTER_PROVEDOR."
        )
    return (commit, sujo)


def main(
    argv: Sequence[str] | None = None,
    *,
    saida: TextIO = sys.stdout,
    executar: Callable[..., Relatorio] | None = None,
    fabrica_modelo: Callable[[], ModeloLLM] | None = None,
    raiz: Path | None = None,
    obter_procedencia: ObterProcedencia | None = None,
) -> int:
    """Roda a avaliação, ou um subcomando (`comparar`, `congelar`), e devolve o código de saída.

    `executar`, `fabrica_modelo`, `raiz` (raiz do repositório) e `obter_procedencia` (o `obter_git`)
    existem para os testes: sem eles, usam o executor real, o cliente do OpenRouter, o repositório
    onde o pacote está e o git de verdade.
    """
    itens = list(sys.argv[1:]) if argv is None else list(argv)
    raiz_repo = raiz if raiz is not None else RAIZ_DO_REPOSITORIO
    obter = obter_procedencia if obter_procedencia is not None else obter_git
    if itens and itens[0] == "comparar":
        return _comparar_cli(itens[1:], saida)
    if itens and itens[0] == "congelar":
        return _congelar_cli(itens[1:], saida, raiz_repo)
    if itens and itens[0] == "graficos":
        return _graficos_cli(itens[1:], saida, raiz_repo)

    try:
        args = _parser().parse_args(itens)
    except SystemExit as erro:
        return erro.code if isinstance(erro.code, int) else CODIGO_USO

    procedencia: tuple[str | None, bool | None] | None = None
    try:
        categorias = _categorias(args.categoria)
        config = _config(args)
        provedor_arg = _provedor_argumento(args.provedor)
        # Linha de base: tudo é conferido antes de gerar dados ou chamar o modelo.
        if args.salvar_linha_de_base is not None:
            procedencia = _validar_linha_de_base(args, categorias, raiz_repo, obter, provedor_arg)
    except ErroDeUso as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO

    # Sem caso ativo na seleção, nada roda: não lê o `.env`, não confere a chave e não chama o
    # modelo. Só lista os pendentes, cada um com o que o destrava, e sai com zero.
    selecao = _selecao_sem_modelo(categorias, args.grupo, args.semente, args.escala, args.conjunto)
    if not any(caso.situacao.ativo for caso in selecao):
        _sem_casos_ativos(saida, selecao, categorias, args.grupo)
        return CODIGO_OK

    try:
        ambiente = _ambiente_da_execucao(provedor_arg)
        provedor_solicitado = _provedor_de(ambiente)
        if fabrica_modelo is None:
            fabrica_modelo = _fabrica_de_modelo(ambiente)
        configuracao_telemetria = _config_telemetria(ambiente)
    except ErroDeUso as erro:
        saida.write(f"{erro}\n")
        return CODIGO_USO
    chave = ambiente.get("OPENROUTER_API_KEY", "").strip()

    fabrica_telemetria: Callable[[], TelemetriaOtel | None] | None = None
    if configuracao_telemetria is not None:
        saida.write(f"Telemetria: {descricao_telemetria(configuracao_telemetria)}\n")
        fabrica_telemetria = _fabrica_de_telemetria(ambiente)

    def progresso(caso_id: str, numero: int, classificacao: Classificacao) -> None:
        saida.write(f"{caso_id} rodada {numero}/{config.rodadas}: {classificacao.value}\n")

    # Procedência tirada antes da execução: é o estado do código que foi rodado.
    if procedencia is None:
        procedencia = obter(raiz_repo)
    commit, sujo = procedencia
    iniciado = datetime.now(UTC).isoformat(timespec="seconds")

    executor = executar if executar is not None else executar_avaliacao
    extra: dict[str, Any] = {}
    if fabrica_telemetria is not None:
        extra["fabrica_telemetria"] = fabrica_telemetria
    try:
        relatorio = executor(
            _selecionar(categorias, args.grupo, args.conjunto),
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
    relatorio = relatorio.model_copy(
        update={
            "conjunto": args.conjunto,
            "versao_conjunto": _versao_do_conjunto(config.escala, args.conjunto),
            "escala": config.escala,
            "commit": commit,
            "commit_sujo": sujo,
            "iniciado_em": iniciado,
            "orcamento": relatorio.orcamento
            if relatorio.orcamento is not None
            else orcamento_usado(config),
            "provedor_solicitado": provedor_solicitado,
        }
    )

    caminho: Path | None = None
    try:
        caminho = escrever_relatorio(relatorio, args.saida)
    except ErroDeRelatorio as erro:
        saida.write(f"{erro}\n")
    _resumo(saida, relatorio, caminho)

    falha_linha = False
    if args.salvar_linha_de_base is not None:
        falha_linha = not _gravar_linha_de_base(
            saida, relatorio, args.salvar_linha_de_base, raiz_repo, [chave] if chave else []
        )

    if relatorio.teto_estourado:
        return CODIGO_TETO
    if relatorio.erros_infra or caminho is None or falha_linha:
        return CODIGO_ERRO_INFRA
    return CODIGO_OK


def _gravar_linha_de_base(
    saida: TextIO,
    relatorio: Relatorio,
    nome: str,
    raiz: Path,
    segredos: list[str],
) -> bool:
    """Grava a linha de base, se a execução serve para isso. Devolve False se não gravou."""
    if relatorio.teto_estourado or relatorio.erros_infra or relatorio.nao_executadas:
        saida.write(
            "linha de base não gravada: a execução teve teto estourado, erro de infraestrutura "
            "ou rodada não executada. Rode de novo.\n"
        )
        return False
    try:
        gravado = escrever_linha_de_base(relatorio, nome, raiz, segredos=segredos)
    except (ErroDeLinhaDeBase, ErroDeRelatorio) as erro:
        saida.write(f"{erro}\n")
        return False
    relativo = gravado.relative_to(raiz)
    saida.write(f"Linha de base gravada: {relativo}\n")
    return True


if __name__ == "__main__":
    sys.exit(main())
