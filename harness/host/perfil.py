"""Perfis de acesso e montagem de dados por perfil (spec v0, "Perfis de acesso").

O controle de acesso acontece aqui, antes de qualquer código do modelo rodar: só os
departamentos do perfil entram nas montagens que o DockerSandbox recebe. Um departamento
fora do perfil simplesmente não existe dentro do contêiner.

Concessões (D14, versão mínima): o perfil também lista as ferramentas que pode chamar. Cada item
é um nome exato (`catalogo.listar`) ou um grupo inteiro com curinga (`descoberta.*`). A regra
padrão é negar: sem concessão, a ferramenta não existe para o perfil. O registro consulta
`permite` antes de validar argumentos e de chamar a função.
"""

import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, field_validator

# Item de concessão: `grupo.nome` ou `grupo.*`, com grupo e nome em minúsculas, `_` ou letras.
_ITEM_DE_CONCESSAO: Final = re.compile(r"[a-z_]+\.(?:[a-z_]+|\*)")
# Nome de ferramenta bem formado, no formato que o Registro aceita. Só ele pode ser permitido.
_NOME_DE_FERRAMENTA: Final = re.compile(r"[a-z_]+\.[a-z_]+")


class Perfil(BaseModel):
    """Perfil do solicitante: nome, departamentos que ele pode acessar e ferramentas concedidas."""

    model_config = ConfigDict(frozen=True)

    nome: str
    # Nomes de departamento em minúsculas e sem acento, como os diretórios em /dados.
    departamentos: frozenset[str]
    # Ferramentas que o perfil pode chamar: `grupo.nome` ou `grupo.*`. Vazio nega tudo.
    concessoes: frozenset[str] = frozenset()

    @field_validator("concessoes")
    @classmethod
    def _concessoes_validas(cls, valor: frozenset[str]) -> frozenset[str]:
        for item in valor:
            if not _ITEM_DE_CONCESSAO.fullmatch(item):
                raise ValueError(f"concessão inválida: {item!r} (use 'grupo.nome' ou 'grupo.*')")
        return valor

    def permite(self, nome: str) -> bool:
        """Verdadeiro se o perfil pode chamar a ferramenta `nome`.

        Vale o item exato ou o curinga cujo grupo é o texto antes do primeiro ponto, e só para
        nomes no formato `grupo.nome`. Nome vazio, sem ponto, com caracteres fora do formato ou
        muito longo fica sem permissão. Nunca levanta exceção.
        """
        if _NOME_DE_FERRAMENTA.fullmatch(nome) is None:
            return False
        grupo = nome.split(".", 1)[0]
        return nome in self.concessoes or f"{grupo}.*" in self.concessoes

    def com_concessoes(self, extra: Iterable[str]) -> "Perfil":
        """Cópia do perfil com as concessões de `extra` somadas às atuais (validadas de novo)."""
        return Perfil(
            nome=self.nome,
            departamentos=self.departamentos,
            concessoes=self.concessoes | frozenset(extra),
        )


# Concessões que todo perfil embutido tem: o catálogo dos próprios arquivos e a descoberta.
CONCESSOES_BASE: Final = frozenset({"catalogo.listar", "descoberta.*"})

PERFIS: tuple[Perfil, ...] = (
    Perfil(
        nome="Vendas",
        departamentos=frozenset({"vendas", "comercial"}),
        concessoes=CONCESSOES_BASE,
    ),
    Perfil(nome="Marketing", departamentos=frozenset({"marketing"}), concessoes=CONCESSOES_BASE),
    Perfil(
        nome="Financeiro",
        departamentos=frozenset({"financeiro", "vendas", "comercial"}),
        concessoes=CONCESSOES_BASE,
    ),
    Perfil(
        nome="Direção",
        departamentos=frozenset({"vendas", "comercial", "marketing", "financeiro", "direcao"}),
        concessoes=CONCESSOES_BASE,
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
