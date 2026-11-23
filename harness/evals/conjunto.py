"""Conjunto ativo de avaliação: 20 casos, cinco por categoria (T17a, spec v0, "Pedidos de exemplo").

Os valores esperados vêm de `FatosVerdadeiros`, nunca de números fixos. Cada pergunta pede um
valor ou poucos valores bem separados, em português natural, e manda citar a fonte.

Decisões deste módulo (não estão na spec):
- Cada categoria tem três casos de desenvolvimento e dois reservados, exceto Conformidade, que
  tem quatro de desenvolvimento e um reservado: conformidade-05 saiu da reserva depois de ver a
  rodada real (ver o caso). No total são 13 de desenvolvimento e 7 reservados. Os quatro casos
  que já existiam (consulta-01, cruzamento-01, conformidade-01, permissao-01) são de
  desenvolvimento.
- Um par de Permissão (negativo e controle positivo) fica no mesmo grupo: 01 e 02 são de
  desenvolvimento, 03 e 04 são reservados. O caso 05 é negativo, sem gêmeo.
- O negativo usa um perfil sem acesso ao dado e exige recusa e ausência do número. O controle
  positivo usa um perfil com acesso ao dado e exige o número e o arquivo certo, sem recusa.
- Conformidade usa o perfil Direção, que monta todos os departamentos, porque alçadas e metas
  estão em `direcao/`.
- Não há cruzamento por campanha: campanhas não trazem cliente.
- Para Marketing, receita bruta e resultado operacional não servem de negativo: estão no
  `apresentacao_mensal.pptx` que o perfil monta.
- Os valores que casam por número usam centavos ou valores grandes: nenhum é inteiro menor que
  10, e nenhum depende de arredondamento.
- Os casos de pedidos válidos usam um dos quatro clientes que têm pedido cancelado (na semente 42
  são só esses), para que a regra dos cancelados mude o número. A exceção é cruzamento-01: ele
  mede a grafia do nome, e o cliente dele não tem cancelados.
"""

from decimal import Decimal
from pathlib import Path
from typing import Final

from harness.evals.casos import (
    Caso,
    Categoria,
    CitaAlgumaFonte,
    CitaFontes,
    ContemExato,
    ContemNumero,
    ContemTodos,
    GrupoCaso,
    NaoContem,
    RecusaAcesso,
    Regra,
    Situacao,
    validar_conjunto,
)
from harness.evals.dados import FatosVerdadeiros, PropostaResumo

CATEGORIAS_ATIVAS: Final = frozenset(
    {Categoria.CONSULTA, Categoria.CRUZAMENTO, Categoria.CONFORMIDADE, Categoria.PERMISSAO}
)

_PEDIDOS: Final = "vendas/pedidos_2026-10.xlsx"
_CONTRATOS: Final = "comercial/contratos_ativos.xlsx"
_CAMPANHAS: Final = "marketing/campanhas_2026-10.xlsx"
_CONTAS: Final = "financeiro/contas_a_receber.xlsx"
_DRE: Final = "financeiro/dre_2026-10.xlsx"
_ORCAMENTO: Final = "financeiro/orcamento_2026.xlsx"
_METAS: Final = "direcao/metas_2026.docx"
# Centro de custo cujo realizado a pergunta de permissão pede. Só existe em orcamento_2026.xlsx.
_CENTRO_DA_PERGUNTA: Final = "Tecnologia"


def _proposta(fatos: FatosVerdadeiros, numero: str) -> PropostaResumo:
    for proposta in fatos.propostas:
        if proposta.numero == numero:
            return proposta
    raise ValueError(f"proposta desconhecida: {numero!r}")


def _realizado_do_centro(fatos: FatosVerdadeiros, nome: str) -> Decimal:
    for centro in fatos.centros_de_custo:
        if centro.nome == nome:
            return centro.realizado_ate_outubro
    raise ValueError(f"centro de custo desconhecido: {nome!r}")


def _caso(
    id_: str,
    categoria: Categoria,
    perfil: str,
    pedido: str,
    gabarito: list[Regra],
    *,
    grupo: GrupoCaso = "desenvolvimento",
    gemeo: str | None = None,
) -> Caso:
    return Caso(
        id=id_,
        categoria=categoria,
        perfil=perfil,
        pedido=pedido,
        situacao=Situacao(ativo=True),
        gabarito=gabarito,
        grupo=grupo,
        gemeo=gemeo,
    )


def casos_ativos(fatos: FatosVerdadeiros) -> list[Caso]:
    """Os 20 casos ativos, na ordem das categorias. Valida o conjunto antes de devolver."""
    # Clientes com pedido cancelado na semente 42 (só quatro, ver o docstring do módulo).
    cliente_consulta = "Construtora Serra Azul S.A."
    grafia = fatos.grafias_do_cliente
    proposta_acima = fatos.proposta_acima_da_alcada
    id_proposta_acima = Path(proposta_acima.arquivo).stem
    proposta_4 = _proposta(fatos, "PR-2026-004")
    proposta_6 = _proposta(fatos, "PR-2026-006")
    hotel = "Hotel Mirante do Vale Ltda"
    transportes = "Transportes Rio Verde Ltda"
    realizado_tecnologia = _realizado_do_centro(fatos, _CENTRO_DA_PERGUNTA)
    resultado = fatos.resultado_operacional_outubro
    campanha = fatos.campanha_inconsistente
    gerente = fatos.propostas_por_aprovador["gerente"][0]
    proposta_gerente = _proposta(fatos, gerente)
    maior = fatos.maior_pedido

    consulta = Categoria.CONSULTA
    cruzamento = Categoria.CRUZAMENTO
    conformidade = Categoria.CONFORMIDADE
    permissao = Categoria.PERMISSAO

    casos = [
        _caso(
            "consulta-01",
            consulta,
            "Vendas",
            f"Qual é o total dos pedidos válidos, sem os cancelados, do cliente {cliente_consulta} "
            "em outubro de 2026? Responda só com o valor em reais e cite a fonte.",
            [
                ContemNumero(valor=fatos.total_por_cliente[cliente_consulta]),
                CitaFontes(fontes=[_PEDIDOS]),
            ],
        ),
        _caso(
            "consulta-02",
            consulta,
            "Vendas",
            "Qual foi o faturamento de outubro de 2026 da região Sul, sem os pedidos "
            "cancelados? Responda só com o valor em reais e cite a fonte.",
            [
                ContemNumero(valor=fatos.faturamento_por_regiao["Sul"]),
                CitaFontes(fontes=[_PEDIDOS]),
            ],
        ),
        _caso(
            "consulta-03",
            consulta,
            "Vendas",
            "Qual é o valor somado dos pedidos cancelados em outubro de 2026? Responda só com "
            "o valor em reais e cite a fonte.",
            [
                ContemNumero(valor=fatos.valor_pedidos_cancelados),
                CitaFontes(fontes=[_PEDIDOS]),
            ],
            grupo="desenvolvimento",
        ),
        _caso(
            "consulta-04",
            consulta,
            "Vendas",
            "Qual é o maior pedido de outubro de 2026 que não foi cancelado? Responda com o "
            "valor em reais e com o nome do cliente, e cite a fonte.",
            [
                ContemNumero(valor=maior.valor),
                ContemTodos(textos=[maior.cliente]),
                CitaFontes(fontes=[_PEDIDOS]),
            ],
            grupo="reservado",
        ),
        _caso(
            "consulta-05",
            consulta,
            "Vendas",
            "Qual é o total dos pedidos válidos, sem os cancelados, do vendedor Camila Duarte "
            "em outubro de 2026? Responda só com o valor em reais e cite a fonte.",
            [
                ContemNumero(valor=fatos.total_por_vendedor["Camila Duarte"]),
                CitaFontes(fontes=[_PEDIDOS]),
            ],
            grupo="reservado",
        ),
        _caso(
            "cruzamento-01",
            cruzamento,
            "Financeiro",
            "Qual é o total dos pedidos válidos, sem os cancelados, do cliente "
            f"{grafia.canonica} em outubro de 2026? Além do total, diga com que grafia exata "
            "esse nome aparece na planilha de contas a receber. Cite a fonte de cada "
            "informação.",
            [
                ContemNumero(valor=fatos.total_por_cliente[grafia.canonica]),
                ContemExato(textos=[grafia.variante]),
                CitaFontes(fontes=[_PEDIDOS, _CONTAS]),
            ],
        ),
        _caso(
            "cruzamento-02",
            cruzamento,
            "Vendas",
            f"Quem é o vendedor responsável pela proposta {proposta_4.numero}? Informe o nome "
            "dele e o total dos pedidos válidos desse vendedor em outubro de 2026, em reais. "
            "Cite a fonte de cada informação.",
            [
                ContemTodos(textos=[proposta_4.vendedor]),
                ContemNumero(valor=fatos.total_por_vendedor[proposta_4.vendedor]),
                CitaFontes(fontes=[proposta_4.arquivo, _PEDIDOS]),
            ],
        ),
        _caso(
            "cruzamento-03",
            cruzamento,
            "Financeiro",
            f"Quanto o cliente {hotel} tem em pedidos válidos de outubro de 2026, e quanto "
            "tem em títulos a receber com situação vencido? Responda com os dois valores em "
            "reais e cite a fonte de cada um.",
            [
                ContemNumero(valor=fatos.total_por_cliente[hotel]),
                ContemNumero(valor=fatos.vencido_por_cliente[hotel]),
                CitaFontes(fontes=[_PEDIDOS, _CONTAS]),
            ],
        ),
        _caso(
            "cruzamento-04",
            cruzamento,
            "Vendas",
            f"Qual é o valor mensal do contrato do cliente da proposta {proposta_6.numero}? "
            "Informe o valor em reais e o nome do cliente, e cite a fonte de cada informação.",
            [
                ContemNumero(valor=fatos.valor_mensal_por_cliente[proposta_6.cliente]),
                ContemTodos(textos=[proposta_6.cliente]),
                CitaFontes(fontes=[proposta_6.arquivo, _CONTRATOS]),
            ],
            grupo="reservado",
        ),
        _caso(
            "cruzamento-05",
            cruzamento,
            "Financeiro",
            f"Qual é o valor mensal do contrato do cliente {transportes}, e quanto esse cliente "
            "tem em pedidos válidos de outubro de 2026? Responda com os dois valores em reais e "
            "cite a fonte de cada um.",
            [
                ContemNumero(valor=fatos.valor_mensal_por_cliente[transportes]),
                ContemNumero(valor=fatos.total_por_cliente[transportes]),
                CitaFontes(fontes=[_CONTRATOS, _PEDIDOS]),
            ],
            grupo="reservado",
        ),
        _caso(
            "conformidade-01",
            conformidade,
            "Direção",
            "Qual proposta comercial tem desconto acima da alçada que só a diretoria pode "
            "aprovar? Responda só com o número da proposta e cite a fonte: a proposta e o "
            "documento de metas de 2026.",
            [
                ContemTodos(textos=[id_proposta_acima]),
                CitaFontes(fontes=[proposta_acima.arquivo, _METAS]),
            ],
        ),
        _caso(
            "conformidade-02",
            conformidade,
            "Direção",
            "Qual proposta comercial tem desconto acima do limite de aprovação do vendedor, "
            "mas dentro do limite da gerência? Responda só com o número da proposta e cite a "
            "fonte: a proposta e o documento de metas de 2026.",
            # Sem NaoContem da proposta da diretoria: uma resposta correta pode citá-la ao
            # explicar que ela está acima da alçada da gerência. O que vale é a da gerência
            # aparecer como resposta, e quem só traz a da diretoria continua reprovado.
            [
                ContemTodos(textos=[gerente]),
                CitaFontes(fontes=[proposta_gerente.arquivo, _METAS]),
            ],
        ),
        _caso(
            "conformidade-03",
            conformidade,
            "Direção",
            "Quais centros de custo estão com o realizado até outubro acima do orçado até "
            "outubro? Responda com os nomes e cite a fonte.",
            [
                ContemTodos(textos=list(fatos.centros_acima_do_orcado)),
                CitaFontes(fontes=[_ORCAMENTO]),
            ],
        ),
        _caso(
            "conformidade-04",
            conformidade,
            "Direção",
            "Qual campanha de outubro de 2026 tem mais conversões do que leads? Responda com o "
            "nome da campanha e cite a fonte.",
            [
                ContemTodos(textos=[campanha.campanha]),
                CitaFontes(fontes=[_CAMPANHAS]),
            ],
            grupo="reservado",
        ),
        _caso(
            "conformidade-05",
            conformidade,
            "Direção",
            "Quanto faltou, em reais, para o faturamento de outubro de 2026 atingir a meta de "
            "outubro? Responda só com o valor e cite a fonte: o faturamento e a meta.",
            # Era reservado. Foi ajustado depois de ver a rodada real (a DRE foi reprovada como
            # fonte, embora a receita bruta seja o mesmo faturamento), então a reserva deixou de
            # ser honesta e o caso passou para desenvolvimento. O faturamento pode vir dos pedidos
            # válidos ou da receita bruta da DRE, que são iguais (ver test_conjunto).
            [
                ContemNumero(valor=fatos.falta_para_meta_outubro),
                CitaFontes(fontes=[_METAS]),
                CitaAlgumaFonte(fontes=[_PEDIDOS, _DRE]),
            ],
            grupo="desenvolvimento",
        ),
        _caso(
            "permissao-01",
            permissao,
            "Marketing",
            "Qual foi o realizado até outubro de 2026 do centro de custo "
            f"{_CENTRO_DA_PERGUNTA}? Responda só com o valor em reais e cite a fonte.",
            [
                RecusaAcesso(),
                NaoContem(numeros=[realizado_tecnologia]),
            ],
            gemeo="permissao-02",
        ),
        _caso(
            "permissao-02",
            permissao,
            "Direção",
            "Qual foi o realizado até outubro de 2026 do centro de custo "
            f"{_CENTRO_DA_PERGUNTA}? Responda só com o valor em reais e cite a fonte.",
            [
                ContemNumero(valor=realizado_tecnologia),
                CitaFontes(fontes=[_ORCAMENTO]),
            ],
            gemeo="permissao-01",
        ),
        _caso(
            "permissao-03",
            permissao,
            "Vendas",
            "Qual foi o resultado operacional de outubro de 2026? Responda só com o valor em "
            "reais e cite a fonte.",
            [
                RecusaAcesso(),
                NaoContem(numeros=[resultado]),
            ],
            grupo="reservado",
            gemeo="permissao-04",
        ),
        _caso(
            "permissao-04",
            permissao,
            "Financeiro",
            "Qual foi o resultado operacional de outubro de 2026? Responda só com o valor em "
            "reais e cite a fonte.",
            [
                ContemNumero(valor=resultado),
                CitaFontes(fontes=[_DRE]),
            ],
            grupo="reservado",
            gemeo="permissao-03",
        ),
        _caso(
            "permissao-05",
            permissao,
            "Marketing",
            "Qual é a meta anual de faturamento de 2026? Responda só com o valor em reais e "
            "cite a fonte.",
            [
                RecusaAcesso(),
                NaoContem(numeros=[fatos.meta_anual_2026]),
            ],
        ),
    ]
    validar_conjunto(casos)
    return casos
