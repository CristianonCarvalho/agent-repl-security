"""Política da lethal trifecta e rastreador de arquivos abertos (Fase 4, peça 1)."""

from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pytest

from harness.host.politica import Decisao, Exposicao, RastreadorDeArquivos, avaliar


@dataclass(frozen=True)
class Ferr:
    dado_privado: bool = False
    conteudo_nao_confiavel: bool = False
    canal_externo: bool = False


@pytest.mark.parametrize(
    ("privado", "nao_confiavel", "canal"), list(product([False, True], repeat=3))
)
def test_avaliar_para_as_oito_combinacoes_da_exposicao(
    privado: bool, nao_confiavel: bool, canal: bool
) -> None:
    exposicao = Exposicao(viu_dado_privado=privado, viu_nao_confiavel=nao_confiavel)
    decisao = avaliar(exposicao, Ferr(canal_externo=canal))
    esperada = (
        Decisao.PEDIR_APROVACAO if (privado and nao_confiavel and canal) else Decisao.PERMITIR
    )
    assert decisao is esperada


def test_a_propria_ferramenta_conta_para_o_estado_efetivo() -> None:
    # Sessão só viu dado privado; a ferramenta é não confiável e com canal externo (MCP).
    exposicao = Exposicao(viu_dado_privado=True)
    ferramenta = Ferr(conteudo_nao_confiavel=True, canal_externo=True)
    assert avaliar(exposicao, ferramenta) is Decisao.PEDIR_APROVACAO


def test_sessao_limpa_e_ferramenta_externa_sem_dado_privado_passa() -> None:
    assert avaliar(Exposicao(), Ferr(canal_externo=True)) is Decisao.PERMITIR


def test_marcar_arquivo_confiavel_so_marca_dado_privado() -> None:
    exposicao = Exposicao()
    exposicao.marcar_arquivo("confiavel")
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (True, False)


def test_marcar_arquivo_externo_marca_os_dois() -> None:
    exposicao = Exposicao()
    exposicao.marcar_arquivo("externo")
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (True, True)


def test_marcar_ferramenta_e_zerar() -> None:
    exposicao = Exposicao()
    exposicao.marcar_ferramenta(Ferr(dado_privado=True, conteudo_nao_confiavel=True))
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (True, True)
    exposicao.zerar()
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (False, False)


def _rastreador(
    tmp_path: Path, manifesto: str | None = None
) -> tuple[RastreadorDeArquivos, Exposicao]:
    if manifesto is not None:
        (tmp_path / "manifesto.json").write_text(manifesto, encoding="utf-8")
    exposicao = Exposicao()
    return RastreadorDeArquivos(tmp_path, exposicao), exposicao


def test_rastreador_arquivo_confiavel(tmp_path: Path) -> None:
    rastreador, exposicao = _rastreador(tmp_path)
    assert rastreador("/dados/vendas/a.csv") is None
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (True, False)


def test_rastreador_arquivo_externo(tmp_path: Path) -> None:
    rastreador, exposicao = _rastreador(tmp_path, '{"vendas/a.csv": "externo"}')
    assert rastreador("/dados/vendas/a.csv") is None
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (True, True)


def test_rastreador_manifesto_malformado_conta_como_nao_confiavel(tmp_path: Path) -> None:
    rastreador, exposicao = _rastreador(tmp_path, "isto não é json")
    assert rastreador("/dados/vendas/a.csv") is None
    assert exposicao.viu_nao_confiavel is True


@pytest.mark.parametrize(
    "caminho",
    ["/dados/../etc/passwd", "/dados//vendas/a.csv", "/etc/passwd", "dados/a.csv", "/dados/"],
)
def test_rastreador_recusa_caminho_fora_do_formato(tmp_path: Path, caminho: str) -> None:
    rastreador, exposicao = _rastreador(tmp_path)
    assert isinstance(rastreador(caminho), str)
    assert (exposicao.viu_dado_privado, exposicao.viu_nao_confiavel) == (False, False)
