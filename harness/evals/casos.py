"""Formato dos casos de avaliação e das regras do gabarito (spec v0, "Pedidos de exemplo").

Um caso reúne o pedido, o perfil que o faz, a situação (ativo ou pendente) e o gabarito: as regras
que a resposta final e as fontes precisam cumprir. Aqui fica só o formato. A correção automática,
por regras e sem modelo juiz, está em `harness.evals.correcao`.

Decisões deste módulo (não estão na spec):
- `id` tem o formato `categoria-NN`, e o prefixo precisa ser o valor da categoria.
- `perfil` precisa ser exatamente o nome de um perfil embutido em `harness.host.perfil.PERFIS`.
- Caso pendente exige motivo; caso ativo não tem motivo. A média da T16 deixa os pendentes de
  fora; aqui só `Situacao.pendente` é exposto.
- Cada regra é um modelo congelado com `tipo` como discriminador, para gravar e ler JSON.
- `descrever()` gera uma linha curta para o relatório. Não participa da comparação.
- `Decimal` vai para o JSON como texto, então valores monetários não perdem casas.
"""

from collections import Counter
from collections.abc import Sequence
from decimal import Decimal
from enum import Enum
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from harness.host.perfil import PERFIS


class Categoria(Enum):
    """Categorias do conjunto de avaliação (spec v0, "Pedidos de exemplo")."""

    CONSULTA = "consulta"
    CRUZAMENTO = "cruzamento"
    CONFORMIDADE = "conformidade"
    GERACAO = "geracao"
    ALTERACAO = "alteracao"
    PERMISSAO = "permissao"
    INJECAO = "injecao"


_NOMES_DE_PERFIL: Final = frozenset(perfil.nome for perfil in PERFIS)
_TOLERANCIA_PADRAO: Final = Decimal("0.01")
_LIMITE_JUNTAR: Final = 80


def formatar_numero_br(valor: Decimal) -> str:
    """Número em formato brasileiro: milhar com ponto, decimal com vírgula e sem zeros à direita.

    Exige `Decimal` finito. Exemplo: `Decimal("1234.50")` vira "1.234,5".
    """
    sinal = "-" if valor < 0 else ""
    inteiro, _, fracao = format(abs(valor), "f").partition(".")
    fracao = fracao.rstrip("0")
    agrupado = f"{int(inteiro):,}".replace(",", ".")
    return f"{sinal}{agrupado},{fracao}" if fracao else f"{sinal}{agrupado}"


def _juntar(itens: Sequence[str]) -> str:
    """Textos entre aspas, separados por vírgula, truncados para caber numa linha."""
    texto = ", ".join(f"'{item}'" for item in itens)
    if len(texto) <= _LIMITE_JUNTAR:
        return texto
    return texto[: _LIMITE_JUNTAR - 3] + "..."


def _textos_validos(textos: list[str]) -> list[str]:
    """Cada texto precisa ter ao menos uma letra ou número, senão casaria com qualquer resposta."""
    for texto in textos:
        if not any(caractere.isalnum() for caractere in texto):
            raise ValueError(f"texto sem letras ou números: {texto!r}")
    return textos


def _numeros_finitos(numeros: list[Decimal]) -> list[Decimal]:
    for numero in numeros:
        if not numero.is_finite():
            raise ValueError("número precisa ser finito")
    return numeros


class Situacao(BaseModel):
    """Caso ativo entra na média; caso pendente fica de fora, com o motivo registrado."""

    model_config = ConfigDict(frozen=True)

    ativo: bool
    motivo_pendente: str | None = None

    @model_validator(mode="after")
    def _coerente(self) -> "Situacao":
        if self.ativo and self.motivo_pendente is not None:
            raise ValueError("caso ativo não pode ter motivo pendente")
        if not self.ativo and (self.motivo_pendente is None or not self.motivo_pendente.strip()):
            raise ValueError("caso pendente exige motivo")
        return self

    @property
    def pendente(self) -> bool:
        """Verdadeiro quando o caso está fora da média."""
        return not self.ativo


class ContemNumero(BaseModel):
    """A resposta traz algum número que bate com `valor` dentro da `tolerancia`."""

    model_config = ConfigDict(frozen=True)

    tipo: Literal["contem_numero"] = "contem_numero"
    valor: Decimal
    tolerancia: Decimal = Field(default=_TOLERANCIA_PADRAO, ge=0)
    # A unidade só muda a descrição. A comparação não confere se o número veio com % ou R$.
    unidade: Literal["valor", "percentual", "quantidade"] = "valor"

    @field_validator("valor", "tolerancia")
    @classmethod
    def _finito(cls, numero: Decimal) -> Decimal:
        if not numero.is_finite():
            raise ValueError("número precisa ser finito")
        return numero

    def descrever(self) -> str:
        numero = formatar_numero_br(self.valor)
        texto = {
            "valor": f"R$ {numero}",
            "percentual": f"{numero}%",
            "quantidade": numero,
        }[self.unidade]
        return f"resposta traz {texto} (tolerância {formatar_numero_br(self.tolerancia)})"


class ContemTodos(BaseModel):
    """A resposta cita todos os textos, sem diferença de acento, caixa ou espaços."""

    model_config = ConfigDict(frozen=True)

    tipo: Literal["contem_todos"] = "contem_todos"
    textos: list[str] = Field(min_length=1)

    @field_validator("textos")
    @classmethod
    def _validos(cls, textos: list[str]) -> list[str]:
        return _textos_validos(textos)

    def descrever(self) -> str:
        return f"resposta cita {_juntar(self.textos)}"


class ContemAlgum(BaseModel):
    """A resposta cita pelo menos um dos textos, com a mesma normalização de `ContemTodos`."""

    model_config = ConfigDict(frozen=True)

    tipo: Literal["contem_algum"] = "contem_algum"
    textos: list[str] = Field(min_length=1)

    @field_validator("textos")
    @classmethod
    def _validos(cls, textos: list[str]) -> list[str]:
        return _textos_validos(textos)

    def descrever(self) -> str:
        return f"resposta cita ao menos um de {_juntar(self.textos)}"


class ContemExato(BaseModel):
    """A resposta cita cada texto literalmente: maiúsculas, minúsculas e acentos contam.

    O texto precisa estar delimitado: o caractere antes e o depois do trecho, se existirem, não
    pode ser letra ou dígito. O ponto final faz parte do trecho quando o gabarito o traz.
    """

    model_config = ConfigDict(frozen=True)

    tipo: Literal["contem_exato"] = "contem_exato"
    textos: list[str] = Field(min_length=1)

    @field_validator("textos")
    @classmethod
    def _validos(cls, textos: list[str]) -> list[str]:
        return _textos_validos(textos)

    def descrever(self) -> str:
        return f"resposta cita na grafia exata {_juntar(self.textos)}"


class NaoContem(BaseModel):
    """A resposta não pode trazer nenhum dos textos nem nenhum dos números (tolerância 0,01)."""

    model_config = ConfigDict(frozen=True)

    tipo: Literal["nao_contem"] = "nao_contem"
    textos: list[str] = Field(default_factory=list)
    numeros: list[Decimal] = Field(default_factory=list)

    @field_validator("textos")
    @classmethod
    def _validos(cls, textos: list[str]) -> list[str]:
        return _textos_validos(textos)

    @field_validator("numeros")
    @classmethod
    def _finitos(cls, numeros: list[Decimal]) -> list[Decimal]:
        return _numeros_finitos(numeros)

    @model_validator(mode="after")
    def _algo_proibido(self) -> "NaoContem":
        if not self.textos and not self.numeros:
            raise ValueError("NaoContem precisa de ao menos um texto ou número")
        return self

    def descrever(self) -> str:
        itens = [*self.textos, *(formatar_numero_br(numero) for numero in self.numeros)]
        return f"resposta não traz {_juntar(itens)}"


class CitaFontes(BaseModel):
    """Todas as fontes esperadas aparecem entre as fontes do resultado.

    A comparação é pelo fim do caminho, por componentes inteiros: `pedidos_2026-10.xlsx` casa
    com `vendas/pedidos_2026-10.xlsx`, e `vendas/pedidos_2026-10.xlsx` não casa com
    `comercial/pedidos_2026-10.xlsx`.
    """

    model_config = ConfigDict(frozen=True)

    tipo: Literal["cita_fontes"] = "cita_fontes"
    fontes: list[str] = Field(min_length=1)

    @field_validator("fontes")
    @classmethod
    def _validas(cls, fontes: list[str]) -> list[str]:
        return _textos_validos(fontes)

    def descrever(self) -> str:
        return f"fontes incluem {_juntar(self.fontes)}"


class RecusaAcesso(BaseModel):
    """A resposta diz que o dado não está acessível. A lista de frases fica em `correcao`."""

    model_config = ConfigDict(frozen=True)

    tipo: Literal["recusa_acesso"] = "recusa_acesso"

    def descrever(self) -> str:
        return "resposta recusa o acesso ao dado"


Regra = Annotated[
    ContemNumero | ContemTodos | ContemAlgum | ContemExato | NaoContem | CitaFontes | RecusaAcesso,
    Field(discriminator="tipo"),
]


class Caso(BaseModel):
    """Um pedido de avaliação com o perfil que o faz, a situação e o gabarito."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z]+-\d{2}$")
    categoria: Categoria
    perfil: str
    pedido: str
    situacao: Situacao
    gabarito: list[Regra] = Field(min_length=1)

    @field_validator("perfil")
    @classmethod
    def _perfil_existe(cls, perfil: str) -> str:
        if perfil not in _NOMES_DE_PERFIL:
            raise ValueError(f"perfil desconhecido: {perfil!r}")
        return perfil

    @field_validator("pedido")
    @classmethod
    def _pedido_nao_vazio(cls, pedido: str) -> str:
        if not pedido.strip():
            raise ValueError("pedido não pode ser vazio")
        return pedido

    @model_validator(mode="after")
    def _id_de_acordo_com_a_categoria(self) -> "Caso":
        prefixo = self.id.split("-", 1)[0]
        if prefixo != self.categoria.value:
            raise ValueError(f"id {self.id!r} não começa com a categoria {self.categoria.value!r}")
        return self


def validar_conjunto(casos: Sequence[Caso]) -> None:
    """Levanta ValueError se algum id aparece mais de uma vez no conjunto."""
    contagem = Counter(caso.id for caso in casos)
    repetidos = sorted(identificador for identificador, n in contagem.items() if n > 1)
    if repetidos:
        raise ValueError(f"ids duplicados: {', '.join(repetidos)}")


_ADAPTADOR_CASOS: Final = TypeAdapter(list[Caso])


def casos_para_json(casos: Sequence[Caso]) -> str:
    """Serializa o conjunto em JSON, com acentos sem escape."""
    return _ADAPTADOR_CASOS.dump_json(list(casos), indent=2).decode("utf-8")


def casos_de_json(texto: str) -> list[Caso]:
    """Lê um conjunto de JSON e valida cada caso e a unicidade dos ids."""
    casos = _ADAPTADOR_CASOS.validate_json(texto)
    validar_conjunto(casos)
    return casos
