"""Configuração do cliente OpenRouter (spec v0, "Modelo e provedor").

Tudo o que muda entre modelos fica aqui, para que os evals comparem modelos sem mudar código.
A chave da API não faz parte desta configuração: ela é passada separada ao cliente e nunca
aparece em `ConfigModelo`.

Decisões deste módulo (não estão na spec):
- `slug` não tem valor padrão: o snapshot exato do DeepSeek V4.1 Flash ainda será confirmado.
  Vem de `OPENROUTER_MODELO`, e a ausência dele é erro claro em `config_de_ambiente`.
- Variáveis opcionais: `OPENROUTER_TEMPERATURA`, `OPENROUTER_SEED`, `OPENROUTER_FALLBACK`
  (1/0, true/false, sim/não, yes/no) e `OPENROUTER_PRAZO_TOTAL` (segundos, finito e positivo;
  padrão 120). Vazia ou ausente conta como não definida.
- `prazo_total_s` não pode ser menor que `timeout_s`: o prazo da chamada inteira tem de caber
  pelo menos uma tentativa.
- `carregar_env` é um leitor mínimo de `.env`, sem dependência extra. Ele não altera
  `os.environ`: quem chama decide a precedência, por exemplo
  `{**carregar_env(caminho), **os.environ}`. Não remove comentários no fim da linha, porque
  um valor pode conter `#`.
- Mensagens de erro citam o nome da variável, nunca o seu valor.
"""

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

URL_PADRAO: Final = "https://openrouter.ai/api/v1"
_VERDADEIROS: Final = frozenset({"1", "true", "sim", "yes", "on"})
_FALSOS: Final = frozenset({"0", "false", "nao", "não", "no", "off"})


class ConfigModelo(BaseModel):
    """Parâmetros de uma chamada ao modelo. Valores fora da faixa falham."""

    model_config = ConfigDict(frozen=True)

    slug: str = Field(min_length=1)
    base_url: str = URL_PADRAO
    temperatura: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int | None = None
    fallback_provedor: bool = False
    timeout_s: float = Field(default=60.0, gt=0)
    # Prazo de uma chamada inteira a `responder`, com todas as tentativas e esperas. O timeout
    # por tentativa (`timeout_s`) só limita cada operação de socket, então precisa deste teto.
    prazo_total_s: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    max_tentativas: int = Field(default=3, ge=1)
    max_tokens_saida: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _prazo_cobre_a_tentativa(self) -> "ConfigModelo":
        if self.prazo_total_s < self.timeout_s:
            raise ValueError(
                "prazo_total_s não pode ser menor que timeout_s: o prazo da chamada inteira "
                "tem de cobrir pelo menos uma tentativa"
            )
        return self


def config_de_ambiente(env: Mapping[str, str]) -> ConfigModelo:
    """Monta a configuração a partir de variáveis de ambiente (ou de um mapping equivalente).

    Levanta `ValueError` se `OPENROUTER_MODELO` faltar ou se um valor opcional não for válido.
    """
    slug = env.get("OPENROUTER_MODELO", "").strip()
    if not slug:
        raise ValueError(
            "OPENROUTER_MODELO não definido: informe o slug exato do modelo no OpenRouter"
        )
    campos: dict[str, Any] = {"slug": slug}
    temperatura = _float_opcional(env, "OPENROUTER_TEMPERATURA")
    if temperatura is not None:
        campos["temperatura"] = temperatura
    seed = _int_opcional(env, "OPENROUTER_SEED")
    if seed is not None:
        campos["seed"] = seed
    fallback = _bool_opcional(env, "OPENROUTER_FALLBACK")
    if fallback is not None:
        campos["fallback_provedor"] = fallback
    prazo = _float_opcional(env, "OPENROUTER_PRAZO_TOTAL")
    if prazo is not None:
        # Checado aqui e não só pelo Pydantic: a mensagem do Pydantic repete o valor recebido.
        if not math.isfinite(prazo) or prazo <= 0:
            raise ValueError("OPENROUTER_PRAZO_TOTAL deve ser um número positivo e finito")
        campos["prazo_total_s"] = prazo
    return ConfigModelo(**campos)


def carregar_env(caminho: Path) -> dict[str, str]:
    """Lê um arquivo `.env` simples (KEY=VALUE). Arquivo ausente ou ilegível devolve {}.

    Linhas em branco e comentários (`#`) são ignoradas; `export ` no início é aceito; aspas
    simples ou duplas que envolvem o valor são removidas.
    """
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    valores: dict[str, str] = {}
    for linha in linhas:
        limpa = linha.strip()
        if not limpa or limpa.startswith("#"):
            continue
        if limpa.startswith("export "):
            limpa = limpa[len("export ") :].lstrip()
        chave, separador, valor = limpa.partition("=")
        chave = chave.strip()
        if not separador or not chave:
            continue
        valores[chave] = _sem_aspas(valor.strip())
    return valores


def _sem_aspas(valor: str) -> str:
    if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in ("'", '"'):
        return valor[1:-1]
    return valor


def _valor_opcional(env: Mapping[str, str], nome: str) -> str | None:
    bruto = env.get(nome, "").strip()
    return bruto or None


def _float_opcional(env: Mapping[str, str], nome: str) -> float | None:
    bruto = _valor_opcional(env, nome)
    if bruto is None:
        return None
    try:
        return float(bruto)
    except ValueError:
        raise ValueError(f"{nome} deve ser um número") from None


def _int_opcional(env: Mapping[str, str], nome: str) -> int | None:
    bruto = _valor_opcional(env, nome)
    if bruto is None:
        return None
    try:
        return int(bruto)
    except ValueError:
        raise ValueError(f"{nome} deve ser um inteiro") from None


def _bool_opcional(env: Mapping[str, str], nome: str) -> bool | None:
    bruto = _valor_opcional(env, nome)
    if bruto is None:
        return None
    minusculo = bruto.lower()
    if minusculo in _VERDADEIROS:
        return True
    if minusculo in _FALSOS:
        return False
    raise ValueError(f"{nome} deve ser 1/0, true/false ou sim/não")
