"""Testes da opção `--contexto` da CLI, com LLM falso e sandbox falso (sem Docker e sem rede)."""

from collections.abc import Callable, Sequence
from io import StringIO
from pathlib import Path

import pytest

from harness.cli import CODIGO_COMPLETO, CODIGO_USO, main
from harness.llm.base import LLMFalso, Mensagem
from harness.sandbox.contexto import ValorContexto
from harness.sandbox.docker_sandbox import LIMITE_CONTEXTO_BYTES, ResultadoExecucao
from harness.sandbox.protocol import ExecDone, RespostaFinal, ToolCall, ToolResult

FINAL_OK = "```python\nFINAL('Resposta pronta', ['contexto.txt'])\n```"
RESPOSTA_FINAL = RespostaFinal(answer="Resposta pronta", sources=["contexto.txt"])


class SandboxContexto:
    """Sandbox falso: registra as cargas de contexto e responde a célula com FINAL."""

    def __init__(self) -> None:
        self.cargas: list[ValorContexto] = []
        self.fechamentos = 0

    def start(self) -> None:
        pass

    def carregar_contexto(self, valor: ValorContexto) -> None:
        self.cargas.append(valor)

    def executar_detalhado(
        self,
        cell_id: int,
        code: str,
        timeout_s: float,
        despachante: Callable[[ToolCall], ToolResult] | None = None,
    ) -> ResultadoExecucao:
        done = ExecDone(
            cell_id=cell_id,
            stdout="",
            stderr="",
            exception=None,
            duration_ms=1,
            final=RESPOSTA_FINAL,
        )
        return ResultadoExecucao(done=done, ferramentas={})

    def reiniciar(self) -> None:
        pass

    def close(self) -> None:
        self.fechamentos += 1


@pytest.fixture
def dados(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    return raiz


def _argv(dados: Path, trace: Path, *extra: str) -> list[str]:
    return [
        "Resuma o arquivo",
        "--perfil",
        "Vendas",
        "--dados",
        str(dados),
        "--trace",
        str(trace),
        *extra,
    ]


def _executar(
    argv: Sequence[str], sandbox: SandboxContexto, modelo: LLMFalso | None = None
) -> tuple[int, str]:
    saida = StringIO()
    codigo = main(
        argv,
        modelo=modelo if modelo is not None else LLMFalso([FINAL_OK]),
        saida=saida,
        fabrica_sandbox=lambda *_args, **_kwargs: sandbox,
    )
    return codigo, saida.getvalue()


def _mensagens_do_usuario(modelo: LLMFalso) -> list[Mensagem]:
    return [m for m in modelo.chamadas[0] if m.role == "user"]


# --- Carga bem-sucedida -------------------------------------------------------------


def test_contexto_de_texto_chega_ao_sandbox_como_texto(dados: Path, tmp_path: Path) -> None:
    arquivo = tmp_path / "contexto.txt"
    arquivo.write_text("Relatório de vendas\nção, acentuação\n", encoding="utf-8")
    sandbox = SandboxContexto()

    codigo, _ = _executar(_argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), sandbox)

    assert codigo == CODIGO_COMPLETO
    assert sandbox.cargas == ["Relatório de vendas\nção, acentuação\n"]


def test_contexto_json_vira_objeto_e_extensao_e_ignorada_em_maiusculas(
    dados: Path, tmp_path: Path
) -> None:
    arquivo = tmp_path / "pedidos.JSON"
    arquivo.write_text('{"pedidos": [1, 2], "cliente": "Acme"}', encoding="utf-8")
    sandbox = SandboxContexto()

    codigo, _ = _executar(_argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), sandbox)

    assert codigo == CODIGO_COMPLETO
    assert sandbox.cargas == [{"pedidos": [1, 2], "cliente": "Acme"}]


def test_contexto_json_lista_e_aceito(dados: Path, tmp_path: Path) -> None:
    arquivo = tmp_path / "linhas.json"
    arquivo.write_text("[1, 2, 3]", encoding="utf-8")
    sandbox = SandboxContexto()
    codigo, _ = _executar(_argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), sandbox)
    assert codigo == CODIGO_COMPLETO
    assert sandbox.cargas == [[1, 2, 3]]


def test_sem_a_opcao_nao_ha_carga_e_o_modelo_nao_ve_metadados(dados: Path, tmp_path: Path) -> None:
    sandbox = SandboxContexto()
    modelo = LLMFalso([FINAL_OK])
    codigo, _ = _executar(_argv(dados, tmp_path / "traces"), sandbox, modelo)
    assert codigo == CODIGO_COMPLETO
    assert sandbox.cargas == []
    assert [m.content for m in _mensagens_do_usuario(modelo)] == ["Resuma o arquivo"]


def test_com_contexto_o_modelo_recebe_tipo_e_tamanho_na_primeira_mensagem(
    dados: Path, tmp_path: Path
) -> None:
    arquivo = tmp_path / "contexto.txt"
    arquivo.write_text("abcdef", encoding="utf-8")
    modelo = LLMFalso([FINAL_OK])
    _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(arquivo)),
        SandboxContexto(),
        modelo,
    )
    primeira = _mensagens_do_usuario(modelo)[0]
    assert "Tipo: texto. Tamanho: 6 caracteres." in primeira.content
    assert "abcdef" in primeira.content


# --- Erros de uso: código 2, antes de iniciar o sandbox -----------------------------


def test_json_invalido_sai_dois_sem_ecoar_o_conteudo(dados: Path, tmp_path: Path) -> None:
    arquivo = tmp_path / "ruim.json"
    arquivo.write_text('{"segredo": "SEGREDO-DO-ARQUIVO", ', encoding="utf-8")
    sandbox = SandboxContexto()

    codigo, saida = _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), sandbox
    )

    assert codigo == CODIGO_USO
    assert "JSON inválido" in saida
    assert "SEGREDO-DO-ARQUIVO" not in saida
    assert sandbox.cargas == []


@pytest.mark.parametrize("conteudo", ['"só um texto"', "42", "true", "null"])
def test_json_que_nao_e_objeto_ou_lista_sai_dois(
    dados: Path, tmp_path: Path, conteudo: str
) -> None:
    arquivo = tmp_path / "escalar.json"
    arquivo.write_text(conteudo, encoding="utf-8")
    codigo, saida = _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), SandboxContexto()
    )
    assert codigo == CODIGO_USO
    assert "objeto ou uma lista" in saida


def test_arquivo_inexistente_sai_dois(dados: Path, tmp_path: Path) -> None:
    codigo, saida = _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(tmp_path / "nao_existe.txt")),
        SandboxContexto(),
    )
    assert codigo == CODIGO_USO
    assert "não pode ser lido" in saida


def test_arquivo_que_nao_e_utf8_sai_dois(dados: Path, tmp_path: Path) -> None:
    arquivo = tmp_path / "binario.txt"
    arquivo.write_bytes(b"\xff\xfe\x00 nao utf-8")
    codigo, saida = _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), SandboxContexto()
    )
    assert codigo == CODIGO_USO
    assert "UTF-8" in saida


def test_arquivo_acima_do_teto_sai_dois_sem_ler_tudo(dados: Path, tmp_path: Path) -> None:
    arquivo = tmp_path / "grande.txt"
    arquivo.write_bytes(b"a" * (LIMITE_CONTEXTO_BYTES + 1))
    sandbox = SandboxContexto()
    codigo, saida = _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), sandbox
    )
    assert codigo == CODIGO_USO
    assert "maior que o limite" in saida
    assert sandbox.cargas == []


def test_json_canonico_acima_do_teto_sai_dois(dados: Path, tmp_path: Path) -> None:
    # O arquivo cabe no teto, mas o JSON canônico de um texto com aspas passa dele.
    arquivo = tmp_path / "quase.txt"
    arquivo.write_text('"' * (LIMITE_CONTEXTO_BYTES // 2 + 10), encoding="utf-8")
    codigo, saida = _executar(
        _argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), SandboxContexto()
    )
    assert codigo == CODIGO_USO
    assert "como JSON" in saida


def test_contexto_sem_a_extensao_json_nao_e_interpretado_como_json(
    dados: Path, tmp_path: Path
) -> None:
    arquivo = tmp_path / "tabela.csv"
    arquivo.write_text("a,b\n1,2\n", encoding="utf-8")
    sandbox = SandboxContexto()
    codigo, _ = _executar(_argv(dados, tmp_path / "traces", "--contexto", str(arquivo)), sandbox)
    assert codigo == CODIGO_COMPLETO
    assert sandbox.cargas == ["a,b\n1,2\n"]
