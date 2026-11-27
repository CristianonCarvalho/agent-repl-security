"""Política da lethal trifecta e rastreio de arquivos abertos (spec v0, regra 3, D10; Fase 4).

A trifecta é a combinação de dado privado, conteúdo não confiável e canal externo na mesma
sessão. Aqui ficam três coisas sem dependência do sandbox nem do registro:

- `Exposicao`: o que a sessão já viu (dois marcadores; o terceiro fator vem da ferramenta).
- `avaliar`: decisão pura sobre uma chamada de ferramenta.
- `RastreadorDeArquivos`: transforma "o sandbox vai abrir tal caminho" em marcas na exposição,
  com a mesma regra de confiança do catálogo.

`PEDIR_APROVACAO` ainda não tem aprovação humana por trás: o registro o trata como recusa
enquanto não houver aprovador (peça 2 da Fase 4).
"""

import posixpath
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Protocol

from harness.host.confianca import (
    CONFIAVEL,
    EXTERNO,
    ErroManifesto,
    confianca_de,
    ler_manifesto,
)

_PREFIXO_DADOS: Final = "/dados/"


class _Propriedades(Protocol):
    """O que a política precisa saber de uma ferramenta (o `Ferramenta` do registro cumpre)."""

    @property
    def dado_privado(self) -> bool: ...

    @property
    def conteudo_nao_confiavel(self) -> bool: ...

    @property
    def canal_externo(self) -> bool: ...


class Decisao(Enum):
    """Resultado de `avaliar`."""

    PERMITIR = "permitir"
    PEDIR_APROVACAO = "pedir_aprovacao"
    RECUSAR = "recusar"


@dataclass
class Exposicao:
    """O que a sessão já viu. Mutável: o rastreador e o registro a atualizam."""

    viu_dado_privado: bool = False
    viu_nao_confiavel: bool = False

    def marcar_arquivo(self, confianca: str) -> None:
        """Abrir qualquer arquivo de /dados é ver dado privado; arquivo não `confiavel` é
        também conteúdo não confiável."""
        self.viu_dado_privado = True
        if confianca != CONFIAVEL:
            self.viu_nao_confiavel = True

    def marcar_ferramenta(self, ferramenta: _Propriedades) -> None:
        """Uma ferramenta que devolve dado privado ou conteúdo não confiável marca a sessão."""
        if ferramenta.dado_privado:
            self.viu_dado_privado = True
        if ferramenta.conteudo_nao_confiavel:
            self.viu_nao_confiavel = True

    def zerar(self) -> None:
        """Começa um pedido novo sem exposição."""
        self.viu_dado_privado = False
        self.viu_nao_confiavel = False


def avaliar(exposicao: Exposicao, ferramenta: _Propriedades) -> Decisao:
    """Decide sobre uma chamada. Soma a exposição às propriedades da própria ferramenta."""
    privado = exposicao.viu_dado_privado or ferramenta.dado_privado
    nao_confiavel = exposicao.viu_nao_confiavel or ferramenta.conteudo_nao_confiavel
    if ferramenta.canal_externo and privado and nao_confiavel:
        return Decisao.PEDIR_APROVACAO
    return Decisao.PERMITIR


class RastreadorDeArquivos:
    """Recebe o caminho que o sandbox vai abrir e marca a exposição.

    Chame com o caminho como o sandbox o vê (`/dados/<departamento>/...`). Devolve `None`
    quando a abertura pode seguir, ou uma mensagem curta quando deve ser bloqueada. A chave do
    manifesto é o caminho relativo a `/dados`, a mesma que o catálogo usa em `arquivo`.
    """

    def __init__(self, raiz: Path, exposicao: Exposicao) -> None:
        self._raiz = raiz
        self._exposicao = exposicao

    def __call__(self, caminho: str) -> str | None:
        if not caminho.startswith(_PREFIXO_DADOS) or posixpath.normpath(caminho) != caminho:
            return "caminho de arquivo fora do formato esperado"
        arquivo = caminho[len(_PREFIXO_DADOS) :]
        if not arquivo:
            return "caminho de arquivo fora do formato esperado"
        try:
            manifesto = ler_manifesto(self._raiz)
        except ErroManifesto:
            # Falha fechada: sem manifesto legível, o arquivo conta como não confiável.
            self._exposicao.marcar_arquivo(EXTERNO)
            return None
        self._exposicao.marcar_arquivo(confianca_de(manifesto, arquivo))
        return None
