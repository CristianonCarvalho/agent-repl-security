"""Testes da descrição do contexto (tipo, tamanho, prévia e hash), sem Docker e sem o REPL."""

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from harness.sandbox.contexto import (
    LIMITE_PREVIA,
    MAXIMO_CHAVES_PREVIA,
    DescricaoContexto,
    bytes_do_contexto,
    descrever_contexto,
    sha256_contexto,
    tipo_json,
)


def test_texto_tem_tamanho_em_caracteres_e_previa_dos_primeiros_200() -> None:
    descricao = descrever_contexto("á" * 500)
    assert descricao == DescricaoContexto(tipo="texto", tamanho=500, previa="á" * LIMITE_PREVIA)


def test_texto_curto_tem_previa_inteira() -> None:
    assert descrever_contexto("linha 1\nlinha 2").previa == "linha 1\nlinha 2"


def test_texto_vazio() -> None:
    assert descrever_contexto("") == DescricaoContexto(tipo="texto", tamanho=0, previa="")


@given(st.text())
def test_previa_do_texto_e_prefixo_e_tamanho_e_o_comprimento(texto: str) -> None:
    descricao = descrever_contexto(texto)
    assert texto.startswith(descricao.previa)
    assert len(descricao.previa) <= LIMITE_PREVIA
    assert descricao.tamanho == len(texto)


def test_objeto_conta_chaves_e_mostra_o_tipo_de_cada_valor() -> None:
    valor = {"nome": "Acme", "total": 10.5, "ativo": True, "itens": [1], "extra": None, "d": {}}
    assert descrever_contexto(valor) == DescricaoContexto(
        tipo="objeto",
        tamanho=6,
        previa="nome: texto, total: numero, ativo: booleano, itens: lista, extra: nulo, d: objeto",
    )


def test_objeto_considera_no_maximo_vinte_chaves_de_topo() -> None:
    valor = {f"c{i:02d}": i for i in range(30)}
    descricao = descrever_contexto(valor)
    assert descricao.tamanho == 30
    assert "c00" in descricao.previa
    assert "c20" not in descricao.previa
    assert MAXIMO_CHAVES_PREVIA == 20


def test_objeto_com_chave_enorme_respeita_o_limite_da_previa() -> None:
    assert len(descrever_contexto({"k" * 1000: 1}).previa) <= LIMITE_PREVIA


def test_objeto_vazio() -> None:
    assert descrever_contexto({}) == DescricaoContexto(tipo="objeto", tamanho=0, previa="")


def test_lista_conta_itens_e_resume_o_primeiro() -> None:
    assert descrever_contexto([{"a": 1}, 2, 3]) == DescricaoContexto(
        tipo="lista", tamanho=3, previa='primeiro item: objeto; {"a": 1}'
    )


def test_lista_com_primeiro_item_grande_corta_o_resumo() -> None:
    descricao = descrever_contexto(["x" * 1000])
    assert descricao.tamanho == 1
    assert descricao.previa.startswith('primeiro item: texto; "xxx')
    assert len(descricao.previa) <= LIMITE_PREVIA


def test_lista_vazia() -> None:
    assert descrever_contexto([]) == DescricaoContexto(tipo="lista", tamanho=0, previa="")


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        (True, "booleano"),
        (False, "booleano"),
        (1, "numero"),
        (1.5, "numero"),
        (None, "nulo"),
        ("x", "texto"),
        ({}, "objeto"),
        ([], "lista"),
    ],
)
def test_tipo_json_distingue_booleano_de_numero(valor: object, esperado: str) -> None:
    assert tipo_json(valor) == esperado


def test_sha256_e_do_json_canonico_com_chaves_ordenadas() -> None:
    a = {"b": 1, "a": "ção"}
    b = {"a": "ção", "b": 1}
    assert sha256_contexto(a) == sha256_contexto(b)
    canonico = json.dumps(a, sort_keys=True, ensure_ascii=False).encode("utf-8")
    assert sha256_contexto(a) == hashlib.sha256(canonico).hexdigest()


def test_bytes_do_contexto_mede_o_json_em_utf8() -> None:
    # '"é"' ocupa 4 bytes: duas aspas e o 'é' em UTF-8 com dois bytes.
    assert bytes_do_contexto("é") == 4
