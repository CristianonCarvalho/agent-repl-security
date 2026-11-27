# Aprovação humana e entrega.publicar: plano de implementação

**Objetivo:** dar a uma pessoa o poder de aprovar chamadas de ferramenta (regra 4 e decisão `pedir_aprovacao` da trifecta) e criar `entrega.publicar`, a primeira ação com efeito real.

**Arquitetura:** `harness/host/aprovacao.py` define a interface `Aprovador`. O `Registro` a consulta quando a ferramenta não é de leitura ou a trifecta pede aprovação, e devolve `espera_s` no `tool_result`. O sandbox e o host somam esse tempo ao prazo da célula, e o orquestrador o desconta do orçamento de tempo. `entrega.publicar` valida um arquivo de `/saida/` (lido por `DockerSandbox.ler_saida`), pede aprovação e grava na pasta de entregas.

**Spec:** [docs/superpowers/specs/2026-11-27-aprovacao-entrega-design.md](../specs/2026-11-27-aprovacao-entrega-design.md)

## Restrições globais

- Sem aprovador configurado, nada muda: mesmas mensagens, mesmos relatórios e mesmos traces. Os testes existentes continuam passando.
- Texto voltado ao modelo e ao usuário em português, sem caminhos nem detalhes internos.
- `harness/sandbox/` não importa nada de `harness/host/`.
- Formato: o gate roda `ruff format --check .`. Rode `uv run ruff format .` antes de terminar cada tarefa. Linhas de código até 100 caracteres.
- Gates ao fim de cada tarefa: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy harness tests` (deve dar `Success`) e os testes da tarefa.
- Não faça `git commit` por conta própria além do que o Meister faz.

## Foco da revisão

1. **Aprovação que demora mais que o prazo da célula** não derruba a célula (Tarefas 2, 3 e 10).
2. **Arquivo inválido nunca chega a pedir aprovação** (Tarefa 6).
3. **Texto do pedido sem caracteres de controle** vindos de nome de arquivo hostil (Tarefas 1 e 6).
4. **Aprovador que levanta exceção** conta como recusa, nunca como aprovação (Tarefa 5).
5. **`espera_s` não pode ser pedido pelo código do modelo**: só o `tool_result` montado pelo registro o carrega (Tarefa 5).

---

### Task 1: Interface do aprovador e implementações

**Files:**
- Create: `harness/host/aprovacao.py`
- Test: `tests/host/test_aprovacao.py`

**Interfaces:**
- Produces:
  - `limpar_para_exibicao(texto: str) -> str`: remove escapes ANSI e qualquer caractere não imprimível (inclusive quebras de linha) e corta em 200 caracteres, terminando em `…` quando corta.
  - `class RespostaDeAprovacao(Enum)`: `APROVADO = "aprovado"`, `RECUSADO = "recusado"`, `EXPIRADO = "expirado"`.
  - `@dataclass(frozen=True) class PedidoDeAprovacao`: `ferramenta: str`, `motivo: str`, `linhas: tuple[tuple[str, str], ...] = ()`, método `texto() -> str` que devolve o pedido formatado em várias linhas, com todo valor passado por `limpar_para_exibicao`.
  - `class Aprovador(Protocol)`: `__call__(self, pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao`.
  - `@dataclass(frozen=True) class AprovadorFixo`: campo `resposta`, devolve sempre ela. Funções `sempre_aprova()` e `sempre_recusa()`.
  - `class AprovadorDeTerminal`: `__init__(self, entrada: TextIO, saida: TextIO)`; imprime `pedido.texto()` e `Aprovar? [s/N] `, lê uma linha numa thread daemon com `join(prazo_s)`. `s` ou `sim` (qualquer caixa) aprova; qualquer outra coisa, inclusive vazio e fim da entrada, recusa; thread ainda viva depois do prazo devolve `EXPIRADO`.

- [ ] **Passo 1: escreva os testes** em `tests/host/test_aprovacao.py`:

```python
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

    def readline(self, size: int = -1) -> str:
        self.solta.wait(5)
        return ""


def test_terminal_expira_quando_ninguem_responde() -> None:
    entrada = _EntradaQueBloqueia()
    aprovador = AprovadorDeTerminal(entrada, io.StringIO())
    try:
        assert aprovador(PEDIDO, 0.2) is RespostaDeAprovacao.EXPIRADO
    finally:
        entrada.solta.set()
```

- [ ] **Passo 2:** rode `uv run pytest tests/host/test_aprovacao.py -q`; deve falhar por `ModuleNotFoundError`.
- [ ] **Passo 3:** crie `harness/host/aprovacao.py` com as interfaces acima (docstring de módulo em português explicando que o pedido só traz fatos medidos pelo host e que a aprovação vale uma chamada).
- [ ] **Passo 4:** `uv run ruff format . && uv run pytest tests/host/test_aprovacao.py -q && uv run ruff check . && uv run mypy harness tests`.

---

### Task 2: `espera_s` no protocolo, na ponte e no prazo da célula

**Files:**
- Modify: `harness/sandbox/protocol.py`
- Modify: `harness/sandbox/tools_client.py`
- Modify: `harness/sandbox/runner.py`
- Test: `tests/sandbox/test_espera.py`

**Interfaces:**
- Produces:
  - `ToolResult.espera_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)`: segundos que o host esperou por aprovação humana. `encode` **omite** o campo quando é `None` (a mensagem fica idêntica à de hoje).
  - `Ponte.credito_s -> float` (propriedade): soma dos `espera_s` recebidos na célula em curso; `Ponte.nova_celula()` o zera.
  - `Repl.run_cell` espera até `inicio + timeout_s + credito_s`, recalculado a cada volta de 50 ms; a interrupção e a graça de 1 s continuam como hoje.

- [ ] **Passo 1: escreva os testes** em `tests/sandbox/test_espera.py`:

```python
import io

import pytest
from pydantic import ValidationError

from harness.sandbox.protocol import ToolResult, decode, encode
from harness.sandbox.runner import Repl
from harness.sandbox.tools_client import Ponte


def test_espera_s_validacao() -> None:
    assert ToolResult(call_id="c1", ok=True, espera_s=1.5).espera_s == 1.5
    for invalido in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            ToolResult(call_id="c1", ok=True, espera_s=invalido)


def test_encode_omite_espera_quando_none_e_inclui_quando_presente() -> None:
    assert "espera_s" not in encode(ToolResult(call_id="c1", ok=True))
    linha = encode(ToolResult(call_id="c1", ok=True, espera_s=2.0))
    assert '"espera_s":2.0' in linha
    assert decode(linha) == ToolResult(call_id="c1", ok=True, espera_s=2.0)


def test_ponte_soma_o_credito_e_zera_na_celula_nova() -> None:
    resposta = encode(ToolResult(call_id="c1", ok=True, data=1, espera_s=2.5))
    ponte = Ponte(io.StringIO(resposta), io.StringIO())
    assert ponte.chamar("a.b", {}) == 1
    assert ponte.credito_s == 2.5
    ponte.nova_celula()
    assert ponte.credito_s == 0.0


CELULA = "tools.g.n()\nimport time\ntime.sleep(0.6)\nprint('ok')"


def _repl(espera: float | None) -> Repl:
    resposta = encode(ToolResult(call_id="c1", ok=True, data=1, espera_s=espera))
    return Repl(Ponte(io.StringIO(resposta), io.StringIO()))


def test_credito_estende_o_prazo_da_celula() -> None:
    feito = _repl(1.0).run_cell(1, CELULA, timeout_s=0.4).done
    assert feito.exception is None and feito.stdout == "ok\n"


def test_sem_credito_a_mesma_celula_estoura() -> None:
    feito = _repl(None).run_cell(1, CELULA, timeout_s=0.4).done
    assert feito.exception is not None and "TimeoutError" in feito.exception
```

- [ ] **Passo 2:** rode e veja falhar (`uv run pytest tests/sandbox/test_espera.py -q`).
- [ ] **Passo 3:** em `protocol.py`, acrescente o campo a `ToolResult` e, em `encode`, retire `espera_s` do dicionário quando for `None` e a mensagem for `tool_result`.
- [ ] **Passo 4:** em `tools_client.py`, `Ponte` ganha `self._credito_s = 0.0`; `nova_celula` o zera; propriedade `credito_s`; em `_esperar_resultado`, ao receber um `ToolResult` do `call_id` esperado, faça `self._credito_s += mensagem.espera_s or 0.0` (antes de devolver ou levantar `ToolError`).
- [ ] **Passo 5:** em `runner.py`, `Repl.run_cell`: troque `thread.join(timeout_s)` por este laço (a variável `inicio = time.monotonic()` já existe acima):

```python
            thread.start()
            while thread.is_alive():
                credito = self._ponte.credito_s if self._ponte is not None else 0.0
                restante = inicio + timeout_s + credito - time.monotonic()
                if restante <= 0:
                    break
                thread.join(min(restante, 0.05))
            if thread.is_alive():
                estourou = True
                ...  # bloco existente de interrupção, sem mudança
```

- [ ] **Passo 6:** `uv run ruff format . && uv run pytest tests/sandbox -q -m "not docker" && uv run ruff check . && uv run mypy harness tests`.

---

### Task 3: Ler `/saida` e somar a espera no host

**Depends on:** Task 2

**Files:**
- Modify: `harness/sandbox/docker_sandbox.py`
- Test: `tests/sandbox/test_ler_saida.py`

**Interfaces:**
- Consumes: `ToolResult.espera_s` (Tarefa 2).
- Produces:
  - `ResultadoExecucao.espera_aprovacao_s: float = 0.0` (último campo do dataclass).
  - `DockerSandbox.ler_saida(self, nome: str, limite_bytes: int) -> bytes`: usa `self._conteiner.get_archive(f"/saida/{nome}")`; sem contêiner levanta `SandboxErro`; falha da API vira `SandboxErro("arquivo de saída não encontrado ou ilegível")`.
  - `_extrair_arquivo_unico(fluxo: Iterable[bytes], nome: str, limite_bytes: int) -> bytes` (função de módulo, pura): junta os pedaços (recusa se passar de `limite_bytes` + 1 MiB de cabeçalhos tar), abre o tar e exige **exatamente um membro**, arquivo regular, com `name == nome` e `size <= limite_bytes`; qualquer outra coisa levanta `SandboxErro` com mensagem curta.
  - `executar_detalhado`: a cada resposta de ferramenta com `espera_s`, soma ao `prazo` do host e a um acumulador, devolvido em `ResultadoExecucao.espera_aprovacao_s`.

- [ ] **Passo 1: escreva os testes** em `tests/sandbox/test_ler_saida.py` (tar em memória, sem Docker):

```python
import io
import tarfile

import pytest

from harness.sandbox.docker_sandbox import (
    ResultadoExecucao,
    SandboxErro,
    _extrair_arquivo_unico,
)


def _tar(*membros: tuple[str, bytes | None, str]) -> list[bytes]:
    """Cada membro: (nome, conteúdo ou None, tipo 'arq'|'dir'|'link')."""
    bruto = io.BytesIO()
    with tarfile.open(fileobj=bruto, mode="w") as tar:
        for nome, conteudo, tipo in membros:
            info = tarfile.TarInfo(nome)
            if tipo == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif tipo == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                dados = conteudo or b""
                info.size = len(dados)
                tar.addfile(info, io.BytesIO(dados))
    valor = bruto.getvalue()
    return [valor[i : i + 700] for i in range(0, len(valor), 700)]


def test_le_um_arquivo_regular() -> None:
    assert (
        _extrair_arquivo_unico(_tar(("a.csv", b"x,y\n1,2\n", "arq")), "a.csv", 1000)
        == b"x,y\n1,2\n"
    )


@pytest.mark.parametrize(
    "membros",
    [
        (("a.csv", None, "dir"),),
        (("a.csv", None, "link"),),
        (("a.csv", b"1", "arq"), ("b.csv", b"2", "arq")),
        (("outro.csv", b"1", "arq"),),
    ],
)
def test_recusa_o_que_nao_e_um_unico_arquivo_regular(membros) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(SandboxErro):
        _extrair_arquivo_unico(_tar(*membros), "a.csv", 1000)


def test_recusa_arquivo_acima_do_limite() -> None:
    with pytest.raises(SandboxErro):
        _extrair_arquivo_unico(_tar(("a.csv", b"x" * 2000, "arq")), "a.csv", 1000)


def test_resultado_de_execucao_tem_espera_zero_por_padrao() -> None:
    assert ResultadoExecucao.__dataclass_fields__["espera_aprovacao_s"].default == 0.0
```

- [ ] **Passo 2:** rode e veja falhar.
- [ ] **Passo 3:** implemente `_extrair_arquivo_unico`, `DockerSandbox.ler_saida` e o campo `espera_aprovacao_s` (ajuste os imports `io`, `tarfile`, `Iterable`).
- [ ] **Passo 4:** em `executar_detalhado`, depois de `resposta = _resposta_a_ferramenta(...)`, acrescente `if resposta.espera_s: prazo += resposta.espera_s; espera_total += resposta.espera_s` (`espera_total = 0.0` antes do laço) e devolva `espera_aprovacao_s=espera_total` nos dois `ResultadoExecucao` do fim.
- [ ] **Passo 5:** `uv run ruff format . && uv run pytest tests/sandbox -q -m "not docker" && uv run ruff check . && uv run mypy harness tests`.

---

### Task 4: Orçamento de tempo desconta a espera

**Depends on:** Task 3

**Files:**
- Modify: `harness/orchestrator/loop.py`
- Modify: `harness/orchestrator/loop_json.py`
- Test: `tests/orchestrator/test_espera_orcamento.py`

**Interfaces:**
- Consumes: `ResultadoExecucao.espera_aprovacao_s` (Tarefa 3) e `ToolResult.espera_s`.
- Produces: `_Estado.espera_s: float = 0.0`; `Orquestrador._restante` passa a ser `max_segundos - (relogio() - inicio - espera_s)`. Em `loop_json.py`, o mesmo desconto onde houver cálculo equivalente de tempo restante (procure `max_segundos`), somando `resultado.espera_s or 0.0` de cada chamada despachada.

- [ ] **Passo 1: escreva os testes** em `tests/orchestrator/test_espera_orcamento.py`. Leia primeiro `tests/orchestrator/test_loop.py` para ver como construir um `Orquestrador` e um `_Estado`. O teste central é puro:

```python
from harness.host.perfil import perfil_por_nome
from harness.host.registry import Registro
from harness.orchestrator.loop import Orcamento, Orquestrador, _Estado
from harness.llm.base import LLMFalso


def _orquestrador(agora: list[float]) -> Orquestrador:
    return Orquestrador(
        LLMFalso([]),
        object(),  # type: ignore[arg-type]
        Registro(),
        perfil_por_nome("Vendas"),
        orcamento=Orcamento(max_segundos=10.0),
        relogio=lambda: agora[0],
    )


def test_restante_desconta_a_espera_por_aprovacao() -> None:
    agora = [100.0]
    orquestrador = _orquestrador(agora)
    estado = _Estado()
    estado.inicio = 0.0
    assert orquestrador._restante(estado) == pytest.approx(-90.0)
    estado.espera_s = 95.0
    assert orquestrador._restante(estado) == pytest.approx(5.0)
```

(acrescente `import pytest`; ajuste a construção de `LLMFalso` e `_Estado` ao que o arquivo `test_loop.py` usa). Acrescente também, se o fake de sandbox do `test_loop.py` permitir, um teste em que a célula devolve `espera_aprovacao_s` e o `estado.espera_s` acumula.

- [ ] **Passo 2:** rode e veja falhar.
- [ ] **Passo 3:** em `loop.py`: campo `espera_s: float = 0.0` em `_Estado`; depois de cada `executar_detalhado`, `estado.espera_s += execucao.espera_aprovacao_s`; `_restante` com o desconto. Em `loop_json.py`: o mesmo para a espera de cada `ToolResult`.
- [ ] **Passo 4:** `uv run ruff format . && uv run pytest tests/orchestrator -q && uv run ruff check . && uv run mypy harness tests`.

---

### Task 5: Registro pede aprovação

**Depends on:** Task 1, Task 2

**Files:**
- Modify: `harness/host/registry.py`
- Modify: `tests/host/test_registro_politica.py`
- Test: `tests/host/test_registro_aprovacao.py`

**Interfaces:**
- Consumes: `aprovacao.Aprovador`, `PedidoDeAprovacao`, `RespostaDeAprovacao`; `ToolResult.espera_s`.
- Produces:
  - `Ferramenta.montar_pedido: Callable[[Any, Perfil], tuple[tuple[str, str], ...]] | None = None`, e o parâmetro `montar_pedido=None` no decorador `Registro.ferramenta(...)`.
  - `Registro(limite_resultado_bytes=..., *, politica=False, aprovador: Aprovador | None = None, prazo_aprovacao_s: float = 120.0)`. O parâmetro `aprovador` antigo (um `Callable[[ToolCall, Ferramenta], bool]`) é **substituído**; remova `_nunca_aprova`.
  - `Registro.observar_aprovacao(ao_evento: Callable[[str, dict[str, Any]], None] | None) -> None`: recebe `("aprovacao_pedida", {"ferramenta", "motivo"})` e `("aprovacao_resposta", {"ferramenta", "resposta", "espera_s"})`.
  - Em `_despachar`, depois de validar os argumentos: motivos = `nivel` (ferramenta não leitura) e `trifecta` (política ligada e `avaliar` devolveu algo diferente de `PERMITIR`; `_notificar` continua sendo chamado). Sem motivo, segue. Com motivo e **sem aprovador**, o comportamento de hoje (mensagem de nível exatamente como está; `MENSAGEM_POLITICA` quando o único motivo é a trifecta). Com aprovador: monta as `linhas` com `ferramenta.montar_pedido(argumentos, perfil)` dentro de `try` (`ErroFerramenta` vira erro da chamada com a mensagem dele, `PermissionError` vira `acesso negado: ...`, outra exceção vira `falha ao executar a ferramenta '<nome>'`, e nada disso pede aprovação); sem montador, `(("Ferramenta", nome),)`. Pede a aprovação com `prazo_aprovacao_s`, mede o tempo com `time.monotonic()` e limita `espera` a `prazo_aprovacao_s`. Aprovador que levanta exceção conta como `RECUSADO`.
  - `RECUSADO`: erro `a chamada foi recusada pela pessoa responsável`; `EXPIRADO`: erro `a aprovação não chegou a tempo`; `APROVADO`: executa a função. Todo `ToolResult` devolvido depois de uma espera (sucesso ou erro) traz `espera_s=espera`, via `resultado.model_copy(update={"espera_s": espera})`; sem aprovação o campo não existe.

- [ ] **Passo 1: atualize** `tests/host/test_registro_politica.py`: o teste `test_aprovador_pode_liberar_a_chamada` passa a usar `aprovador=sempre_aprova()` (importe de `harness.host.aprovacao`).
- [ ] **Passo 2: escreva os testes** em `tests/host/test_registro_aprovacao.py`:

```python
from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.host.aprovacao import (
    PedidoDeAprovacao,
    RespostaDeAprovacao,
    sempre_aprova,
    sempre_recusa,
)
from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import ErroFerramenta, Nivel, Registro
from harness.sandbox.protocol import ToolCall


class Vazio(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _perfil() -> Perfil:
    return perfil_por_nome("Vendas").com_concessoes(frozenset({"teste.*"}))


def _registro(**opcoes: Any) -> tuple[Registro, list[str]]:
    chamadas: list[str] = []
    registro = Registro(**opcoes)

    @registro.ferramenta(
        nome="teste.escreve",
        nivel=Nivel.ESCRITA_EXTERNA,
        descricao="x",
        args=Vazio,
        canal_externo=True,
        montar_pedido=lambda args, perfil: (("Arquivo", "a\nb.csv"),),
    )
    def escreve(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("escreveu")
        return "ok"

    @registro.ferramenta(
        nome="teste.invalida",
        nivel=Nivel.ESCRITA_EXTERNA,
        descricao="x",
        args=Vazio,
        montar_pedido=lambda args, perfil: (_ for _ in ()).throw(
            ErroFerramenta("arquivo inválido")
        ),
    )
    def invalida(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("invalida")
        return "ok"

    return registro, chamadas


def _chamar(registro: Registro, nome: str = "teste.escreve") -> Any:
    return registro.despachar(ToolCall(call_id="c1", name=nome, args={}), _perfil())


def test_sem_aprovador_mantem_a_mensagem_de_hoje() -> None:
    registro, chamadas = _registro()
    resultado = _chamar(registro)
    assert resultado.ok is False and "exige aprovação humana" in (resultado.error or "")
    assert chamadas == []


def test_aprovado_executa_e_traz_espera() -> None:
    registro, chamadas = _registro(aprovador=sempre_aprova())
    resultado = _chamar(registro)
    assert resultado.ok is True and chamadas == ["escreveu"]
    assert resultado.espera_s is not None and resultado.espera_s >= 0.0


def test_recusado_nao_executa() -> None:
    registro, chamadas = _registro(aprovador=sempre_recusa())
    resultado = _chamar(registro)
    assert resultado.ok is False and "pessoa responsável" in (resultado.error or "")
    assert chamadas == [] and resultado.espera_s is not None


def test_expirado_nao_executa() -> None:
    registro, chamadas = _registro(aprovador=lambda pedido, prazo: RespostaDeAprovacao.EXPIRADO)
    resultado = _chamar(registro)
    assert "a tempo" in (resultado.error or "") and chamadas == []


def test_aprovador_que_levanta_conta_como_recusa() -> None:
    def quebra(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        raise RuntimeError("segredo em /Users/alguem")

    registro, chamadas = _registro(aprovador=quebra)
    resultado = _chamar(registro)
    assert resultado.ok is False and chamadas == []
    assert "/Users" not in (resultado.error or "")


def test_montador_que_recusa_nao_pede_aprovacao() -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro, chamadas = _registro(aprovador=espiao)
    resultado = _chamar(registro, "teste.invalida")
    assert resultado.error == "arquivo inválido" and pedidos == [] and chamadas == []


def test_pedido_leva_as_linhas_do_montador_e_o_motivo() -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro, _ = _registro(aprovador=espiao)
    _chamar(registro)
    assert pedidos[0].motivo == "nivel" and pedidos[0].linhas == (("Arquivo", "a\nb.csv"),)


def test_motivo_nivel_e_trifecta_pergunta_uma_vez() -> None:
    pedidos: list[PedidoDeAprovacao] = []

    def espiao(pedido: PedidoDeAprovacao, prazo: float) -> RespostaDeAprovacao:
        pedidos.append(pedido)
        return RespostaDeAprovacao.APROVADO

    registro, _ = _registro(politica=True, aprovador=espiao)
    registro.exposicao.viu_dado_privado = True
    registro.exposicao.viu_nao_confiavel = True
    _chamar(registro)
    assert len(pedidos) == 1 and pedidos[0].motivo == "nivel+trifecta"


def test_observador_recebe_pedida_e_resposta() -> None:
    registro, _ = _registro(aprovador=sempre_aprova())
    eventos: list[tuple[str, dict[str, Any]]] = []
    registro.observar_aprovacao(lambda tipo, dados: eventos.append((tipo, dados)))
    _chamar(registro)
    assert [tipo for tipo, _ in eventos] == ["aprovacao_pedida", "aprovacao_resposta"]
    assert eventos[1][1]["resposta"] == "aprovado"
```

- [ ] **Passo 3:** rode e veja falhar.
- [ ] **Passo 4:** implemente em `registry.py` conforme as interfaces. Mantenha `MENSAGEM_POLITICA` e a mensagem de nível atual intactas para o caso sem aprovador.
- [ ] **Passo 5:** `uv run ruff format . && uv run pytest tests/host -q && uv run ruff check . && uv run mypy harness tests`.

---

### Task 6: Ferramenta `entrega.publicar`

**Depends on:** Task 5

**Files:**
- Create: `harness/host/tools/entrega.py`
- Test: `tests/host/test_entrega.py`

**Interfaces:**
- Consumes: `Registro.ferramenta(..., montar_pedido=...)`, `Nivel.ESCRITA_EXTERNA`, `ErroFerramenta`.
- Produces: `registrar(registro: Registro, ler_saida: Callable[[str, int], bytes], pasta_entregas: Path, agora: Callable[[], datetime] = ...) -> None`, que registra `entrega.publicar` (`nivel=ESCRITA_EXTERNA`, `canal_externo=True`, `dado_privado=False`, `conteudo_nao_confiavel=False`). Constantes `DEPARTAMENTOS = ("vendas", "comercial", "marketing", "financeiro", "direcao")`, `EXTENSOES = frozenset({".xlsx", ".docx", ".pptx", ".pdf", ".csv", ".txt", ".png"})`, `LIMITE_BYTES = 5 * 1024 * 1024`.

Comportamento:

- Argumentos (`extra="forbid"`): `caminho: str`, `destinatario: str`.
- `montar_pedido` valida e prepara, **antes** de qualquer aprovação: (1) `caminho` começa com `/saida/`, normalizado (`posixpath.normpath(caminho) == caminho`), com exatamente um nível depois de `/saida/` e nome em `[A-Za-z0-9._-]+`; (2) `destinatario` em `DEPARTAMENTOS`; (3) extensão em `EXTENSOES`; (4) `conteudo = ler_saida(nome, LIMITE_BYTES)` (`SandboxErro` ou qualquer falha vira `ErroFerramenta("não foi possível ler o arquivo de saída")`); (5) arquivo não vazio; (6) para `.xlsx`, `.docx` e `.pptx`, `zipfile.is_zipfile` e nenhum nome de membro terminando em `vbaProject.bin` (caso contrário `ErroFerramenta` com mensagem curta). Guarda `conteudo` numa memória interna indexada por `caminho` e devolve as linhas `("Arquivo", nome)`, `("Tipo", extensão)`, `("Tamanho", "<n> KiB")`, `("Hash", sha256[:16])`, `("Destinatário", destinatario)`.
- A função da ferramenta retira o conteúdo da memória (sem reler `/saida`; se faltar, `ErroFerramenta("o arquivo não foi validado")`), grava em `<pasta>/<destinatario>/<aaaammdd-hhmmss>-<nome>` com modo `"xb"` (não sobrescreve; colisão vira `ErroFerramenta`), acrescenta uma linha JSON a `<pasta>/registro.jsonl` (`data`, `arquivo`, `destinatario`, `sha256`, `perfil`) e devolve `{"entregue": True, "arquivo": "<destinatario>/<nome gravado>", "sha256": "<hash completo>"}`.

- [ ] **Passo 1: escreva os testes** em `tests/host/test_entrega.py`, usando `Registro(aprovador=sempre_aprova())`, um `ler_saida` falso (dicionário de nome para bytes, levantando `KeyError` para ausentes) e `tmp_path`. Cubra, cada um como teste separado: entrega feliz de `.csv` (arquivo gravado com o mesmo conteúdo, linha no `registro.jsonl`, retorno com `entregue`); caminho com `..`, com subpasta, fora de `/saida/` e com nome de caracteres estranhos; destinatário desconhecido; extensão fora da lista (`.exe`); arquivo vazio; zip de `.xlsx` com `xl/vbaProject.bin` (recusa); `.xlsx` que não é zip (recusa); `ler_saida` que levanta (mensagem curta, sem o texto da exceção); **a validação roda antes do pedido** (um aprovador espião não é chamado quando o arquivo é inválido); **o conteúdo gravado é o que foi validado** (o `ler_saida` falso devolve outro conteúdo na segunda chamada e o arquivo gravado traz o primeiro; `ler_saida` chamado uma vez só); nome hostil com `\n` e `\x1b` é recusado pela regra do caminho; duas entregas do mesmo nome no mesmo segundo (relógio falso fixo) levantam erro na segunda; com `sempre_recusa()` nada é gravado.
- [ ] **Passo 2:** rode e veja falhar.
- [ ] **Passo 3:** implemente `harness/host/tools/entrega.py`.
- [ ] **Passo 4:** `uv run ruff format . && uv run pytest tests/host -q && uv run ruff check . && uv run mypy harness tests`.

---

### Task 7: Orquestradores emitem os eventos de aprovação

**Depends on:** Task 4, Task 5

**Files:**
- Modify: `harness/orchestrator/loop.py`
- Modify: `harness/orchestrator/loop_json.py`
- Test: `tests/orchestrator/test_aprovacao_loop.py`

**Interfaces:**
- Consumes: `Registro.observar_aprovacao`.
- Produces: no início do pedido (junto de `observar_politica`), `self._registro.observar_aprovacao(self._emitir_aprovacao)`, onde `_emitir_aprovacao(tipo: str, dados: dict[str, Any]) -> None` chama `self._emitir(tipo, **dados)`. Os eventos de trace passam a ser `aprovacao_pedida` e `aprovacao_resposta`.

- [ ] **Passo 1: escreva o teste** em `tests/orchestrator/test_aprovacao_loop.py`, no mesmo estilo de `tests/orchestrator/test_politica_loop.py` (reutilize os fakes de lá): um `Registro(aprovador=sempre_aprova())` com uma ferramenta de escrita registrada e uma chamada que a aciona pelo laço; depois do pedido, a lista de eventos contém `aprovacao_pedida` e `aprovacao_resposta` com `ferramenta` e `resposta="aprovado"`; e nenhum evento carrega o argumento da ferramenta nem o conteúdo de arquivo.
- [ ] **Passo 2:** rode e veja falhar.
- [ ] **Passo 3:** implemente nos dois laços.
- [ ] **Passo 4:** `uv run ruff format . && uv run pytest tests/orchestrator -q && uv run ruff check . && uv run mypy harness tests`.

---

### Task 8: `--aprovador` nas avaliações

**Depends on:** Task 5

**Files:**
- Modify: `harness/evals/executor.py`
- Modify: `harness/evals/relatorio.py`
- Modify: `harness/evals/ferramentas_json.py`
- Modify: `harness/evals/__main__.py`
- Test: `tests/evals/test_aprovador_avaliacao.py`

**Interfaces:**
- Produces: `ConfigExecucao.aprovador: Literal["recusar", "aprovar"] | None = None`; `registro_json(raiz, variante, *, politica=False, aprovador=None)`; `agregar(..., aprovador: str | None = None)` e `Relatorio.aprovador: str | None = None`, que some do JSON quando é `None` (acrescente em `_CAMPOS_DE_ATAQUE_PADRAO`); flag `--aprovador {recusar,aprovar}` (padrão ausente). No executor, `Registro(politica=config.politica_trifecta, aprovador=_aprovador_de(config.aprovador))`, onde `None` dá `None` (comportamento de hoje), `"recusar"` dá `sempre_recusa()` e `"aprovar"` dá `sempre_aprova()`. `--salvar-linha-de-base` recusa `--aprovador`.

- [ ] **Passo 1: escreva os testes** em `tests/evals/test_aprovador_avaliacao.py`, no estilo de `tests/evals/test_politica_trifecta.py`: padrão `None`; parser aceita as duas opções e recusa outra; `registro_json(..., aprovador=sempre_aprova())` repassa; relatório sem aprovador não tem o campo no JSON, e com `aprovador="aprovar"` o tem; linha de base recusa a flag (código `CODIGO_USO` e a mensagem cita `--aprovador`).
- [ ] **Passo 2:** rode e veja falhar.
- [ ] **Passo 3:** implemente espelhando o encadeamento de `politica_trifecta`.
- [ ] **Passo 4:** `uv run ruff format . && uv run pytest tests/evals -q -m "not docker" && uv run ruff check . && uv run mypy harness tests`.

---

### Task 9: CLI do harness: `--entregas` e `--prazo-aprovacao`

**Depends on:** Task 3, Task 6, Task 7

**Files:**
- Modify: `harness/cli.py`
- Modify: `harness/host/tools/__init__.py`
- Test: `tests/test_cli_entregas.py`

**Interfaces:**
- Consumes: `entrega.registrar`, `AprovadorDeTerminal`, `sempre_recusa`, `DockerSandbox.ler_saida`.
- Produces: flags `--entregas PASTA` e `--prazo-aprovacao N` (segundos, padrão 120, inteiro positivo). O aprovador só existe quando `--entregas` ou `--politica-trifecta` for passado: com `sys.stdin.isatty()` verdadeiro, `AprovadorDeTerminal(sys.stdin, saida)`; sem terminal, `sempre_recusa()`. O `Registro` recebe `aprovador` e `prazo_aprovacao_s`. Com `--entregas`, registra `entrega.publicar` com `ler_saida` ligada ao sandbox (como o sandbox é criado depois do registro, use um suporte `sandbox_ref: list[Any] = []`, preencha-o depois de criar o sandbox e passe `lambda nome, limite: sandbox_ref[0].ler_saida(nome, limite)`), e soma a concessão `entrega.publicar` ao perfil. Sem as flags, nada muda.

- [ ] **Passo 1: escreva os testes** em `tests/test_cli_entregas.py`, no estilo de `tests/test_cli_mcp.py` (fábrica de sandbox falsa): parser aceita as flags; sem `--entregas` a ferramenta `entrega.publicar` não existe no registro nem no perfil; com `--entregas` ela existe e o perfil a concede; `--prazo-aprovacao 0` e `abc` são recusados; sem terminal o aprovador recusa (uma chamada que exige aprovação volta com a mensagem de recusa pela pessoa responsável).
- [ ] **Passo 2:** rode e veja falhar.
- [ ] **Passo 3:** implemente. Em `harness/host/tools/__init__.py` não registre `entrega` em `registrar_todas`: ela depende de `ler_saida` e da pasta, e é registrada pela CLI.
- [ ] **Passo 4:** `uv run ruff format . && uv run pytest tests/test_cli.py tests/test_cli_mcp.py tests/test_cli_entregas.py -q && uv run ruff check . && uv run mypy harness tests`.

---

### Task 10: Integração com Docker e documentação

**Depends on:** Task 8, Task 9

**Files:**
- Create: `tests/sandbox/test_docker_entrega.py`
- Modify: `docs/especificacao-v0.md`
- Modify: `docs/diagramas-seguranca.md`

Fecha a peça: teste ponta a ponta com Docker, documentação e os gates finais.

- [ ] **Passo 1: escreva o teste de integração** em `tests/sandbox/test_docker_entrega.py` (marcador `docker`, no estilo de `tests/sandbox/test_docker_rastreio.py`): raiz de dados mínima, `pasta_entregas` temporária, `Registro(aprovador=...)` com `registrar_todas` e `entrega.registrar(registro, lambda n, l: sandbox.ler_saida(n, l), pasta)`, perfil `Financeiro` somado a `entrega.publicar`. Casos: (a) com `sempre_aprova()`, a célula grava `/saida/relatorio.csv` e chama `tools.entrega.publicar(caminho="/saida/relatorio.csv", destinatario="financeiro")`, e o arquivo chega a `<pasta>/financeiro/` com o mesmo conteúdo; (b) com `sempre_recusa()` nada chega e a célula recebe `ToolError`; (c) **aprovação mais lenta que o prazo da célula**: um aprovador de teste que dorme 1,5 s, `Registro(prazo_aprovacao_s=5)`, `executar_detalhado(1, codigo, 1.0, despachante=...)`: `done.exception is None` e `resultado.espera_aprovacao_s >= 1.4`; (d) `ler_saida` recusa link simbólico (`os.symlink('/etc/passwd', '/saida/l')`), diretório (`os.mkdir('/saida/d')`) e arquivo acima do limite.
- [ ] **Passo 2:** rode `uv run pytest tests/sandbox/test_docker_entrega.py -q`; com Docker disponível todos passam.
- [ ] **Passo 3: documentação.** Em `docs/especificacao-v0.md`: na tabela de estado, acrescente a linha "Aprovação humana por terminal e `entrega.publicar` (spec de 27/11/2026, peça 2)" como **Implementado**, e deixe "Aprovação por painel web ou fila, `propor_alteracao`, gVisor" como **Não iniciado**; na tabela de ferramentas, troque a nota de `entrega.publicar` para "implementada na peça 2 da Fase 4"; ajuste a frase da D12 pendente (espera por aprovação) para "implementada: a espera vira crédito de prazo da célula e sai do orçamento do pedido". Em `docs/diagramas-seguranca.md`, acrescente um diagrama `mermaid` do fluxo de uma entrega: o código grava em `/saida/`, chama a ferramenta, o registro valida o arquivo e monta o pedido só com fatos medidos, a pessoa aprova no terminal, o host grava em entregas e devolve o resultado com `espera_s`.
- [ ] **Passo 4: gates e suíte completa**, sem execuções reais de modelo em paralelo: `uv run ruff format . && uv run ruff check . && uv run mypy harness tests && uv run pytest -q -p no:cacheprovider`.

---

## Autorrevisão

**Cobertura da spec.** Aprovador e implementações (Tarefa 1); `espera_s`, crédito na ponte e no `run_cell` (Tarefa 2); `ler_saida` e soma no prazo do host (Tarefa 3); desconto no orçamento (Tarefa 4); registro com aprovador, motivos `nivel` e `trifecta`, mensagens, `espera_s` e eventos (Tarefa 5); `entrega.publicar` com validação antes do pedido e memória por caminho (Tarefa 6); eventos de trace nos laços (Tarefa 7); `--aprovador` nas avaliações (Tarefa 8); `--entregas` e `--prazo-aprovacao` (Tarefa 9); integração com Docker e documentação (Tarefa 10).

**Ajustes em relação à spec.**
- `--aprovador` nas avaliações fica sem valor padrão (equivale ao comportamento de recusa de hoje) para não mudar as medições existentes; `recusar` explícito usa `sempre_recusa()`.
- A memória do arquivo validado é indexada pelo `caminho` (as chamadas são sequenciais numa sessão), em vez do `call_id`, porque o montador do pedido não recebe o `call_id`.
- O aprovador na CLI só existe com `--entregas` ou `--politica-trifecta`, para não mudar o comportamento dos testes e usos atuais.
