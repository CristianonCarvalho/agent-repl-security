"""Procedência das execuções da avaliação e linhas de base (T17c).

`obter_git` diz de qual commit a execução saiu e se a árvore estava suja. Ele nunca levanta
exceção: sem git, fora de repositório ou com timeout, devolve `(None, None)`. Quem chama injeta a
função, então os testes não dependem do estado real do repositório.

`escrever_linha_de_base` grava um relatório em `evals/linhas-de-base/NOME.json` depois de
sanitizá-lo (sem caminhos de trace) e de conferir que nenhuma chave nem a pasta pessoal do usuário
estão no texto. Nunca sobrescreve.

Decisões deste módulo (não estão na spec):
- O nome da linha de base segue `[a-z0-9][a-z0-9._-]*`, sem barra, então não sai da pasta.
- A verificação de segredo compara os valores passados (a chave do OpenRouter), não um padrão.
  Se algum aparece no texto, nada é gravado e a mensagem não cita o valor.
- Ao gravar, o relatório é o mesmo de `escrever_relatorio`, com `linha_de_base` preenchido.
"""

import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from harness.evals.relatorio import ErroDeRelatorio, Relatorio, relatorio_para_json

RAIZ_DO_REPOSITORIO: Final = Path(__file__).resolve().parents[2]
PASTA_LINHAS_DE_BASE: Final = Path("evals") / "linhas-de-base"
_NOME_VALIDO: Final = re.compile(r"[a-z0-9][a-z0-9._-]*")
_COMMIT_VALIDO: Final = re.compile(r"[0-9a-f]{40,64}")
_TIMEOUT_GIT_S: Final = 5.0

ObterProcedencia = Callable[[Path], tuple[str | None, bool | None]]


class ErroDeLinhaDeBase(Exception):
    """A linha de base não pôde ser gravada. A mensagem nunca traz valores secretos."""


def _git(raiz: Path, *argumentos: str) -> str | None:
    """Saída do git, sem a quebra final, ou None se o comando falhou de qualquer jeito."""
    try:
        resultado = subprocess.run(
            ["git", *argumentos],
            cwd=raiz,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_GIT_S,
            check=False,
        )
    except Exception:
        return None
    if resultado.returncode != 0:
        return None
    return resultado.stdout.rstrip("\n")


def obter_git(raiz: Path) -> tuple[str | None, bool | None]:
    """Commit completo do HEAD e se a árvore tem alterações (`git status --porcelain`).

    Devolve `(None, None)` quando não dá para saber: sem git, fora de repositório, timeout ou
    saída inesperada. Nunca levanta exceção.
    """
    try:
        commit = _git(raiz, "rev-parse", "HEAD")
        if commit is None or _COMMIT_VALIDO.fullmatch(commit.strip()) is None:
            return (None, None)
        status = _git(raiz, "status", "--porcelain")
        if status is None:
            return (None, None)
        return (commit.strip(), bool(status.strip()))
    except Exception:
        return (None, None)


def validar_nome_da_linha(nome: str) -> str:
    """Devolve o nome se ele casa `[a-z0-9][a-z0-9._-]*`. Senão levanta ErroDeLinhaDeBase."""
    if _NOME_VALIDO.fullmatch(nome) is None:
        raise ErroDeLinhaDeBase(
            f"nome de linha de base inválido: {nome!r}. Use letras minúsculas, números, "
            "ponto, hífen ou sublinhado, começando por letra ou número."
        )
    return nome


def caminho_da_linha_de_base(nome: str, raiz: Path) -> Path:
    """`evals/linhas-de-base/NOME.json` dentro da raiz do repositório."""
    return raiz / PASTA_LINHAS_DE_BASE / f"{validar_nome_da_linha(nome)}.json"


def sanear_relatorio(relatorio: Relatorio, nome: str) -> Relatorio:
    """Cópia sem caminhos de trace, com o nome da linha de base. O original não muda."""
    casos = tuple(
        caso.model_copy(
            update={
                "rodadas": tuple(
                    rodada.model_copy(update={"caminho_trace": None}) for rodada in caso.rodadas
                )
            }
        )
        for caso in relatorio.casos
    )
    return relatorio.model_copy(update={"casos": casos, "linha_de_base": nome})


def escrever_linha_de_base(
    relatorio: Relatorio,
    nome: str,
    raiz: Path,
    *,
    segredos: Sequence[str] = (),
) -> Path:
    """Grava o relatório sanitizado em `evals/linhas-de-base/NOME.json` e devolve o caminho.

    Recusa a gravação se já existe arquivo com esse nome, se algum segredo aparece no texto ou se
    a pasta pessoal do usuário aparece nele. Falha de I/O vira ErroDeRelatorio.
    """
    caminho = caminho_da_linha_de_base(nome, raiz)
    texto = relatorio_para_json(sanear_relatorio(relatorio, nome)) + "\n"
    for segredo in segredos:
        if segredo and segredo in texto:
            raise ErroDeLinhaDeBase("a linha de base não foi gravada: o texto traz um segredo")
    pessoal = str(Path.home())
    if len(pessoal) > 5 and pessoal in texto:
        raise ErroDeLinhaDeBase("a linha de base não foi gravada: o texto traz um caminho pessoal")
    if caminho.exists():
        raise ErroDeLinhaDeBase(f"a linha de base {nome!r} já existe: não sobrescrevo")
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("x", encoding="utf-8") as arquivo:
            arquivo.write(texto)
    except FileExistsError:
        raise ErroDeLinhaDeBase(f"a linha de base {nome!r} já existe: não sobrescrevo") from None
    except OSError as erro:
        motivo = erro.strerror or type(erro).__name__
        raise ErroDeRelatorio(f"não foi possível gravar a linha de base: {motivo}") from erro
    return caminho
