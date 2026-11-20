"""Descrição do contexto do pedido: tipo, tamanho, prévia e hash (spec v0, passo 2 e contrato 1).

O contexto é dado de entrada do pedido (documento, tabela, JSON), nunca código do modelo. Este
módulo só descreve o valor; quem o guarda ou o mostra decide o resto. Ele roda nos dois lados:
no runner (dentro do contêiner, em `context_info`) e no host (metadados da primeira mensagem,
evento `pedido_iniciado` e conferência da resposta do sandbox). Por isso não importa nada do
restante do pacote e usa só a biblioteca padrão.

Decisões deste módulo (não estão na spec):
- Vocabulário dos tipos: `texto` (str), `objeto` (dict) e `lista` (list). Tipos de item, dentro
  da prévia, usam também `numero`, `booleano` e `nulo`.
- A prévia tem no máximo LIMITE_PREVIA caracteres. Em objeto, mostra até MAXIMO_CHAVES_PREVIA
  chaves de topo com o tipo de cada valor; o corte de 200 caracteres pode reduzir esse número
  quando as chaves são longas.
- O hash é o SHA-256 do JSON canônico (chaves ordenadas, `ensure_ascii=False`, UTF-8).
"""

import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Any, Final

# Valor que pode ser o contexto: texto, objeto ou lista de JSON.
ValorContexto = str | dict[str, Any] | list[Any]

LIMITE_PREVIA: Final = 200
MAXIMO_CHAVES_PREVIA: Final = 20


@dataclass(frozen=True)
class DescricaoContexto:
    """Metadados de um contexto: tipo, tamanho na unidade do tipo e prévia curta."""

    tipo: str
    tamanho: int
    previa: str


def tipo_json(valor: Any) -> str:
    """Nome curto do tipo de um valor JSON, para a prévia de objetos e listas."""
    if isinstance(valor, bool):
        return "booleano"
    if isinstance(valor, int | float):
        return "numero"
    if isinstance(valor, str):
        return "texto"
    if isinstance(valor, dict):
        return "objeto"
    if isinstance(valor, list):
        return "lista"
    return "nulo"


def descrever_contexto(valor: ValorContexto) -> DescricaoContexto:
    """Tipo, tamanho e prévia do contexto. Função pura: não altera o valor recebido."""
    if isinstance(valor, str):
        return DescricaoContexto(tipo="texto", tamanho=len(valor), previa=valor[:LIMITE_PREVIA])
    if isinstance(valor, dict):
        pares = [
            f"{chave}: {tipo_json(conteudo)}"
            for chave, conteudo in itertools.islice(valor.items(), MAXIMO_CHAVES_PREVIA)
        ]
        previa = ", ".join(pares)[:LIMITE_PREVIA]
        return DescricaoContexto(tipo="objeto", tamanho=len(valor), previa=previa)
    previa = ""
    if valor:
        primeiro = valor[0]
        resumo = json.dumps(primeiro, ensure_ascii=False)
        previa = f"primeiro item: {tipo_json(primeiro)}; {resumo}"[:LIMITE_PREVIA]
    return DescricaoContexto(tipo="lista", tamanho=len(valor), previa=previa)


def bytes_do_contexto(valor: ValorContexto) -> int:
    """Tamanho em bytes do JSON do contexto, medido como o limite do sandbox exige."""
    return len(json.dumps(valor, ensure_ascii=False).encode("utf-8"))


def sha256_contexto(valor: ValorContexto) -> str:
    """SHA-256 do JSON canônico do contexto: chaves ordenadas, `ensure_ascii=False`, UTF-8."""
    canonico = json.dumps(valor, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonico).hexdigest()
