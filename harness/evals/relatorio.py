"""Relatório da avaliação: modelos congelados, agregação pura e gravação em JSON (T16).

`agregar` recebe os casos e as rodadas já prontas e calcula taxas, variação, custo e tokens.
Não toca em rede nem em disco, então os números podem ser conferidos à mão nos testes.
`escrever_relatorio` grava o JSON numa pasta, com nome que traz data, hora UTC e semente.

Decisões deste módulo (não estão na spec):
- Taxa do caso = aprovadas / (aprovadas + reprovadas). `erro_infra`, `nao_executada` e
  `pendente` ficam fora. Desvio do caso = desvio padrão populacional dos 0/1 das rodadas
  válidas; com menos de duas rodadas válidas, o desvio é None.
- Taxa da categoria e taxa total: média das taxas dos casos que têm taxa. Cada caso pesa igual.
- `taxas_por_rodada[k]`: média, entre os casos ativos que têm rodada válida com número k+1, do
  resultado 0/1 dessa rodada. `desvio_entre_rodadas` é o desvio padrão populacional dessas
  taxas, e fica None com menos de duas.
- `rodadas_sem_custo` conta as rodadas em que o modelo respondeu e nenhum custo veio. Custo
  parcial (algumas respostas com custo) conta como conhecido e não entra nessa contagem.
- `hash_casos` é o SHA-256 de `casos_para_json`. `hash_prompt` é o primeiro não nulo, na
  ordem dos casos e das rodadas.
- Coerência, checada na construção: `aprovada` exige veredito aprovado; `reprovada` exige
  veredito reprovado; `pendente` só em caso não ativo; `detalhe` só em `erro_infra` e
  `nao_executada`. Em `agregar`, rodada de caso pendente só pode ser `pendente`.
- Escrita nunca sobrescreve: se o nome já existe, ganha um sufixo numérico.
- `grupos` tem a taxa de cada grupo (desenvolvimento e reservado), com a mesma regra da taxa
  total: média das taxas dos casos ativos do grupo que têm taxa. Relatório antigo sem `grupo`
  em `ResumoCaso` carrega como desenvolvimento, e sem `grupos` carrega com a tupla vazia.
"""

import hashlib
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.evals.casos import GRUPOS_DE_CASOS, Caso, Categoria, GrupoCaso, casos_para_json
from harness.evals.correcao import Veredito


class ErroDeRelatorio(Exception):
    """O relatório não pôde ser gravado. A mensagem cita a pasta e o motivo do sistema."""


class Classificacao(Enum):
    """Resultado de uma rodada. Só `aprovada` e `reprovada` entram na taxa."""

    APROVADA = "aprovada"
    REPROVADA = "reprovada"
    ERRO_INFRA = "erro_infra"
    NAO_EXECUTADA = "nao_executada"
    PENDENTE = "pendente"


_VALIDAS: Final = (Classificacao.APROVADA, Classificacao.REPROVADA)
_SEM_VEREDITO: Final = (
    Classificacao.ERRO_INFRA,
    Classificacao.NAO_EXECUTADA,
    Classificacao.PENDENTE,
)
_COM_DETALHE: Final = (Classificacao.ERRO_INFRA, Classificacao.NAO_EXECUTADA)


class RodadaCaso(BaseModel):
    """Uma execução de um caso. `detalhe` traz só o motivo curto, nunca texto de exceção."""

    model_config = ConfigDict(frozen=True)

    numero: int = Field(ge=1)
    classificacao: Classificacao
    veredito: Veredito | None = None
    motivo_termino: str | None = None
    iteracoes: int = Field(default=0, ge=0)
    tokens_entrada: int = Field(default=0, ge=0)
    tokens_saida: int = Field(default=0, ge=0)
    custo: float | None = None
    duracao_s: float = Field(default=0.0, ge=0)
    caminho_trace: str | None = None
    hash_prompt: str | None = None
    detalhe: str | None = None

    @model_validator(mode="after")
    def _coerente(self) -> Self:
        if self.classificacao is Classificacao.APROVADA:
            if self.veredito is None or not self.veredito.aprovado:
                raise ValueError("rodada aprovada exige veredito aprovado")
        if self.classificacao is Classificacao.REPROVADA:
            if self.veredito is None or self.veredito.aprovado:
                raise ValueError("rodada reprovada exige veredito reprovado")
        if self.classificacao in _SEM_VEREDITO and self.veredito is not None:
            raise ValueError(f"rodada {self.classificacao.value} não tem veredito")
        if self.detalhe is not None and self.classificacao not in _COM_DETALHE:
            raise ValueError("detalhe só vale para erro_infra e nao_executada")
        return self


class ResumoCaso(BaseModel):
    """Um caso com as rodadas, a taxa e o desvio das rodadas válidas."""

    model_config = ConfigDict(frozen=True)

    caso_id: str
    categoria: Categoria
    situacao: Literal["ativo", "pendente"]
    rodadas: tuple[RodadaCaso, ...] = ()
    taxa: float | None = None
    desvio: float | None = None
    grupo: GrupoCaso = "desenvolvimento"


class ResumoCategoria(BaseModel):
    """Média das taxas dos casos de uma categoria que têm taxa."""

    model_config = ConfigDict(frozen=True)

    categoria: Categoria
    taxa: float | None = None
    casos_considerados: int = Field(ge=0)


class ResumoGrupo(BaseModel):
    """Média das taxas dos casos de um grupo (desenvolvimento ou reservado) que têm taxa."""

    model_config = ConfigDict(frozen=True)

    grupo: GrupoCaso
    taxa: float | None = None
    casos_considerados: int = Field(ge=0)


class Relatorio(BaseModel):
    """Relatório completo de uma execução de avaliação. Ida e volta em JSON não perde nada."""

    model_config = ConfigDict(frozen=True)

    variante: Literal["repl", "json"] = "repl"
    semente: int
    rodadas_por_caso: int = Field(ge=1)
    modelo: str | None = None
    provedores: tuple[str, ...] = ()
    hash_casos: str
    hash_prompt: str | None = None
    casos: tuple[ResumoCaso, ...] = ()
    categorias: tuple[ResumoCategoria, ...] = ()
    grupos: tuple[ResumoGrupo, ...] = ()
    taxa_total: float | None = None
    taxas_por_rodada: tuple[float | None, ...] = ()
    desvio_entre_rodadas: float | None = None
    tokens_entrada_total: int = Field(default=0, ge=0)
    tokens_saida_total: int = Field(default=0, ge=0)
    custo_total: float | None = None
    rodadas_sem_custo: int = Field(default=0, ge=0)
    erros_infra: int = Field(default=0, ge=0)
    nao_executadas: int = Field(default=0, ge=0)
    pendentes: tuple[str, ...] = ()
    teto_estourado: bool = False
    teto_motivo: str | None = None
    # Nome curto da telemetria ligada ("langfuse-us"), ou None. Nunca traz endpoint nem chaves.
    telemetria: str | None = None
    # Falhas de telemetria somadas em todas as rodadas. None quando a telemetria está desligada;
    # zero também é um número válido, quando ela está ligada e nada falhou.
    falhas_telemetria: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _coerente(self) -> Self:
        if len(self.taxas_por_rodada) != self.rodadas_por_caso:
            raise ValueError("taxas_por_rodada precisa ter uma entrada por rodada")
        if self.teto_estourado != (self.teto_motivo is not None):
            raise ValueError("teto_estourado e teto_motivo precisam andar juntos")
        return self


# --- agregação --------------------------------------------------------------------------------


def _pontos(rodadas: Iterable[RodadaCaso]) -> list[float]:
    """1 para cada rodada aprovada e 0 para cada reprovada. As demais ficam de fora."""
    return [
        1.0 if r.classificacao is Classificacao.APROVADA else 0.0
        for r in rodadas
        if r.classificacao in _VALIDAS
    ]


def _conferir_rodadas(caso: Caso, rodadas: Sequence[RodadaCaso], total: int) -> None:
    numeros = [r.numero for r in rodadas]
    if len(set(numeros)) != len(numeros):
        raise ValueError(f"caso {caso.id}: número de rodada repetido")
    for rodada in rodadas:
        if rodada.numero > total:
            raise ValueError(f"caso {caso.id}: rodada {rodada.numero} acima de {total}")
        if caso.situacao.ativo and rodada.classificacao is Classificacao.PENDENTE:
            raise ValueError(f"caso ativo {caso.id} não pode ter rodada pendente")
        if not caso.situacao.ativo and rodada.classificacao is not Classificacao.PENDENTE:
            raise ValueError(f"caso pendente {caso.id} não tem rodadas executadas")


def _resumir(caso: Caso, rodadas: tuple[RodadaCaso, ...]) -> ResumoCaso:
    pontos = _pontos(rodadas)
    taxa = sum(pontos) / len(pontos) if pontos else None
    desvio = statistics.pstdev(pontos) if len(pontos) >= 2 else None
    return ResumoCaso(
        caso_id=caso.id,
        categoria=caso.categoria,
        situacao="ativo" if caso.situacao.ativo else "pendente",
        rodadas=rodadas,
        taxa=taxa,
        desvio=desvio,
        grupo=caso.grupo,
    )


def _media(valores: Sequence[float]) -> float | None:
    return statistics.fmean(valores) if valores else None


def _categorias(ativos: Sequence[ResumoCaso]) -> tuple[ResumoCategoria, ...]:
    resumos: list[ResumoCategoria] = []
    for categoria in Categoria:
        do_grupo = [r for r in ativos if r.categoria is categoria]
        if not do_grupo:
            continue
        taxas = [r.taxa for r in do_grupo if r.taxa is not None]
        resumos.append(
            ResumoCategoria(categoria=categoria, taxa=_media(taxas), casos_considerados=len(taxas))
        )
    return tuple(resumos)


def _grupos(ativos: Sequence[ResumoCaso]) -> tuple[ResumoGrupo, ...]:
    resumos: list[ResumoGrupo] = []
    for grupo in GRUPOS_DE_CASOS:
        do_grupo = [r for r in ativos if r.grupo == grupo]
        if not do_grupo:
            continue
        taxas = [r.taxa for r in do_grupo if r.taxa is not None]
        resumos.append(ResumoGrupo(grupo=grupo, taxa=_media(taxas), casos_considerados=len(taxas)))
    return tuple(resumos)


def _taxas_por_rodada(ativos: Sequence[ResumoCaso], total: int) -> tuple[float | None, ...]:
    taxas: list[float | None] = []
    for numero in range(1, total + 1):
        pontos = [
            1.0 if r.classificacao is Classificacao.APROVADA else 0.0
            for resumo in ativos
            for r in resumo.rodadas
            if r.numero == numero and r.classificacao in _VALIDAS
        ]
        taxas.append(_media(pontos))
    return tuple(taxas)


def agregar(
    casos: Sequence[Caso],
    rodadas: Mapping[str, Sequence[RodadaCaso]],
    *,
    semente: int,
    rodadas_por_caso: int,
    variante: Literal["repl", "json"] = "repl",
    modelo: str | None = None,
    provedores: Iterable[str] = (),
    teto_motivo: str | None = None,
    falhas_telemetria: int | None = None,
) -> Relatorio:
    """Monta o relatório a partir dos casos e das rodadas de cada um. Não faz I/O.

    `rodadas` tem uma entrada por id de caso; casos sem entrada ficam sem rodadas. Levanta
    ValueError se uma rodada não bate com o caso (pendente em caso ativo, número fora da faixa).
    """
    ids = {caso.id for caso in casos}
    desconhecidos = sorted(set(rodadas) - ids)
    if desconhecidos:
        raise ValueError(f"rodadas de casos fora do conjunto: {', '.join(desconhecidos)}")

    resumos: list[ResumoCaso] = []
    for caso in casos:
        ordenadas = tuple(sorted(rodadas.get(caso.id, ()), key=lambda r: r.numero))
        _conferir_rodadas(caso, ordenadas, rodadas_por_caso)
        resumos.append(_resumir(caso, ordenadas))

    ativos = [r for r in resumos if r.situacao == "ativo"]
    taxas_caso = [r.taxa for r in ativos if r.taxa is not None]
    taxas_rodada = _taxas_por_rodada(ativos, rodadas_por_caso)
    valores_rodada = [t for t in taxas_rodada if t is not None]
    todas = [rodada for resumo in resumos for rodada in resumo.rodadas]
    custos = [r.custo for r in todas if r.custo is not None]
    hash_prompt = next((r.hash_prompt for r in todas if r.hash_prompt is not None), None)

    return Relatorio(
        variante=variante,
        semente=semente,
        rodadas_por_caso=rodadas_por_caso,
        modelo=modelo,
        provedores=tuple(sorted(set(provedores))),
        hash_casos=hashlib.sha256(casos_para_json(list(casos)).encode("utf-8")).hexdigest(),
        hash_prompt=hash_prompt,
        casos=tuple(resumos),
        categorias=_categorias(ativos),
        grupos=_grupos(ativos),
        taxa_total=_media(taxas_caso),
        taxas_por_rodada=taxas_rodada,
        desvio_entre_rodadas=(
            statistics.pstdev(valores_rodada) if len(valores_rodada) >= 2 else None
        ),
        tokens_entrada_total=sum(r.tokens_entrada for r in todas),
        tokens_saida_total=sum(r.tokens_saida for r in todas),
        custo_total=sum(custos) if custos else None,
        rodadas_sem_custo=sum(
            1 for r in todas if r.custo is None and r.tokens_entrada + r.tokens_saida > 0
        ),
        erros_infra=sum(1 for r in todas if r.classificacao is Classificacao.ERRO_INFRA),
        nao_executadas=sum(1 for r in todas if r.classificacao is Classificacao.NAO_EXECUTADA),
        pendentes=tuple(caso.id for caso in casos if not caso.situacao.ativo),
        teto_estourado=teto_motivo is not None,
        teto_motivo=teto_motivo,
        falhas_telemetria=falhas_telemetria,
    )


# --- JSON e arquivo ---------------------------------------------------------------------------


def relatorio_para_json(relatorio: Relatorio) -> str:
    """Serializa o relatório em JSON, com acentos sem escape."""
    return relatorio.model_dump_json(indent=2)


def relatorio_de_json(texto: str) -> Relatorio:
    """Lê um relatório de JSON e valida tudo de novo."""
    return Relatorio.model_validate_json(texto)


def escrever_relatorio(relatorio: Relatorio, pasta: Path) -> Path:
    """Grava o relatório em `pasta` e devolve o caminho. Cria a pasta se preciso.

    O nome traz a data e a hora UTC e a semente. Se o nome já existe, ganha um sufixo; nunca
    sobrescreve. Falha de I/O levanta `ErroDeRelatorio`.
    """
    texto = relatorio_para_json(relatorio) + "\n"
    marca = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = f"avaliacao-{marca}-semente{relatorio.semente}"
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        indice = 1
        while True:
            sufixo = "" if indice == 1 else f"-{indice}"
            caminho = pasta / f"{base}{sufixo}.json"
            try:
                with caminho.open("x", encoding="utf-8") as arquivo:
                    arquivo.write(texto)
                return caminho
            except FileExistsError:
                indice += 1
    except OSError as erro:
        motivo = erro.strerror or type(erro).__name__
        raise ErroDeRelatorio(f"não foi possível gravar o relatório em {pasta}: {motivo}") from erro
