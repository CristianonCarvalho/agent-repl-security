"""Ferramentas no formato de chamada de função da API, e o mapeamento dos nomes (T18a).

A API só aceita nomes de função com `^[a-zA-Z0-9_-]{1,64}$`. O registro usa `grupo.nome`
(`arquivos.ler_planilha`), com ponto. O mapeamento troca o ponto por `__`
(`arquivos__ler_planilha`) e volta com a mesma regra.

Decisões deste módulo (não estão na spec):
- A ida e a volta só são únicas se as partes do nome não tiverem `__`, nem `_` no começo ou no
  fim. Sem isso, `a____b` poderia vir de `a.__b` ou de `a__.b`. Por isso `nome_para_api` recusa
  nomes fora dessa forma, em vez de mapear e torcer para não haver ambiguidade.
- `nome_do_registro` devolve None para nome sem exatamente um `__` ou com partes fora da forma.
  Assim, `finalizar` (sem ponto) nunca é confundido com uma ferramenta do registro.
- O hash dos esquemas usa o JSON canônico (chaves ordenadas, sem espaços). Ele entra na
  procedência do relatório: muda quando o conjunto de ferramentas enviado ao modelo muda.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator

NOME_API_VALIDO: Final = re.compile(r"[a-zA-Z0-9_-]{1,64}")
# Uma parte do nome do registro: letras minúsculas e `_` só entre letras (sem `__`, sem borda `_`).
_PARTE: Final = r"[a-z]+(?:_[a-z]+)*"
_NOME_DO_REGISTRO: Final = re.compile(rf"{_PARTE}\.{_PARTE}")
SEPARADOR_API: Final = "__"


class EsquemaFerramenta(BaseModel):
    """Uma ferramenta como a API a recebe: nome, descrição e JSON Schema dos argumentos."""

    model_config = ConfigDict(frozen=True)

    nome: str
    descricao: str
    parametros: dict[str, Any]

    @field_validator("nome")
    @classmethod
    def _nome_aceito_pela_api(cls, valor: str) -> str:
        if NOME_API_VALIDO.fullmatch(valor) is None:
            raise ValueError("nome de função fora de ^[a-zA-Z0-9_-]{1,64}$")
        return valor


def nome_para_api(nome_do_registro: str) -> str:
    """Nome da função na API para um nome do registro (`grupo.nome` vira `grupo__nome`).

    Levanta ValueError para nome fora da forma que mapeia de volta sem ambiguidade.
    """
    if _NOME_DO_REGISTRO.fullmatch(nome_do_registro) is None:
        raise ValueError(f"nome de ferramenta sem mapeamento para a API: {nome_do_registro!r}")
    return nome_do_registro.replace(".", SEPARADOR_API)


def nome_do_registro(nome_api: str) -> str | None:
    """Nome do registro para um nome da API, ou None se ele não veio de `nome_para_api`."""
    if nome_api.count(SEPARADOR_API) != 1:
        return None
    grupo, nome = nome_api.split(SEPARADOR_API)
    candidato = f"{grupo}.{nome}"
    if _NOME_DO_REGISTRO.fullmatch(candidato) is None:
        return None
    return candidato


def hash_dos_esquemas(esquemas: Sequence[EsquemaFerramenta]) -> str:
    """SHA-256 do JSON canônico dos esquemas enviados ao modelo, na ordem dada."""
    canonico = json.dumps(
        [esquema.model_dump(mode="json") for esquema in esquemas],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()
