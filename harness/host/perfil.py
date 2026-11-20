"""Perfis de acesso e montagem de dados por perfil (spec v0, "Perfis de acesso").

O controle de acesso acontece aqui, antes de qualquer código do modelo rodar: só os
departamentos do perfil entram nas montagens que o DockerSandbox recebe. Um departamento
fora do perfil simplesmente não existe dentro do contêiner.
"""

import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Perfil(BaseModel):
    """Perfil do solicitante: nome para exibição e departamentos que ele pode acessar."""

    model_config = ConfigDict(frozen=True)

    nome: str
    # Nomes de departamento em minúsculas e sem acento, como os diretórios em /dados.
    departamentos: frozenset[str]


PERFIS: tuple[Perfil, ...] = (
    Perfil(nome="Vendas", departamentos=frozenset({"vendas", "comercial"})),
    Perfil(nome="Marketing", departamentos=frozenset({"marketing"})),
    Perfil(nome="Financeiro", departamentos=frozenset({"financeiro", "vendas", "comercial"})),
    Perfil(
        nome="Direção",
        departamentos=frozenset({"vendas", "comercial", "marketing", "financeiro", "direcao"}),
    ),
)


def _normalizar(texto: str) -> str:
    """Minúsculas e sem acento, para comparar nomes de perfil de forma tolerante."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).casefold()


def perfil_por_nome(nome: str) -> Perfil:
    """Devolve o perfil cujo nome bate, sem diferenciar caixa nem acento.

    Levanta ValueError se o nome não corresponder a nenhum perfil.
    """
    alvo = _normalizar(nome)
    for perfil in PERFIS:
        if _normalizar(perfil.nome) == alvo:
            return perfil
    raise ValueError(f"perfil desconhecido: {nome!r}")


def montagens_do_perfil(perfil: Perfil, raiz: Path) -> dict[str, Path]:
    """Mapeia cada departamento do perfil para `raiz/<departamento>`.

    O resultado alimenta o parâmetro `dados` do DockerSandbox, que monta cada entrada em
    /dados/<departamento> somente leitura. Departamentos fora do perfil não aparecem.
    """
    return {departamento: raiz / departamento for departamento in sorted(perfil.departamentos)}
