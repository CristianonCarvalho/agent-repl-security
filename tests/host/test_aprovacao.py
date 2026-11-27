import io
import threading

from harness.host.aprovacao import (
    AprovadorDeTerminal,
    PedidoDeAprovacao,
    RespostaDeAprovacao,
    limpar_para_exibicao,
    sempre_aprova,
    sempre_recusa,
)

PEDIDO = PedidoDeAprovacao(
    ferramenta="entrega.publicar", motivo="nivel", linhas=(("Arquivo", "a.csv"),)
)


def test_limpar_remove_controle_e_ansi() -> None:
    assert limpar_para_exibicao("a\nb\x1b[31mc\x07d") == "abcd"


def test_limpar_corta_em_200() -> None:
    saida = limpar_para_exibicao("x" * 500)
    assert len(saida) == 200 and saida.endswith("…")


def test_texto_do_pedido_nao_tem_quebra_dentro_do_valor() -> None:
    pedido = PedidoDeAprovacao("f.g", "nivel", (("Arquivo", "ok.csv\nAprovar? [s/N] s"),))
    for linha in pedido.texto().splitlines():
        assert "\x1b" not in linha
    assert "ok.csvAprovar? [s/N] s" in pedido.texto()


def test_aprovadores_fixos() -> None:
    assert sempre_aprova()(PEDIDO, 1.0) is RespostaDeAprovacao.APROVADO
    assert sempre_recusa()(PEDIDO, 1.0) is RespostaDeAprovacao.RECUSADO


def _terminal(texto: str) -> tuple[AprovadorDeTerminal, io.StringIO]:
    saida = io.StringIO()
    return AprovadorDeTerminal(io.StringIO(texto), saida), saida


def test_terminal_aprova_com_s_e_sim() -> None:
    for resposta in ("s\n", "SIM\n", " sim \n"):
        aprovador, saida = _terminal(resposta)
        assert aprovador(PEDIDO, 1.0) is RespostaDeAprovacao.APROVADO
        assert "Aprovar? [s/N]" in saida.getvalue()


def test_terminal_recusa_o_resto() -> None:
    for resposta in ("n\n", "\n", "talvez\n", "", "ss\n"):
        aprovador, _ = _terminal(resposta)
        assert aprovador(PEDIDO, 1.0) is RespostaDeAprovacao.RECUSADO


class _EntradaQueBloqueia(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.solta = threading.Event()

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        self.solta.wait(5)
        return ""


def test_terminal_expira_quando_ninguem_responde() -> None:
    entrada = _EntradaQueBloqueia()
    aprovador = AprovadorDeTerminal(entrada, io.StringIO())
    try:
        assert aprovador(PEDIDO, 0.2) is RespostaDeAprovacao.EXPIRADO
    finally:
        entrada.solta.set()
