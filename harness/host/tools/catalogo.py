"""Ferramenta `catalogo.listar`: lista os arquivos visíveis ao perfil (spec v0; D10).

A ferramenta roda no host e lê a raiz de dados que recebe na fábrica `registrar`. Ela
nunca segue links simbólicos, para não listar nem calcular hash de arquivos fora de /dados.
A flag de confiança vem de um `manifesto.json` opcional na raiz: sem manifesto, todo
arquivo é `confiavel`; qualquer valor que não seja `confiavel` no manifesto vira `externo`
(falha fechada).
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from harness.host.perfil import Perfil
from harness.host.registry import ErroFerramenta, Nivel, Registro

NOME_MANIFESTO: Final = "manifesto.json"
CONFIAVEL: Final = "confiavel"
EXTERNO: Final = "externo"
# Tamanho do bloco de leitura para calcular o hash sem carregar o arquivo inteiro.
_TAMANHO_BLOCO: Final = 1024 * 1024


class ListarArgs(BaseModel):
    """Argumentos de `catalogo.listar`. O padrão impede caminhos, maiúsculas e espaços."""

    model_config = ConfigDict(extra="forbid")

    departamento: str | None = Field(default=None, pattern=r"^[a-z_]+$")


def _ler_manifesto(raiz: Path) -> dict[str, Any]:
    """Lê o manifesto da raiz. Ausente vira dicionário vazio; malformado falha fechado."""
    caminho = raiz / NOME_MANIFESTO
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ErroFerramenta("o manifesto.json da raiz de dados é inválido") from erro
    if not isinstance(dados, dict):
        raise ErroFerramenta("o manifesto.json da raiz de dados é inválido")
    return dados


def _confianca(manifesto: dict[str, Any], arquivo: str) -> str:
    """`confiavel` só se o manifesto disser isso; qualquer outra coisa vira `externo` (D10)."""
    if arquivo not in manifesto:
        return CONFIAVEL
    return CONFIAVEL if manifesto[arquivo] == CONFIAVEL else EXTERNO


def _sha256(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(_TAMANHO_BLOCO), b""):
            digest.update(bloco)
    return digest.hexdigest()


def _arquivos_regulares(pasta: Path) -> list[Path]:
    """Arquivos comuns sob `pasta`, em qualquer subpasta, sem seguir links simbólicos."""
    encontrados: list[Path] = []
    for atual, subpastas, nomes in os.walk(pasta, followlinks=False):
        subpastas.sort()
        for nome in sorted(nomes):
            caminho = Path(atual) / nome
            if caminho.is_symlink() or not caminho.is_file():
                continue
            encontrados.append(caminho)
    return encontrados


def listar(args: ListarArgs, perfil: Perfil, raiz: Path) -> list[dict[str, Any]]:
    """Lista os arquivos do perfil, ou só de um departamento dele.

    Pedir um departamento fora do perfil levanta PermissionError antes de qualquer leitura.
    """
    if args.departamento is None:
        departamentos = sorted(perfil.departamentos)
    else:
        if args.departamento not in perfil.departamentos:
            raise PermissionError(
                f"o departamento '{args.departamento}' não faz parte do perfil '{perfil.nome}'"
            )
        departamentos = [args.departamento]

    manifesto = _ler_manifesto(raiz)
    itens: list[dict[str, Any]] = []
    for departamento in departamentos:
        pasta = raiz / departamento
        if pasta.is_symlink() or not pasta.is_dir():
            continue
        for caminho in _arquivos_regulares(pasta):
            relativo = caminho.relative_to(pasta).as_posix()
            arquivo = f"{departamento}/{relativo}"
            status = caminho.stat()
            itens.append(
                {
                    "arquivo": arquivo,
                    "bytes": status.st_size,
                    "sha256": _sha256(caminho),
                    "modificado": datetime.fromtimestamp(status.st_mtime, tz=UTC).isoformat(
                        timespec="seconds"
                    ),
                    "confianca": _confianca(manifesto, arquivo),
                }
            )
    return sorted(itens, key=lambda item: str(item["arquivo"]))


def registrar(registro: Registro, raiz: Path) -> None:
    """Registra `catalogo.listar` no registro, com a raiz de dados informada pelo chamador."""

    @registro.ferramenta(
        nome="catalogo.listar",
        nivel=Nivel.LEITURA,
        descricao=(
            "Lista os arquivos visíveis ao perfil, com tamanho, hash, data e flag de confiança."
        ),
        args=ListarArgs,
        dado_privado=True,
        conteudo_nao_confiavel=False,
        canal_externo=False,
    )
    def executar(args: ListarArgs, perfil: Perfil) -> list[dict[str, Any]]:
        return listar(args, perfil, raiz)
