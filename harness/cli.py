"""Linha de comando do harness: `uv run python -m harness "pedido" --perfil Vendas --dados DIR`.

Monta o que o pedido precisa (modelo, registro de ferramentas, sandbox Docker e trace), roda o
Orquestrador e imprime o resultado. Não tem regra de negócio: quem decide é o Orquestrador.

Decisões deste módulo (não estão na spec):
- A saída do pedido e as mensagens de configuração saem por `saida`, para os testes verem o que
  o usuário veria. Os erros de uso que o argparse trata (argumento inválido) saem pelo próprio
  argparse, em stderr.
- Códigos de saída: 0 pedido completo (FINAL); 1 pedido não completo (orçamento, erro do modelo
  ou do sandbox); 2 uso ou configuração (perfil ou pasta de dados inexistente, configuração do
  modelo ausente, sandbox que não inicia).
- Configuração do modelo: `.env` no diretório atual, com `os.environ` por cima, como em
  `carregar_env`. A chave só entra no ClienteOpenRouter: nunca é impressa nem citada, nem na
  mensagem de erro quando falta.
- O custo total soma o campo `custo` dos eventos `turno_modelo`. Sem nenhum custo informado, a
  linha diz "indisponível" em vez de zero.
- `--contexto ARQUIVO`: arquivo `.json` (sem distinguir maiúsculas) é lido como JSON, e o valor
  precisa ser objeto ou lista; qualquer outra extensão é lida como texto UTF-8. O conteúdo vai
  para a variável `context` do REPL, nunca para o prompt. Arquivo inexistente, ilegível, inválido
  ou acima do teto de `LIMITE_CONTEXTO_BYTES` sai com código 2, antes de iniciar o sandbox. Para
  o teto, o arquivo inteiro conta: um JSON com muito espaço em branco pode ser recusado mesmo
  que o valor canônico coubesse.
- `--mcp ARQUIVO`: configuração dos servidores MCP (stdio). Arquivo ausente, inválido ou com
  nome de grupo repetido sai com código 2, antes de iniciar o sandbox. Os servidores sobem
  depois do sandbox e são SEMPRE fechados ao fim do pedido (try/finally), inclusive em erro. Um
  servidor que não inicia vira um aviso e não impede o pedido.
"""

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, Protocol, TextIO

from harness.host.mcp.adaptador import GerenteMcp, registrar_servidores_mcp, verificar_nomes_mcp
from harness.host.mcp.config import ConfigMcp, ErroConfigMcp, carregar_config_mcp
from harness.host.perfil import PERFIS, montagens_do_perfil, perfil_por_nome
from harness.host.registry import Registro
from harness.host.tools import registrar_todas
from harness.llm.base import ModeloLLM
from harness.llm.config import carregar_env, config_de_ambiente
from harness.llm.openrouter import ClienteOpenRouter
from harness.orchestrator.loop import (
    Evento,
    Orcamento,
    Orquestrador,
    ResultadoPedido,
    SandboxLike,
)
from harness.sandbox.contexto import ValorContexto, bytes_do_contexto
from harness.sandbox.docker_sandbox import IMAGEM_PADRAO, LIMITE_CONTEXTO_BYTES, DockerSandbox
from harness.telemetry.trace import TraceJsonl

ARQUIVO_ENV: Final = Path(".env")
PASTA_TRACE_PADRAO: Final = "traces"
CODIGO_COMPLETO: Final = 0
CODIGO_NAO_COMPLETO: Final = 1
CODIGO_USO: Final = 2


class SandboxIniciavel(SandboxLike, Protocol):
    """Sandbox que a CLI precisa iniciar antes do pedido. DockerSandbox satisfaz este contrato."""

    def start(self) -> None:
        """Cria e inicia o contêiner."""
        ...


class ErroDeConfiguracao(Exception):
    """Configuração do modelo ausente ou inválida. A mensagem nunca traz valores de chave."""


class ErroDeContexto(Exception):
    """Arquivo de contexto inexistente, ilegível, inválido ou grande demais. Sai com código 2."""


def _carregar_contexto(caminho: Path) -> ValorContexto:
    """Lê o arquivo do `--contexto` e devolve o valor (texto, objeto ou lista).

    A mensagem de erro cita só o caminho e o motivo, nunca o conteúdo do arquivo.
    """
    try:
        with caminho.open("rb") as arquivo:
            # Lê um byte além do teto: se vier mais, o arquivo é grande demais sem ser lido inteiro.
            bruto = arquivo.read(LIMITE_CONTEXTO_BYTES + 1)
    except OSError:
        raise ErroDeContexto(f"arquivo de contexto não pode ser lido: {caminho}") from None
    if len(bruto) > LIMITE_CONTEXTO_BYTES:
        raise ErroDeContexto(
            f"arquivo de contexto maior que o limite de {LIMITE_CONTEXTO_BYTES} bytes: {caminho}"
        )
    try:
        texto = bruto.decode("utf-8")
    except UnicodeDecodeError:
        raise ErroDeContexto(f"arquivo de contexto não está em UTF-8: {caminho}") from None
    valor: ValorContexto = texto
    if caminho.suffix.lower() == ".json":
        try:
            carregado = json.loads(texto)
        except json.JSONDecodeError as erro:
            raise ErroDeContexto(
                f"JSON inválido no arquivo de contexto ({erro.msg}, linha {erro.lineno}): {caminho}"
            ) from None
        if not isinstance(carregado, dict | list):
            raise ErroDeContexto(
                f"o JSON do contexto precisa ser um objeto ou uma lista: {caminho}"
            )
        valor = carregado
    if bytes_do_contexto(valor) > LIMITE_CONTEXTO_BYTES:
        raise ErroDeContexto(
            f"o contexto passa do limite de {LIMITE_CONTEXTO_BYTES} bytes como JSON: {caminho}"
        )
    return valor


class _ColetorDeEventos:
    """Grava cada evento na trace e soma o custo informado pelos turnos do modelo."""

    def __init__(self, trace: TraceJsonl) -> None:
        self._trace = trace
        self.custo_total = 0.0
        self.custo_informado = False

    def __call__(self, evento: Evento) -> None:
        self._trace(evento)
        if evento.tipo != "turno_modelo":
            return
        custo = evento.dados.get("custo")
        if isinstance(custo, int | float) and not isinstance(custo, bool):
            self.custo_total += float(custo)
            self.custo_informado = True


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
        prog="python -m harness",
        description=(
            "Responde a um pedido: o modelo escreve código, o sandbox executa e as ferramentas "
            "leem só os dados do perfil."
        ),
    )
    parser.add_argument("pedido", help="o pedido em linguagem natural")
    parser.add_argument("--perfil", required=True, help="perfil de acesso, por exemplo Vendas")
    parser.add_argument(
        "--dados", required=True, type=Path, help="pasta com um subdiretório por departamento"
    )
    parser.add_argument(
        "--trace",
        type=Path,
        default=Path(PASTA_TRACE_PADRAO),
        help=f"pasta onde gravar o trace JSONL (padrão: {PASTA_TRACE_PADRAO})",
    )
    parser.add_argument(
        "--imagem", default=IMAGEM_PADRAO, help=f"imagem do sandbox (padrão: {IMAGEM_PADRAO})"
    )
    parser.add_argument(
        "--max-iteracoes",
        type=_inteiro_positivo,
        default=None,
        help="limite de chamadas ao modelo (padrão: o do Orcamento)",
    )
    parser.add_argument(
        "--contexto",
        type=Path,
        default=None,
        help="arquivo de entrada do pedido: .json vira objeto ou lista; outro tipo, texto",
    )
    parser.add_argument(
        "--mcp",
        type=Path,
        default=None,
        help="arquivo JSON com os servidores MCP (stdio) cujas ferramentas ficam disponíveis",
    )
    return parser


def _modelo_de_ambiente() -> ModeloLLM:
    """Monta o cliente real a partir do `.env` e do ambiente. Falta de configuração levanta."""
    ambiente = {**carregar_env(ARQUIVO_ENV), **os.environ}
    try:
        configuracao = config_de_ambiente(ambiente)
    except ValueError as erro:
        raise ErroDeConfiguracao(
            f"configuração do modelo inválida: {erro}. Defina a variável no ambiente "
            f"ou no arquivo {ARQUIVO_ENV}."
        ) from None
    chave = ambiente.get("OPENROUTER_API_KEY", "").strip()
    if not chave:
        raise ErroDeConfiguracao(
            "OPENROUTER_API_KEY não definida: informe a chave no ambiente ou no arquivo "
            f"{ARQUIVO_ENV}."
        )
    return ClienteOpenRouter(configuracao, chave)


def _avisar_mcp(saida: TextIO, gerente: GerenteMcp) -> None:
    """Uma linha por servidor que não subiu e uma com as ferramentas ignoradas, se houver."""
    for nome, motivo in gerente.falhas.items():
        saida.write(f"aviso: o servidor MCP '{nome}' não foi iniciado: {motivo}\n")
    if gerente.ignoradas:
        saida.write(
            f"aviso: {len(gerente.ignoradas)} ferramenta(s) MCP ignorada(s) por nome fora do "
            "padrão ou repetido\n"
        )


def _imprimir(
    saida: TextIO, resultado: ResultadoPedido, coletor: _ColetorDeEventos, trace: Path
) -> None:
    fontes = ", ".join(resultado.fontes) if resultado.fontes else "nenhuma"
    completo = "sim" if resultado.completo else "não"
    custo = f"US$ {coletor.custo_total:.6f}" if coletor.custo_informado else "indisponível"
    linhas = [
        f"resposta: {resultado.resposta}",
        f"fontes: {fontes}",
        f"motivo: {resultado.motivo.value} (completo: {completo})",
        f"iterações: {resultado.iteracoes}",
        f"tokens: entrada {resultado.tokens_entrada}, saída {resultado.tokens_saida}",
        f"custo total: {custo}",
        f"trace: {trace}",
    ]
    saida.write("\n".join(linhas) + "\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    modelo: ModeloLLM | None = None,
    saida: TextIO = sys.stdout,
    fabrica_sandbox: Callable[..., SandboxIniciavel] | None = None,
) -> int:
    """Roda um pedido pela linha de comando e devolve o código de saída.

    `modelo` e `fabrica_sandbox` existem para os testes: sem eles, a CLI usa o OpenRouter (com
    a configuração do ambiente) e o DockerSandbox.
    """
    try:
        args = _parser().parse_args(argv)
    except SystemExit as erro:
        return erro.code if isinstance(erro.code, int) else CODIGO_USO

    try:
        perfil = perfil_por_nome(args.perfil)
    except ValueError:
        opcoes = ", ".join(p.nome for p in PERFIS)
        saida.write(f"perfil desconhecido: {args.perfil!r}. Perfis: {opcoes}.\n")
        return CODIGO_USO
    if not args.dados.is_dir():
        saida.write(f"pasta de dados não encontrada: {args.dados}\n")
        return CODIGO_USO
    contexto: ValorContexto | None = None
    if args.contexto is not None:
        try:
            contexto = _carregar_contexto(args.contexto)
        except ErroDeContexto as erro:
            saida.write(f"{erro}\n")
            return CODIGO_USO

    mcp_config: ConfigMcp | None = None
    if args.mcp is not None:
        try:
            mcp_config = carregar_config_mcp(args.mcp)
        except ErroConfigMcp as erro:
            saida.write(f"{erro}\n")
            return CODIGO_USO

    if modelo is None:
        try:
            modelo = _modelo_de_ambiente()
        except ErroDeConfiguracao as erro:
            saida.write(f"{erro}\n")
            return CODIGO_USO

    registro = Registro()
    registrar_todas(registro, args.dados)
    if mcp_config is not None:
        try:
            verificar_nomes_mcp(registro, mcp_config)
        except ErroConfigMcp as erro:
            saida.write(f"{erro}\n")
            return CODIGO_USO
    fabrica: Callable[..., SandboxIniciavel] = (
        fabrica_sandbox if fabrica_sandbox is not None else DockerSandbox
    )
    sandbox = fabrica(args.imagem, dados=montagens_do_perfil(perfil, args.dados))
    try:
        sandbox.start()
    except Exception:
        # O erro do Docker pode trazer detalhes do host: a CLI diz só o que o usuário faz.
        with contextlib.suppress(Exception):
            sandbox.close()
        saida.write(
            f"não foi possível iniciar o sandbox (imagem {args.imagem!r}). Confira se o Docker "
            "está ativo e se a imagem foi construída.\n"
        )
        return CODIGO_USO

    trace = TraceJsonl(args.trace)
    coletor = _ColetorDeEventos(trace)
    limites: dict[str, int] = {}
    if args.max_iteracoes is not None:
        limites["max_iteracoes"] = args.max_iteracoes
    orcamento = Orcamento(**limites)
    # Os servidores MCP sobem só agora, e são fechados no finally, inclusive se algo falhar.
    gerente: GerenteMcp | None = None
    try:
        if mcp_config is not None:
            gerente = registrar_servidores_mcp(registro, mcp_config)
            _avisar_mcp(saida, gerente)
    except BaseException:
        # Sem Orquestrador, ninguém mais fecharia o sandbox nem os servidores já iniciados.
        if gerente is not None:
            gerente.fechar()
        with contextlib.suppress(Exception):
            sandbox.close()
        raise
    try:
        # O Orquestrador fecha o sandbox ao fim do pedido, inclusive em erro.
        resultado = Orquestrador(
            modelo, sandbox, registro, perfil, orcamento=orcamento, eventos=coletor
        ).executar_pedido(args.pedido, contexto=contexto)
        _imprimir(saida, resultado, coletor, trace.caminho)
    finally:
        if gerente is not None:
            gerente.fechar()
    return CODIGO_COMPLETO if resultado.completo else CODIGO_NAO_COMPLETO
