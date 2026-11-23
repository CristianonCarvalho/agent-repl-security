"""Comparação de duas execuções da avaliação: a linha de base contra uma execução nova (T17c).

Funções puras: não fazem I/O, rede nem chamam modelo. Quem lê os JSON é a CLI.

Regra final (decisões deste módulo, não estão na spec):
- Unidades: taxas e desvios do relatório são frações (0 a 1). Tudo que sai daqui está em pontos
  percentuais (p.p.), ou seja, fração × 100.
- Comparabilidade: só entram os casos que estão nas duas execuções, com o mesmo `hash_caso` e
  taxa nas duas. Casos só em um lado, com hash diferente (alterados) e sem taxa ficam listados à
  parte, fora da média. Sem nenhum caso em comum, ou com sementes diferentes, o veredito é
  `incomparavel`, que tem prioridade sobre qualquer regressão.
- Relatório sem `hash_caso` (antigo) casa por `caso_id`, com aviso, porque não dá para garantir
  que o caso é o mesmo.
- Médias: total, categoria e grupo são a média das taxas dos casos comparáveis, cada caso pesa
  igual (a mesma regra do relatório).
- Margem: `max(piso_pp, fator_ruido × desvio_base_pp)`, com o desvio entre rodadas da BASE, que é
  a referência. Sem esse desvio, vale só o piso, com aviso.
- Recorte (categoria ou grupo) com `n` casos da interseção de `N` casos: a margem é escalada por
  `sqrt(N / n)`. Recortes menores são mais ruidosos (a média de poucos casos oscila mais), então
  a margem cresce. O piso também é escalado.
- Veredito do recorte: delta > margem é `melhorou`; delta < -margem é `regressao_provavel`; o
  resto é `dentro_do_ruido`. Sem taxa dos dois lados é `sem_dados`.
- Por caso (só indicativo): `piorou` ou `melhorou` quando a diferença de taxa é de pelo menos
  `limite_caso` (padrão 1/3) e os dois lados têm pelo menos 3 rodadas válidas. Senão, `igual`.
- Veredito geral: `regressao_provavel` se o total ou o grupo reservado estiver em regressão
  provável; `ok` nos demais casos, mesmo com melhora.
- Custo e tokens são os totais de cada execução (não da interseção). Iterações médias são a média
  de `iteracoes` nas rodadas aprovadas e reprovadas de cada execução, porque o relatório não tem
  um total de iterações.
- Contexto (modelo, provedores, hash do prompt, commit, árvore suja, orçamento, rodadas, versão
  do conjunto, telemetria) é informativo: diferenças são listadas, nunca bloqueiam.
"""

import math
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from harness.evals.casos import GRUPOS_DE_CASOS, Categoria
from harness.evals.relatorio import Classificacao, Relatorio, ResumoCaso

PISO_PP_PADRAO: Final = 2.0
FATOR_RUIDO_PADRAO: Final = 2.0
LIMITE_CASO_PADRAO: Final = 1 / 3
RODADAS_MINIMAS_CASO: Final = 3
_TOLERANCIA: Final = 1e-9

VereditoRecorte = Literal["melhorou", "regressao_provavel", "dentro_do_ruido", "sem_dados"]
VereditoGeral = Literal["ok", "regressao_provavel", "incomparavel"]
FlagCaso = Literal["piorou", "melhorou", "igual"]


class DeltaRecorte(BaseModel):
    """Taxas de um recorte (total, categoria ou grupo) nas duas execuções, em pontos percentuais."""

    model_config = ConfigDict(frozen=True)

    tipo: Literal["total", "categoria", "grupo"]
    nome: str
    casos: int
    base_pp: float | None = None
    novo_pp: float | None = None
    delta_pp: float | None = None
    margem_pp: float | None = None
    veredito: VereditoRecorte


class ComparacaoCaso(BaseModel):
    """Um caso comparável: taxas em pontos percentuais e a flag indicativa."""

    model_config = ConfigDict(frozen=True)

    caso_id: str
    categoria: str
    grupo: str
    base_pp: float
    novo_pp: float
    delta_pp: float
    rodadas_base: int
    rodadas_novo: int
    flag: FlagCaso


class DiferencaContexto(BaseModel):
    """Um campo de contexto nas duas execuções. Texto já formatado; None vira "não informado"."""

    model_config = ConfigDict(frozen=True)

    campo: str
    base: str | None
    novo: str | None
    diferente: bool


class DiferencaValor(BaseModel):
    """Um número que pode ser comparado entre as execuções (custo, tokens, iterações)."""

    model_config = ConfigDict(frozen=True)

    nome: str
    base: float | None
    novo: float | None
    delta: float | None


class Comparacao(BaseModel):
    """Resultado completo da comparação. Serializa em JSON sem perda."""

    model_config = ConfigDict(frozen=True)

    veredito: VereditoGeral
    motivos: tuple[str, ...]
    avisos: tuple[str, ...] = ()
    semente_base: int
    semente_novo: int
    piso_pp: float
    fator_ruido: float
    limite_caso: float
    desvio_base_pp: float | None = None
    casos_comparados: int
    contexto: tuple[DiferencaContexto, ...] = ()
    total: DeltaRecorte | None = None
    categorias: tuple[DeltaRecorte, ...] = ()
    grupos: tuple[DeltaRecorte, ...] = ()
    casos: tuple[ComparacaoCaso, ...] = ()
    so_na_base: tuple[str, ...] = ()
    so_no_novo: tuple[str, ...] = ()
    hash_diferente: tuple[str, ...] = ()
    sem_taxa: tuple[str, ...] = ()
    valores: tuple[DiferencaValor, ...] = ()


def _validas(resumo: ResumoCaso) -> int:
    return sum(
        1
        for r in resumo.rodadas
        if r.classificacao in (Classificacao.APROVADA, Classificacao.REPROVADA)
    )


def _pp(fracao: float) -> float:
    return fracao * 100.0


def _margem(piso_pp: float, fator_ruido: float, desvio_pp: float | None) -> float:
    if desvio_pp is None:
        return piso_pp
    return max(piso_pp, fator_ruido * desvio_pp)


def _veredito_recorte(delta_pp: float, margem_pp: float) -> VereditoRecorte:
    if delta_pp > margem_pp + _TOLERANCIA:
        return "melhorou"
    if delta_pp < -margem_pp - _TOLERANCIA:
        return "regressao_provavel"
    return "dentro_do_ruido"


def _flag(base: ResumoCaso, novo: ResumoCaso, limite_caso: float) -> FlagCaso:
    if _validas(base) < RODADAS_MINIMAS_CASO or _validas(novo) < RODADAS_MINIMAS_CASO:
        return "igual"
    diferenca = (base.taxa or 0.0) - (novo.taxa or 0.0)
    if diferenca >= limite_caso - _TOLERANCIA:
        return "piorou"
    if -diferenca >= limite_caso - _TOLERANCIA:
        return "melhorou"
    return "igual"


def _recorte(
    tipo: Literal["total", "categoria", "grupo"],
    nome: str,
    pares: Sequence[tuple[ResumoCaso, ResumoCaso]],
    *,
    total_comparaveis: int,
    margem_total_pp: float,
) -> DeltaRecorte:
    """Taxas de um recorte da interseção. Margem escalada pelo tamanho do recorte."""
    if not pares:
        return DeltaRecorte(tipo=tipo, nome=nome, casos=0, veredito="sem_dados")
    base_pp = _pp(sum(a.taxa or 0.0 for a, _ in pares) / len(pares))
    novo_pp = _pp(sum(b.taxa or 0.0 for _, b in pares) / len(pares))
    margem = margem_total_pp
    if tipo != "total":
        margem = margem_total_pp * math.sqrt(total_comparaveis / len(pares))
    delta = novo_pp - base_pp
    return DeltaRecorte(
        tipo=tipo,
        nome=nome,
        casos=len(pares),
        base_pp=base_pp,
        novo_pp=novo_pp,
        delta_pp=delta,
        margem_pp=margem,
        veredito=_veredito_recorte(delta, margem),
    )


def _formatar_orcamento(orcamento: dict[str, float | int] | None) -> str | None:
    if orcamento is None:
        return None
    return ", ".join(f"{chave}={valor}" for chave, valor in orcamento.items())


def _contexto(base: Relatorio, novo: Relatorio) -> tuple[DiferencaContexto, ...]:
    pares: list[tuple[str, str | None, str | None]] = [
        ("modelo", base.modelo, novo.modelo),
        ("provedores", ", ".join(base.provedores) or None, ", ".join(novo.provedores) or None),
        ("hash do prompt", base.hash_prompt, novo.hash_prompt),
        ("commit", base.commit, novo.commit),
        ("árvore do git suja", _sim_nao(base.commit_sujo), _sim_nao(novo.commit_sujo)),
        ("orçamento", _formatar_orcamento(base.orcamento), _formatar_orcamento(novo.orcamento)),
        ("rodadas por caso", str(base.rodadas_por_caso), str(novo.rodadas_por_caso)),
        ("versão do conjunto", base.versao_conjunto, novo.versao_conjunto),
        ("telemetria", base.telemetria, novo.telemetria),
    ]
    return tuple(
        DiferencaContexto(campo=campo, base=b, novo=n, diferente=b != n) for campo, b, n in pares
    )


def _sim_nao(valor: bool | None) -> str | None:
    if valor is None:
        return None
    return "sim" if valor else "não"


def _iteracoes_medias(relatorio: Relatorio) -> float | None:
    iteracoes = [
        r.iteracoes
        for caso in relatorio.casos
        for r in caso.rodadas
        if r.classificacao in (Classificacao.APROVADA, Classificacao.REPROVADA)
    ]
    return sum(iteracoes) / len(iteracoes) if iteracoes else None


def _diferenca(nome: str, base: float | None, novo: float | None) -> DiferencaValor:
    delta = novo - base if base is not None and novo is not None else None
    return DiferencaValor(nome=nome, base=base, novo=novo, delta=delta)


def _valores(base: Relatorio, novo: Relatorio) -> tuple[DiferencaValor, ...]:
    return (
        _diferenca("custo total (US$)", base.custo_total, novo.custo_total),
        _diferenca(
            "tokens de entrada",
            float(base.tokens_entrada_total),
            float(novo.tokens_entrada_total),
        ),
        _diferenca(
            "tokens de saída", float(base.tokens_saida_total), float(novo.tokens_saida_total)
        ),
        _diferenca(
            "iterações médias por rodada",
            _iteracoes_medias(base),
            _iteracoes_medias(novo),
        ),
    )


def _vazia(
    base: Relatorio,
    novo: Relatorio,
    *,
    piso_pp: float,
    fator_ruido: float,
    limite_caso: float,
    motivo: str,
    avisos: tuple[str, ...],
    contexto: tuple[DiferencaContexto, ...],
    desvio_base_pp: float | None,
    casos_comparados: int = 0,
    fora: tuple[tuple[str, ...], ...] = ((), (), (), ()),
    valores: tuple[DiferencaValor, ...] = (),
) -> Comparacao:
    so_base, so_novo, alterados, sem_taxa = fora
    return Comparacao(
        veredito="incomparavel",
        motivos=(motivo,),
        avisos=avisos,
        semente_base=base.semente,
        semente_novo=novo.semente,
        piso_pp=piso_pp,
        fator_ruido=fator_ruido,
        limite_caso=limite_caso,
        desvio_base_pp=desvio_base_pp,
        casos_comparados=casos_comparados,
        contexto=contexto,
        so_na_base=so_base,
        so_no_novo=so_novo,
        hash_diferente=alterados,
        sem_taxa=sem_taxa,
        valores=valores,
    )


def comparar(
    base: Relatorio,
    novo: Relatorio,
    *,
    piso_pp: float = PISO_PP_PADRAO,
    fator_ruido: float = FATOR_RUIDO_PADRAO,
    limite_caso: float = LIMITE_CASO_PADRAO,
) -> Comparacao:
    """Compara a execução `novo` com a linha de base `base`. Não faz I/O.

    Levanta ValueError se os parâmetros estiverem fora da faixa (piso e fator não negativos e
    finitos; limite entre 0 e 1, sem zero).
    """
    if not math.isfinite(piso_pp) or piso_pp < 0:
        raise ValueError("piso_pp precisa ser um número finito e não negativo")
    if not math.isfinite(fator_ruido) or fator_ruido < 0:
        raise ValueError("fator_ruido precisa ser um número finito e não negativo")
    if not 0 < limite_caso <= 1:
        raise ValueError("limite_caso precisa estar entre 0 (sem zero) e 1")

    avisos: list[str] = []
    contexto = _contexto(base, novo)
    desvio_base_pp = (
        _pp(base.desvio_entre_rodadas) if base.desvio_entre_rodadas is not None else None
    )
    valores = _valores(base, novo)
    if base.versao_conjunto != novo.versao_conjunto:
        avisos.append(
            "as execuções usam versões de conjunto diferentes: os casos são casados pelo hash, "
            "mas confira o que mudou"
        )
    if base.rodadas_por_caso < RODADAS_MINIMAS_CASO or novo.rodadas_por_caso < RODADAS_MINIMAS_CASO:
        avisos.append(
            "uma das execuções tem menos de 3 rodadas por caso: a margem é pouco confiável"
        )

    if base.semente != novo.semente:
        return _vazia(
            base,
            novo,
            piso_pp=piso_pp,
            fator_ruido=fator_ruido,
            limite_caso=limite_caso,
            motivo=(
                f"sementes diferentes ({base.semente} e {novo.semente}): os dados dos casos "
                "não são os mesmos"
            ),
            avisos=tuple(avisos),
            contexto=contexto,
            desvio_base_pp=desvio_base_pp,
            valores=valores,
        )

    mapa_base = {caso.caso_id: caso for caso in base.casos}
    mapa_novo = {caso.caso_id: caso for caso in novo.casos}
    so_base = tuple(cid for cid in mapa_base if cid not in mapa_novo)
    so_novo = tuple(cid for cid in mapa_novo if cid not in mapa_base)
    sem_hash = False
    pares: list[tuple[ResumoCaso, ResumoCaso]] = []
    alterados: list[str] = []
    sem_taxa: list[str] = []
    for cid, caso_base in mapa_base.items():
        caso_novo = mapa_novo.get(cid)
        if caso_novo is None:
            continue
        if caso_base.hash_caso is None or caso_novo.hash_caso is None:
            sem_hash = True
        elif caso_base.hash_caso != caso_novo.hash_caso:
            alterados.append(cid)
            continue
        if caso_base.taxa is None or caso_novo.taxa is None:
            # Pendente não tem taxa por desenho: só entra na lista se algum lado é ativo.
            if caso_base.situacao == "ativo" or caso_novo.situacao == "ativo":
                sem_taxa.append(cid)
            continue
        pares.append((caso_base, caso_novo))

    if sem_hash:
        avisos.append(
            "sem hash por caso em uma das execuções: não dá para garantir que o caso é o mesmo; "
            "a comparação casou por caso_id"
        )
    fora = (so_base, so_novo, tuple(alterados), tuple(sem_taxa))
    if not pares:
        return _vazia(
            base,
            novo,
            piso_pp=piso_pp,
            fator_ruido=fator_ruido,
            limite_caso=limite_caso,
            motivo=(
                "nenhum caso em comum com o mesmo hash e taxa nas duas execuções: "
                "não há o que comparar"
            ),
            avisos=tuple(avisos),
            contexto=contexto,
            desvio_base_pp=desvio_base_pp,
            fora=fora,
            valores=valores,
        )

    margem_total = _margem(piso_pp, fator_ruido, desvio_base_pp)
    if desvio_base_pp is None:
        avisos.append(
            "a linha de base não tem desvio entre rodadas: a margem é só o piso de "
            f"{piso_pp:g} p.p."
        )
    n = len(pares)
    total = _recorte("total", "total", pares, total_comparaveis=n, margem_total_pp=margem_total)

    # Só casos ativos contam para os recortes: pendente não tem taxa e não entra na média.
    categorias: list[DeltaRecorte] = []
    presentes_cat = {c.categoria for c in (*base.casos, *novo.casos) if c.situacao == "ativo"}
    for categoria in Categoria:
        if categoria not in presentes_cat:
            continue
        do_recorte = [(a, b) for a, b in pares if a.categoria is categoria]
        categorias.append(
            _recorte(
                "categoria",
                categoria.value,
                do_recorte,
                total_comparaveis=n,
                margem_total_pp=margem_total,
            )
        )

    grupos: list[DeltaRecorte] = []
    presentes_grupo = {c.grupo for c in (*base.casos, *novo.casos) if c.situacao == "ativo"}
    for grupo in GRUPOS_DE_CASOS:
        if grupo not in presentes_grupo:
            continue
        do_recorte = [(a, b) for a, b in pares if a.grupo == grupo]
        grupos.append(
            _recorte("grupo", grupo, do_recorte, total_comparaveis=n, margem_total_pp=margem_total)
        )

    casos = tuple(
        ComparacaoCaso(
            caso_id=a.caso_id,
            categoria=a.categoria.value,
            grupo=a.grupo,
            base_pp=_pp(a.taxa or 0.0),
            novo_pp=_pp(b.taxa or 0.0),
            delta_pp=_pp((b.taxa or 0.0) - (a.taxa or 0.0)),
            rodadas_base=_validas(a),
            rodadas_novo=_validas(b),
            flag=_flag(a, b, limite_caso),
        )
        for a, b in pares
    )

    motivos: list[str] = []
    regressoes = [
        d
        for d in (total, *(g for g in grupos if g.nome == "reservado"))
        if d.veredito == "regressao_provavel"
    ]
    if regressoes:
        veredito: VereditoGeral = "regressao_provavel"
        for recorte in regressoes:
            nome = "a taxa total" if recorte.tipo == "total" else "o grupo reservado"
            motivos.append(
                f"{nome} caiu {_fmt(-(recorte.delta_pp or 0.0))} p.p., acima da margem de "
                f"{_fmt(recorte.margem_pp or 0.0)} p.p.: regressão provável"
            )
    else:
        veredito = "ok"
        motivos.append("nenhuma queda acima da margem no total nem no grupo reservado.")
        if total.veredito == "melhorou":
            motivos.append(
                f"a taxa total subiu {_fmt(total.delta_pp or 0.0)} p.p., acima da margem."
            )

    return Comparacao(
        veredito=veredito,
        motivos=tuple(motivos),
        avisos=tuple(avisos),
        semente_base=base.semente,
        semente_novo=novo.semente,
        piso_pp=piso_pp,
        fator_ruido=fator_ruido,
        limite_caso=limite_caso,
        desvio_base_pp=desvio_base_pp,
        casos_comparados=n,
        contexto=contexto,
        total=total,
        categorias=tuple(categorias),
        grupos=tuple(grupos),
        casos=casos,
        so_na_base=so_base,
        so_no_novo=so_novo,
        hash_diferente=tuple(alterados),
        sem_taxa=tuple(sem_taxa),
        valores=valores,
    )


def _fmt(valor: float) -> str:
    return f"{valor:.1f}".replace(".", ",")


_VEREDITO_GERAL: Final = {
    "ok": "OK",
    "regressao_provavel": "REGRESSÃO PROVÁVEL",
    "incomparavel": "INCOMPARÁVEL",
}
_VEREDITO_RECORTE: Final = {
    "melhorou": "melhorou",
    "regressao_provavel": "regressão provável",
    "dentro_do_ruido": "dentro do ruído",
    "sem_dados": "sem dados",
}


def _br(valor: float, casas: int, *, sinal: bool = False) -> str:
    """Número em formato brasileiro: milhar com ponto, decimal com vírgula."""
    especificador = "+" if sinal else ""
    texto = f"{valor:{especificador},.{casas}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _celula(valor: float | None, *, sinal: bool = False) -> str:
    if valor is None:
        return "-"
    return _br(valor, 1, sinal=sinal)


def _linha_recorte(rotulo: str, recorte: DeltaRecorte) -> str:
    return (
        f"{rotulo:<26}{recorte.casos:>6}{_celula(recorte.base_pp):>9}"
        f"{_celula(recorte.novo_pp):>9}{_celula(recorte.delta_pp, sinal=True):>9}"
        f"{_celula(recorte.margem_pp):>9}  {_VEREDITO_RECORTE[recorte.veredito]}"
    )


def _linhas_contexto(comparacao: Comparacao) -> list[str]:
    linhas: list[str] = []
    for item in comparacao.contexto:
        if not item.diferente:
            continue
        base = item.base if item.base is not None else "não informado"
        novo = item.novo if item.novo is not None else "não informado"
        linhas.append(f"  {item.campo}: {base} -> {novo}")
    if not linhas:
        linhas.append("  o contexto é o mesmo nas duas execuções")
    for valor in comparacao.valores:
        if valor.base is None or valor.novo is None:
            linhas.append(f"  {valor.nome}: não informado em uma das execuções")
            continue
        casas = 4 if "custo" in valor.nome else 0 if "tokens" in valor.nome else 1
        delta = _br(valor.delta or 0.0, casas, sinal=True)
        linhas.append(
            f"  {valor.nome}: base {_br(valor.base, casas)} -> novo {_br(valor.novo, casas)} "
            f"(delta {delta})"
        )
    return linhas


def _linhas_casos(comparacao: Comparacao) -> list[str]:
    if not comparacao.casos:
        return ["  nenhum caso comparável"]
    linhas = [f"  {'Caso':<16}{'Grupo':<16}{'Base':>7}{'Novo':>7}{'Delta':>8}  Flag"]
    for caso in comparacao.casos:
        linhas.append(
            f"  {caso.caso_id:<16}{caso.grupo:<16}{_celula(caso.base_pp):>7}"
            f"{_celula(caso.novo_pp):>7}{_celula(caso.delta_pp, sinal=True):>8}  {caso.flag}"
        )
    return linhas


def _linhas_fora(comparacao: Comparacao) -> list[str]:
    grupos = (
        ("só na base", comparacao.so_na_base),
        ("só no novo", comparacao.so_no_novo),
        ("hash diferente (caso alterado)", comparacao.hash_diferente),
        ("sem taxa", comparacao.sem_taxa),
    )
    return [f"  {rotulo}: {', '.join(ids)}" for rotulo, ids in grupos if ids]


def formatar_texto(comparacao: Comparacao) -> str:
    """Relatório em português: veredito, avisos, o que mudou, taxas, casos e fora da comparação."""
    linhas: list[str] = [
        "Comparação com a linha de base",
        f"Base: semente {comparacao.semente_base}. Novo: semente {comparacao.semente_novo}.",
        "",
        f"Veredito: {_VEREDITO_GERAL[comparacao.veredito]}",
        *(f"  {motivo}" for motivo in comparacao.motivos),
    ]
    if comparacao.avisos:
        linhas += ["", "Avisos:", *(f"  - {aviso}" for aviso in comparacao.avisos)]
    linhas += ["", "O que mudou entre as execuções:", *_linhas_contexto(comparacao)]
    if comparacao.total is not None:
        linhas += [
            "",
            f"Taxas em pontos percentuais, sobre {comparacao.casos_comparados} caso(s) em comum:",
            f"{'Recorte':<26}{'Casos':>6}{'Base':>9}{'Novo':>9}{'Delta':>9}{'Margem':>9}  Veredito",
            _linha_recorte("total", comparacao.total),
            *(_linha_recorte(f"categoria: {c.nome}", c) for c in comparacao.categorias),
            *(_linha_recorte(f"grupo: {g.nome}", g) for g in comparacao.grupos),
            "",
            "Casos (indicativo: piorou ou melhorou quando a queda é de pelo menos "
            f"{_br(comparacao.limite_caso * 100, 1)} p.p., com 3 rodadas válidas dos dois lados):",
            *_linhas_casos(comparacao),
        ]
    fora = _linhas_fora(comparacao)
    if fora:
        linhas += ["", "Fora da comparação:", *fora]
    return "\n".join(linhas) + "\n"


def formatar_json(comparacao: Comparacao) -> str:
    """A comparação inteira em JSON, com acentos sem escape."""
    return comparacao.model_dump_json(indent=2) + "\n"
