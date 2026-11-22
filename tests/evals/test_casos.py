"""Testes do formato dos casos de avaliação (harness/evals/casos.py).

Cobre situação, ids, perfis, regras do gabarito, unicidade do conjunto e a ida e volta em JSON.
Não usam Docker nem rede.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from harness.evals.casos import (
    Caso,
    Categoria,
    CitaFontes,
    ContemAlgum,
    ContemExato,
    ContemNumero,
    ContemTodos,
    NaoContem,
    RecusaAcesso,
    Situacao,
    casos_de_json,
    casos_para_json,
    formatar_numero_br,
    validar_conjunto,
)


def _caso(**campos: object) -> Caso:
    """Caso válido por padrão. Os campos passados substituem os padrões."""
    base: dict[str, object] = {
        "id": "consulta-01",
        "categoria": Categoria.CONSULTA,
        "perfil": "Direção",
        "pedido": "Qual foi o faturamento de outubro?",
        "situacao": Situacao(ativo=True),
        "gabarito": [ContemNumero(valor=Decimal("1234.56"))],
    }
    base.update(campos)
    return Caso.model_validate(base)


# --- situação ---------------------------------------------------------------------------------


def test_situacao_ativa_sem_motivo_e_valida() -> None:
    situacao = Situacao(ativo=True)

    assert situacao.motivo_pendente is None
    assert situacao.pendente is False


def test_situacao_pendente_com_motivo_e_valida() -> None:
    situacao = Situacao(ativo=False, motivo_pendente="gabarito depende da T16")

    assert situacao.pendente is True


@pytest.mark.parametrize("motivo", [None, "", "   "])
def test_situacao_pendente_exige_motivo(motivo: str | None) -> None:
    with pytest.raises(ValidationError):
        Situacao(ativo=False, motivo_pendente=motivo)


def test_situacao_ativa_nao_aceita_motivo() -> None:
    with pytest.raises(ValidationError):
        Situacao(ativo=True, motivo_pendente="sem razão para estar pendente")


# --- campos do caso ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "identificador", ["consulta-1", "consulta01", "Consulta-01", "consulta-001"]
)
def test_id_fora_do_formato_e_rejeitado(identificador: str) -> None:
    with pytest.raises(ValidationError):
        _caso(id=identificador)


def test_id_com_prefixo_de_outra_categoria_e_rejeitado() -> None:
    with pytest.raises(ValidationError):
        _caso(id="injecao-01", categoria=Categoria.CONSULTA)


def test_perfil_inexistente_e_rejeitado() -> None:
    with pytest.raises(ValidationError):
        _caso(perfil="Compras")


def test_perfil_com_caixa_diferente_e_rejeitado() -> None:
    # A validação é pelo nome exato dos perfis embutidos.
    with pytest.raises(ValidationError):
        _caso(perfil="marketing")


@pytest.mark.parametrize("pedido", ["", "   "])
def test_pedido_vazio_e_rejeitado(pedido: str) -> None:
    with pytest.raises(ValidationError):
        _caso(pedido=pedido)


def test_gabarito_vazio_e_rejeitado() -> None:
    with pytest.raises(ValidationError):
        _caso(gabarito=[])


def test_regra_sem_tipo_valido_e_rejeitada_na_leitura() -> None:
    dados = {
        "id": "consulta-01",
        "categoria": "consulta",
        "perfil": "Direção",
        "pedido": "Qual o total?",
        "situacao": {"ativo": True},
        "gabarito": [{"tipo": "tipo_que_nao_existe"}],
    }
    with pytest.raises(ValidationError):
        Caso.model_validate(dados)


def test_regra_vazia_e_rejeitada() -> None:
    with pytest.raises(ValueError):
        ContemTodos(textos=["   "])
    with pytest.raises(ValueError):
        ContemExato(textos=["   "])
    with pytest.raises(ValueError):
        ContemExato(textos=[])
    with pytest.raises(ValueError):
        ContemAlgum(textos=[])
    with pytest.raises(ValueError):
        NaoContem()
    with pytest.raises(ValueError):
        CitaFontes(fontes=[])


def test_numero_nao_finito_e_rejeitado() -> None:
    with pytest.raises(ValueError):
        ContemNumero(valor=Decimal("NaN"))
    with pytest.raises(ValueError):
        NaoContem(numeros=[Decimal("Infinity")])


def test_tolerancia_negativa_e_rejeitada() -> None:
    with pytest.raises(ValueError):
        ContemNumero(valor=Decimal("1"), tolerancia=Decimal("-0.01"))


def test_caso_e_congelado() -> None:
    caso = _caso()

    with pytest.raises(ValidationError):
        caso.pedido = "outro pedido"


# --- conjunto ---------------------------------------------------------------------------------


def test_validar_conjunto_aceita_ids_distintos() -> None:
    validar_conjunto([_caso(id="consulta-01"), _caso(id="consulta-02")])


def test_validar_conjunto_rejeita_id_duplicado() -> None:
    with pytest.raises(ValueError, match="consulta-01"):
        validar_conjunto([_caso(id="consulta-01"), _caso(id="consulta-01")])


# --- descrições e formato numérico -------------------------------------------------------------


@pytest.mark.parametrize(
    ("valor", "texto"),
    [
        (Decimal("1234.50"), "1.234,5"),
        (Decimal("1234"), "1.234"),
        (Decimal("0.5"), "0,5"),
        (Decimal("-1500.00"), "-1.500"),
        (Decimal("1000000"), "1.000.000"),
        (Decimal("1E+3"), "1.000"),
    ],
)
def test_formatar_numero_br(valor: Decimal, texto: str) -> None:
    assert formatar_numero_br(valor) == texto


def test_descrever_regras_sao_curtas_e_legiveis() -> None:
    assert ContemNumero(valor=Decimal("1234.56"), unidade="valor").descrever() == (
        "resposta traz R$ 1.234,56 (tolerância 0,01)"
    )
    assert (
        ContemNumero(valor=Decimal("18"), unidade="percentual")
        .descrever()
        .startswith("resposta traz 18%")
    )
    assert "'Mercado Alvorada'" in ContemTodos(textos=["Mercado Alvorada"]).descrever()
    assert RecusaAcesso().descrever() == "resposta recusa o acesso ao dado"
    assert "na grafia exata 'MERCADO ALVORADA LTDA.'" in (
        ContemExato(textos=["MERCADO ALVORADA LTDA."]).descrever()
    )


def test_descricao_de_textos_longos_e_truncada() -> None:
    longa = ContemAlgum(textos=["x" * 50, "y" * 50, "z" * 50])

    assert len(longa.descrever()) < 150


# --- serialização -----------------------------------------------------------------------------


def _conjunto_de_exemplo() -> list[Caso]:
    return [
        _caso(
            id="consulta-01",
            categoria=Categoria.CONSULTA,
            gabarito=[
                ContemNumero(valor=Decimal("233223.52")),
                CitaFontes(fontes=["pedidos_2026-10.xlsx"]),
            ],
        ),
        _caso(
            id="cruzamento-01",
            categoria=Categoria.CRUZAMENTO,
            situacao=Situacao(ativo=False, motivo_pendente="aguarda revisão da T16"),
            gabarito=[
                ContemTodos(textos=["Mercado Alvorada Ltda"]),
                ContemExato(textos=["MERCADO ALVORADA LTDA."]),
            ],
        ),
        _caso(
            id="permissao-01",
            categoria=Categoria.PERMISSAO,
            perfil="Marketing",
            gabarito=[
                RecusaAcesso(),
                NaoContem(numeros=[Decimal("294989.98")], textos=["resultado operacional"]),
            ],
        ),
    ]


def test_json_ida_e_volta_sem_perda() -> None:
    casos = _conjunto_de_exemplo()

    texto = casos_para_json(casos)

    assert casos_de_json(texto) == casos


def test_json_preserva_acentos_e_valores_monetarios() -> None:
    texto = casos_para_json(_conjunto_de_exemplo())

    assert "Alvorada" in texto
    assert "233223.52" in texto
    assert "\\u" not in texto


def test_json_guarda_a_regra_exata_com_seu_tipo() -> None:
    texto = casos_para_json(_conjunto_de_exemplo())

    assert '"tipo": "contem_exato"' in texto
    assert "MERCADO ALVORADA LTDA." in texto


def test_json_com_id_duplicado_e_rejeitado() -> None:
    casos = [_caso(id="consulta-01"), _caso(id="consulta-01")]
    texto = casos_para_json(casos)

    with pytest.raises(ValueError, match="duplicados"):
        casos_de_json(texto)
