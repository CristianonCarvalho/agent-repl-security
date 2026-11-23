"""Conjunto de casos congelado em arquivo, versionado no git (T17c).

O conjunto é gerado pelo código a partir da semente (`gerar_dados` + `conjunto_completo`) e
gravado em `evals/conjuntos/<versao>-semente<semente>.json`. Depois de gravado, o arquivo é a
referência: o teste de congelamento confere que o código ainda gera exatamente esse conteúdo.
Se a pergunta ou o gabarito mudarem, o teste falha, e a mudança exige uma versão nova.

Decisões deste módulo (não estão na spec):
- `versao` precisa casar `v<número>` (ex.: `v1`). Outros nomes são recusados antes de tocar no
  disco, para o nome do arquivo ficar previsível.
- `congelar` nunca sobrescreve: se o arquivo existe, levanta `ErroDeCongelamento`.
- O texto gravado é `casos_para_json(casos)` mais uma quebra de linha final, o mesmo formato
  que `casos_de_json` lê.
"""

import re
import tempfile
from pathlib import Path
from typing import Final

from harness.evals.casos import Caso, casos_de_json, casos_para_json
from harness.evals.conjunto import conjunto_completo
from harness.evals.dados import gerar_dados

PASTA_CONJUNTOS: Final = Path("evals") / "conjuntos"
_VERSAO_VALIDA: Final = re.compile(r"v\d+")


class ErroDeCongelamento(Exception):
    """O conjunto não pôde ser congelado ou lido. A mensagem diz o caminho e o motivo."""


def _validar_versao(versao: str) -> str:
    if _VERSAO_VALIDA.fullmatch(versao) is None:
        raise ErroDeCongelamento(f"versão inválida: {versao!r}. Use o formato vN, por exemplo v2.")
    return versao


def caminho_do_conjunto(versao: str, semente: int, base: Path) -> Path:
    """`evals/conjuntos/<versao>-semente<semente>.json` dentro de `base` (a raiz do repositório)."""
    return base / PASTA_CONJUNTOS / f"{_validar_versao(versao)}-semente{semente}.json"


def congelar(versao: str, semente: int, base: Path) -> Path:
    """Gera o conjunto completo para a semente e grava no caminho padrão. Devolve o caminho.

    Os dados são gerados numa pasta temporária, que some no fim. Não toca na rede nem no modelo.
    Recusa sobrescrever arquivo existente.
    """
    caminho = caminho_do_conjunto(versao, semente, base)
    if caminho.exists():
        raise ErroDeCongelamento(
            f"o conjunto já existe em {caminho}: não sobrescrevo. Para mudar pergunta ou gabarito, "
            "crie uma versão nova (congelar --versao v<N+1>) e atualize VERSAO_CONJUNTO."
        )
    with tempfile.TemporaryDirectory(prefix="congelar-") as temporaria:
        fatos = gerar_dados(Path(temporaria) / "dados", semente)
        texto = casos_para_json(conjunto_completo(fatos)) + "\n"
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        with caminho.open("x", encoding="utf-8") as arquivo:
            arquivo.write(texto)
    except FileExistsError:
        raise ErroDeCongelamento(f"o conjunto já existe em {caminho}: não sobrescrevo") from None
    except OSError as erro:
        motivo = erro.strerror or type(erro).__name__
        raise ErroDeCongelamento(f"não foi possível gravar {caminho}: {motivo}") from erro
    return caminho


def ler_congelado(versao: str, semente: int, base: Path) -> list[Caso]:
    """Lê e valida o conjunto congelado. Arquivo ausente ou inválido levanta ErroDeCongelamento."""
    caminho = caminho_do_conjunto(versao, semente, base)
    if not caminho.is_file():
        raise ErroDeCongelamento(f"conjunto congelado não encontrado: {caminho}")
    try:
        return casos_de_json(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ErroDeCongelamento(f"conjunto congelado inválido em {caminho}: {erro}") from erro
