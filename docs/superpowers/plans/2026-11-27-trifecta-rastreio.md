# Rastreio de arquivos abertos e política da trifecta: plano de implementação

> **Para quem executar:** a execução deste projeto é sempre `meister orchestrate` (ver `CLAUDE.md`). Os passos usam caixas `- [ ]` para acompanhamento.

**Objetivo:** com a política ligada, recusar uma ferramenta com canal externo quando a sessão já leu dado privado e conteúdo não confiável, sabendo o que o código do sandbox abriu.

**Arquitetura:** um módulo puro `harness/host/politica.py` guarda a exposição da sessão e decide. O `Registro` consulta a política antes de executar uma ferramenta. No sandbox, um gancho de auditoria avisa o host antes de cada `open` sob `/dados`, por uma mensagem nova da ponte, e o host marca a exposição pela mesma regra de confiança do catálogo. A política vem desligada por padrão.

**Stack:** Python 3.12, pydantic, pytest, ruff, mypy, Docker (só no teste de integração).

**Spec:** [docs/superpowers/specs/2026-11-27-trifecta-rastreio-design.md](../specs/2026-11-27-trifecta-rastreio-design.md)

## Restrições globais

- Política **desligada por padrão**: sem a flag, nenhum byte de relatório, trace, prompt ou comportamento existente muda. Os testes existentes continuam passando sem edição, exceto os que dependem de assinaturas ampliadas (indicado nas tarefas).
- Texto voltado ao modelo e ao usuário em português, sem detalhes internos (caminhos, nomes de arquivo) na recusa.
- `harness/sandbox/` não importa nada de `harness/host/` (a imagem só leva `protocol.py`, `runner.py`, `tools_client.py`, `contexto.py`, e passa a levar `rastreio.py`).
- Gates ao fim de cada tarefa: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy harness tests`, e o(s) teste(s) da tarefa. Suíte completa só na Tarefa 8, sem execuções reais do modelo em paralelo (o teste de Docker confere contêineres sobrando).
- Commits só quando o usuário pedir. Os passos "Commit" abaixo são pontos de salvamento sugeridos; o orquestrador não deve fazer `git commit` sem ordem do usuário.
- Mensagens de commit terminam com `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.

## Foco da revisão

Entradas e condições que a spec insinua e nenhuma tarefa comum cobriria sozinha; cada linha tem o teste na tarefa dona:

1. **Caminho com `..` ou duplicado vindo do sandbox** (`/dados/../etc/passwd`, `/dados//x`): o host recusa e o `open` falha. Tarefa 2.
2. **`manifesto.json` malformado**: o rastreio conta o arquivo como não confiável, nunca como confiável. Tarefa 2.
3. **Ferramenta que é, ela mesma, não confiável e com canal externo** (MCP) chamada por sessão que só viu dado privado: recusa. Tarefa 2.
4. **Falha no aviso do gancho** (host não responde ou responde erro): o `open` levanta `PermissionError`, nunca passa em silêncio. Tarefa 4.
5. **Erro numa ferramenta não confiável ainda marca a exposição**: a falha não pode "limpar" a sessão. Tarefa 3.

---

## Mapa de arquivos

| Arquivo | Ação | Responsabilidade |
| --- | --- | --- |
| `harness/host/confianca.py` | criar | Manifesto e flag de confiança, compartilhados |
| `harness/host/tools/catalogo.py` | modificar | Passa a usar `confianca.py` |
| `harness/host/politica.py` | criar | `Exposicao`, `Decisao`, `avaliar`, `RastreadorDeArquivos` |
| `harness/host/registry.py` | modificar | Política, aprovador, observador, exposição |
| `harness/sandbox/protocol.py` | modificar | Mensagem `ArquivoAberto` |
| `harness/sandbox/tools_client.py` | modificar | `Ponte.avisar_abertura` |
| `harness/sandbox/rastreio.py` | criar | Gancho de auditoria |
| `harness/sandbox/runner.py` | modificar | Flag `--rastrear-arquivos` |
| `harness/sandbox/Dockerfile` | modificar | Copia `rastreio.py` |
| `harness/sandbox/docker_sandbox.py` | modificar | `rastrear_arquivos`, `ao_abrir` |
| `harness/orchestrator/loop.py` | modificar | Zera exposição, evento, `ao_abrir` |
| `harness/orchestrator/loop_json.py` | modificar | Zera exposição, evento |
| `harness/evals/executor.py`, `relatorio.py`, `ferramentas_json.py`, `__main__.py` | modificar | `politica_trifecta` na configuração, no relatório e na CLI |
| `harness/cli.py` | modificar | `--politica-trifecta` |
| `tests/host/test_confianca.py`, `test_politica.py`, `test_registro_politica.py` | criar | Unitários do host |
| `tests/sandbox/test_rastreio.py`, `test_docker_rastreio.py` | criar | Gancho e integração |
| `tests/orchestrator/test_politica_loop.py` | criar | Eventos e zeragem |
| `tests/evals/test_politica_trifecta.py` | criar | Configuração, relatório, CLI |

---

### Task 1: Confiança compartilhada

**Files:**
- Create: `harness/host/confianca.py`
- Modify: `harness/host/tools/catalogo.py`
- Test: `tests/host/test_confianca.py`

**Interfaces:**
- Produces: `CONFIAVEL: str`, `EXTERNO: str`, `NOME_MANIFESTO: str`, `class ErroManifesto(Exception)`, `ler_manifesto(raiz: Path) -> dict[str, Any]`, `confianca_de(manifesto: dict[str, Any], arquivo: str) -> str`.

- [ ] **Passo 1: escrever o teste que falha** em `tests/host/test_confianca.py`

```python
"""Confiança dos arquivos (D10), compartilhada pelo catálogo e pelo rastreio."""

from pathlib import Path

import pytest

from harness.host.confianca import (
    CONFIAVEL,
    EXTERNO,
    ErroManifesto,
    confianca_de,
    ler_manifesto,
)


def test_sem_manifesto_o_arquivo_e_confiavel(tmp_path: Path) -> None:
    assert ler_manifesto(tmp_path) == {}
    assert confianca_de({}, "vendas/a.csv") == CONFIAVEL


def test_manifesto_marca_externo(tmp_path: Path) -> None:
    (tmp_path / "manifesto.json").write_text('{"vendas/a.csv": "externo"}', encoding="utf-8")
    manifesto = ler_manifesto(tmp_path)
    assert confianca_de(manifesto, "vendas/a.csv") == EXTERNO
    assert confianca_de(manifesto, "vendas/b.csv") == CONFIAVEL


def test_valor_desconhecido_vira_externo() -> None:
    assert confianca_de({"x/a": "talvez"}, "x/a") == EXTERNO


@pytest.mark.parametrize("conteudo", ["não é json", "[1, 2]", "42"])
def test_manifesto_invalido_levanta(tmp_path: Path, conteudo: str) -> None:
    (tmp_path / "manifesto.json").write_text(conteudo, encoding="utf-8")
    with pytest.raises(ErroManifesto):
        ler_manifesto(tmp_path)
```

- [ ] **Passo 2: rodar e ver falhar**

Run: `uv run pytest tests/host/test_confianca.py -q`
Expected: FAIL com `ModuleNotFoundError: harness.host.confianca`.

- [ ] **Passo 3: criar `harness/host/confianca.py`**

```python
"""Flag de confiança dos arquivos (spec v0, D10), compartilhada pelo catálogo e pelo rastreio.

A flag vem de um `manifesto.json` opcional na raiz de dados: sem manifesto, todo arquivo é
`confiavel`; qualquer valor que não seja `confiavel` no manifesto vira `externo` (falha fechada).
"""

import json
from pathlib import Path
from typing import Any, Final

NOME_MANIFESTO: Final = "manifesto.json"
CONFIAVEL: Final = "confiavel"
EXTERNO: Final = "externo"


class ErroManifesto(Exception):
    """O `manifesto.json` existe, mas não é um objeto JSON válido."""


def ler_manifesto(raiz: Path) -> dict[str, Any]:
    """Lê o manifesto da raiz. Ausente vira dicionário vazio; malformado levanta ErroManifesto."""
    caminho = raiz / NOME_MANIFESTO
    if not caminho.is_file():
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as erro:
        raise ErroManifesto("o manifesto.json da raiz de dados é inválido") from erro
    if not isinstance(dados, dict):
        raise ErroManifesto("o manifesto.json da raiz de dados é inválido")
    return dados


def confianca_de(manifesto: dict[str, Any], arquivo: str) -> str:
    """`confiavel` só se o manifesto disser isso; qualquer outra coisa vira `externo` (D10)."""
    if arquivo not in manifesto:
        return CONFIAVEL
    return CONFIAVEL if manifesto[arquivo] == CONFIAVEL else EXTERNO
```

- [ ] **Passo 4: fazer o catálogo usar o módulo novo**

Em `harness/host/tools/catalogo.py`, remova as constantes `NOME_MANIFESTO`, `CONFIAVEL`, `EXTERNO` e as funções `_ler_manifesto` e `_confianca`, e coloque no lugar (mantendo os nomes antigos, que outros módulos e testes importam):

```python
from harness.host.confianca import (
    CONFIAVEL,
    EXTERNO,
    NOME_MANIFESTO,
    ErroManifesto,
    confianca_de,
    ler_manifesto,
)

__all__ = ["CONFIAVEL", "EXTERNO", "NOME_MANIFESTO"]


def _ler_manifesto(raiz: Path) -> dict[str, Any]:
    """Lê o manifesto; manifesto inválido vira erro de ferramenta, com mensagem para o modelo."""
    try:
        return ler_manifesto(raiz)
    except ErroManifesto as erro:
        raise ErroFerramenta(str(erro)) from erro


_confianca = confianca_de
```

Remova do topo o `import json` se ficar sem uso. O resto do arquivo continua chamando `_ler_manifesto(raiz)` e `_confianca(manifesto, arquivo)`.

- [ ] **Passo 5: rodar os testes do catálogo e do novo módulo**

Run: `uv run pytest tests/host/test_confianca.py tests/host/test_catalogo.py -q && uv run ruff check harness tests && uv run mypy harness tests`
Expected: todos passam, sem erro de lint nem de tipos.

- [ ] **Passo 6: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness/host/confianca.py harness/host/tools/catalogo.py tests/host/test_confianca.py
git commit -m "T20: confianca dos arquivos em modulo compartilhado"
```

---

### Task 2: Política pura e rastreador do host

**Files:**
- Create: `harness/host/politica.py`
- Test: `tests/host/test_politica.py`

**Depends on:** Task 1

**Interfaces:**
- Consumes: `confianca.CONFIAVEL`, `confianca.EXTERNO`, `confianca.ler_manifesto`, `confianca.confianca_de`, `confianca.ErroManifesto`.
- Produces:
  - `class Decisao(Enum)`: `PERMITIR`, `PEDIR_APROVACAO`, `RECUSAR`, com `.value` em `"permitir"`, `"pedir_aprovacao"`, `"recusar"`.
  - `class Exposicao` (dataclass mutável): campos `viu_dado_privado: bool = False`, `viu_nao_confiavel: bool = False`; métodos `marcar_arquivo(confianca: str) -> None`, `marcar_ferramenta(ferramenta: _Propriedades) -> None`, `zerar() -> None`.
  - `avaliar(exposicao: Exposicao, ferramenta: _Propriedades) -> Decisao`.
  - `class RastreadorDeArquivos`: `__init__(self, raiz: Path, exposicao: Exposicao)`, `__call__(self, caminho: str) -> str | None` (devolve `None` se ok ou a mensagem de erro).
  - `_Propriedades` é um `Protocol` com `dado_privado: bool`, `conteudo_nao_confiavel: bool`, `canal_externo: bool`, para o módulo não importar `registry`.

- [ ] **Passo 1: escrever os testes que falham** em `tests/host/test_politica.py`

```python
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
```

- [ ] **Passo 2: rodar e ver falhar**

Run: `uv run pytest tests/host/test_politica.py -q`
Expected: FAIL com `ModuleNotFoundError: harness.host.politica`.

- [ ] **Passo 3: criar `harness/host/politica.py`**

```python
"""Política da lethal trifecta e rastreio de arquivos abertos (spec v0, regra 3, D10; Fase 4).

A trifecta é a combinação de dado privado, conteúdo não confiável e canal externo na mesma
sessão. Aqui ficam três coisas sem dependência do sandbox nem do registro:

- `Exposicao`: o que a sessão já viu (dois marcadores; o terceiro fator vem da ferramenta).
- `avaliar`: decisão pura sobre uma chamada de ferramenta.
- `RastreadorDeArquivos`: transforma "o sandbox vai abrir tal caminho" em marcas na exposição,
  com a mesma regra de confiança do catálogo.

`PEDIR_APROVACAO` ainda não tem aprovação humana por trás: o registro o trata como recusa
enquanto não houver aprovador (peça 2 da Fase 4).
"""

import posixpath
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Protocol

from harness.host.confianca import (
    CONFIAVEL,
    EXTERNO,
    ErroManifesto,
    confianca_de,
    ler_manifesto,
)

_PREFIXO_DADOS: Final = "/dados/"


class _Propriedades(Protocol):
    """O que a política precisa saber de uma ferramenta (o `Ferramenta` do registro cumpre)."""

    dado_privado: bool
    conteudo_nao_confiavel: bool
    canal_externo: bool


class Decisao(Enum):
    """Resultado de `avaliar`."""

    PERMITIR = "permitir"
    PEDIR_APROVACAO = "pedir_aprovacao"
    RECUSAR = "recusar"


@dataclass
class Exposicao:
    """O que a sessão já viu. Mutável: o rastreador e o registro a atualizam."""

    viu_dado_privado: bool = False
    viu_nao_confiavel: bool = False

    def marcar_arquivo(self, confianca: str) -> None:
        """Abrir qualquer arquivo de /dados é ver dado privado; arquivo não `confiavel` é
        também conteúdo não confiável."""
        self.viu_dado_privado = True
        if confianca != CONFIAVEL:
            self.viu_nao_confiavel = True

    def marcar_ferramenta(self, ferramenta: _Propriedades) -> None:
        """Uma ferramenta que devolve dado privado ou conteúdo não confiável marca a sessão."""
        if ferramenta.dado_privado:
            self.viu_dado_privado = True
        if ferramenta.conteudo_nao_confiavel:
            self.viu_nao_confiavel = True

    def zerar(self) -> None:
        """Começa um pedido novo sem exposição."""
        self.viu_dado_privado = False
        self.viu_nao_confiavel = False


def avaliar(exposicao: Exposicao, ferramenta: _Propriedades) -> Decisao:
    """Decide sobre uma chamada. Soma a exposição às propriedades da própria ferramenta."""
    privado = exposicao.viu_dado_privado or ferramenta.dado_privado
    nao_confiavel = exposicao.viu_nao_confiavel or ferramenta.conteudo_nao_confiavel
    if ferramenta.canal_externo and privado and nao_confiavel:
        return Decisao.PEDIR_APROVACAO
    return Decisao.PERMITIR


class RastreadorDeArquivos:
    """Recebe o caminho que o sandbox vai abrir e marca a exposição.

    Chame com o caminho como o sandbox o vê (`/dados/<departamento>/...`). Devolve `None`
    quando a abertura pode seguir, ou uma mensagem curta quando deve ser bloqueada. A chave do
    manifesto é o caminho relativo a `/dados`, a mesma que o catálogo usa em `arquivo`.
    """

    def __init__(self, raiz: Path, exposicao: Exposicao) -> None:
        self._raiz = raiz
        self._exposicao = exposicao

    def __call__(self, caminho: str) -> str | None:
        if not caminho.startswith(_PREFIXO_DADOS) or posixpath.normpath(caminho) != caminho:
            return "caminho de arquivo fora do formato esperado"
        arquivo = caminho[len(_PREFIXO_DADOS) :]
        if not arquivo:
            return "caminho de arquivo fora do formato esperado"
        try:
            manifesto = ler_manifesto(self._raiz)
        except ErroManifesto:
            # Falha fechada: sem manifesto legível, o arquivo conta como não confiável.
            self._exposicao.marcar_arquivo(EXTERNO)
            return None
        self._exposicao.marcar_arquivo(confianca_de(manifesto, arquivo))
        return None
```

- [ ] **Passo 4: rodar**

Run: `uv run pytest tests/host/test_politica.py -q && uv run ruff check harness tests && uv run ruff format harness tests && uv run mypy harness tests`
Expected: passam. Se `ruff format` reformatar o teste, aceite.

- [ ] **Passo 5: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness/host/politica.py tests/host/test_politica.py
git commit -m "T20: politica da trifecta e rastreador de arquivos (modulo puro)"
```

---

### Task 3: Registro com política

**Files:**
- Modify: `harness/host/registry.py`
- Test: `tests/host/test_registro_politica.py`

**Depends on:** Task 2

**Interfaces:**
- Consumes: `politica.Exposicao`, `politica.avaliar`, `politica.Decisao`.
- Produces:
  - `Registro(limite_resultado_bytes=..., *, politica: bool = False, aprovador: Callable[[ToolCall, Ferramenta], bool] | None = None)`.
  - `Registro.exposicao: Exposicao` (atributo público, mesma instância durante a vida do registro).
  - `Registro.politica_ligada: bool` (propriedade).
  - `Registro.observar_politica(ao_decidir: Callable[[dict[str, Any]], None] | None) -> None`. O callback recebe `{"ferramenta": str, "decisao": str, "viu_dado_privado": bool, "viu_nao_confiavel": bool}`.
  - Constante `MENSAGEM_POLITICA: str`.

- [ ] **Passo 1: escrever os testes que falham** em `tests/host/test_registro_politica.py`

```python
"""Registro com a política da trifecta ligada ou desligada (Fase 4, peça 1)."""

from typing import Any

from pydantic import BaseModel, ConfigDict

from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import MENSAGEM_POLITICA, Nivel, Registro
from harness.sandbox.protocol import ToolCall


class Vazio(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _perfil() -> Perfil:
    return perfil_por_nome("Vendas").com_concessoes(frozenset({"teste.*"}))


def _registro(**opcoes: Any) -> tuple[Registro, list[str]]:
    chamadas: list[str] = []
    registro = Registro(**opcoes)

    @registro.ferramenta(
        nome="teste.privado", nivel=Nivel.LEITURA, descricao="x", args=Vazio, dado_privado=True
    )
    def privado(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("privado")
        return "ok"

    @registro.ferramenta(
        nome="teste.externa", nivel=Nivel.LEITURA, descricao="x", args=Vazio, canal_externo=True
    )
    def externa(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("externa")
        return "ok"

    @registro.ferramenta(
        nome="teste.suja",
        nivel=Nivel.LEITURA,
        descricao="x",
        args=Vazio,
        conteudo_nao_confiavel=True,
    )
    def suja(args: Vazio, perfil: Perfil) -> Any:
        chamadas.append("suja")
        return "ok"

    @registro.ferramenta(
        nome="teste.suja_que_falha",
        nivel=Nivel.LEITURA,
        descricao="x",
        args=Vazio,
        conteudo_nao_confiavel=True,
    )
    def suja_que_falha(args: Vazio, perfil: Perfil) -> Any:
        raise RuntimeError("segredo em /Users/alguem")

    return registro, chamadas


def _chamar(registro: Registro, nome: str) -> Any:
    return registro.despachar(ToolCall(call_id="c1", name=nome, args={}), _perfil())


def test_politica_desligada_nunca_recusa_e_nao_rastreia() -> None:
    registro, chamadas = _registro()
    assert registro.politica_ligada is False
    assert _chamar(registro, "teste.privado").ok
    assert _chamar(registro, "teste.suja").ok
    assert _chamar(registro, "teste.externa").ok
    assert chamadas == ["privado", "suja", "externa"]
    assert registro.exposicao.viu_dado_privado is False
    assert registro.exposicao.viu_nao_confiavel is False


def test_politica_ligada_recusa_externa_depois_de_privado_e_nao_confiavel() -> None:
    registro, chamadas = _registro(politica=True)
    assert _chamar(registro, "teste.privado").ok
    assert _chamar(registro, "teste.suja").ok
    resultado = _chamar(registro, "teste.externa")
    assert resultado.ok is False
    assert resultado.error == MENSAGEM_POLITICA
    assert "externa" not in chamadas


def test_politica_ligada_permite_externa_sem_exposicao() -> None:
    registro, chamadas = _registro(politica=True)
    assert _chamar(registro, "teste.externa").ok
    assert chamadas == ["externa"]


def test_mensagem_de_recusa_nao_vaza_detalhe() -> None:
    assert "/" not in MENSAGEM_POLITICA
    assert "dados" not in MENSAGEM_POLITICA.lower().replace("enviados", "")


def test_aprovador_pode_liberar_a_chamada() -> None:
    registro, chamadas = _registro(politica=True, aprovador=lambda chamada, ferramenta: True)
    _chamar(registro, "teste.privado")
    _chamar(registro, "teste.suja")
    assert _chamar(registro, "teste.externa").ok
    assert "externa" in chamadas


def test_erro_em_ferramenta_nao_confiavel_ainda_marca_a_exposicao() -> None:
    registro, _ = _registro(politica=True)
    resultado = _chamar(registro, "teste.suja_que_falha")
    assert resultado.ok is False
    assert registro.exposicao.viu_nao_confiavel is True


def test_observador_recebe_cada_decisao() -> None:
    registro, _ = _registro(politica=True)
    eventos: list[dict[str, Any]] = []
    registro.observar_politica(eventos.append)
    _chamar(registro, "teste.privado")
    _chamar(registro, "teste.suja")
    _chamar(registro, "teste.externa")
    assert [e["decisao"] for e in eventos] == ["permitir", "permitir", "pedir_aprovacao"]
    assert eventos[-1] == {
        "ferramenta": "teste.externa",
        "decisao": "pedir_aprovacao",
        "viu_dado_privado": True,
        "viu_nao_confiavel": True,
    }


def test_observador_que_falha_nao_derruba_o_despacho() -> None:
    registro, _ = _registro(politica=True)

    def quebra(_: dict[str, Any]) -> None:
        raise RuntimeError("falha do consumidor")

    registro.observar_politica(quebra)
    assert _chamar(registro, "teste.externa").ok
```

- [ ] **Passo 2: rodar e ver falhar**

Run: `uv run pytest tests/host/test_registro_politica.py -q`
Expected: FAIL (`ImportError: MENSAGEM_POLITICA`).

- [ ] **Passo 3: implementar em `harness/host/registry.py`**

3a. Adicione aos imports: `from harness.host.politica import Decisao, Exposicao, avaliar` (e `Any` já existe).

3b. Depois de `LIMITE_RESULTADO_PADRAO`, acrescente:

```python
MENSAGEM_POLITICA: Final = (
    "a chamada foi recusada pela política de segurança: a sessão leu conteúdo não confiável "
    "e esta ferramenta envia dados para fora"
)
```

3c. Troque o `__init__` por (mantendo a validação existente de `limite_resultado_bytes`):

```python
def __init__(
    self,
    limite_resultado_bytes: int = LIMITE_RESULTADO_PADRAO,
    *,
    politica: bool = False,
    aprovador: Callable[[ToolCall, "Ferramenta"], bool] | None = None,
) -> None:
    # (validação de limite_resultado_bytes igual à atual)
    self._limite_resultado = limite_resultado_bytes
    self._ferramentas: dict[str, Ferramenta] = {}
    self._politica = politica
    # Sem aprovação humana ainda, "pedir aprovação" equivale a recusar.
    self._aprovador = aprovador if aprovador is not None else _nunca_aprova
    self.exposicao = Exposicao()
    self._ao_decidir: Callable[[dict[str, Any]], None] | None = None


@property
def politica_ligada(self) -> bool:
    """Verdadeiro quando a política da trifecta vale para este registro."""
    return self._politica


def observar_politica(self, ao_decidir: Callable[[dict[str, Any]], None] | None) -> None:
    """Registra o callback que recebe cada decisão da política (para o trace)."""
    self._ao_decidir = ao_decidir


def _notificar(self, nome: str, decisao: Decisao) -> None:
    if self._ao_decidir is None:
        return
    try:
        self._ao_decidir(
            {
                "ferramenta": nome,
                "decisao": decisao.value,
                "viu_dado_privado": self.exposicao.viu_dado_privado,
                "viu_nao_confiavel": self.exposicao.viu_nao_confiavel,
            }
        )
    except Exception:
        # Falha do consumidor nunca muda o resultado da chamada.
        pass
```

3d. Antes da classe `Registro`, acrescente:

```python
def _nunca_aprova(chamada: ToolCall, ferramenta: "Ferramenta") -> bool:
    return False
```

3e. Em `_despachar`, logo depois do bloco `if ferramenta.nivel is not Nivel.LEITURA: ...` e antes do `try: resultado = ferramenta.funcao(...)`, insira:

```python
if self._politica:
    decisao = avaliar(self.exposicao, ferramenta)
    self._notificar(nome, decisao)
    if decisao is not Decisao.PERMITIR:
        liberada = decisao is Decisao.PEDIR_APROVACAO and self._aprovador(chamada, ferramenta)
        if not liberada:
            return _resultado_de_erro(chamada.call_id, MENSAGEM_POLITICA)
```

3f. Envolva a chamada da função com `finally`, para a exposição ser marcada mesmo quando a ferramenta falha:

```python
        try:
            resultado = ferramenta.funcao(argumentos, perfil)
        except ErroFerramenta as erro:
            ...  # blocos except existentes, sem mudança
        finally:
            if self._politica:
                self.exposicao.marcar_ferramenta(ferramenta)
```

(O `finally` vai depois do último `except` do mesmo `try`.)

- [ ] **Passo 4: rodar os testes novos e os do registro**

Run: `uv run pytest tests/host/test_registro_politica.py tests/host/test_registro.py -q && uv run ruff check harness tests && uv run ruff format harness tests && uv run mypy harness tests`
Expected: passam. Nenhum teste de `test_registro.py` precisa mudar.

- [ ] **Passo 5: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness/host/registry.py tests/host/test_registro_politica.py
git commit -m "T20: registro consulta a politica da trifecta"
```

---

### Task 4: Gancho de auditoria no sandbox

**Files:**
- Create: `harness/sandbox/rastreio.py`
- Modify: `harness/sandbox/protocol.py`
- Modify: `harness/sandbox/tools_client.py`
- Modify: `harness/sandbox/runner.py`
- Modify: `harness/sandbox/Dockerfile`
- Test: `tests/sandbox/test_rastreio.py`
- Test: `tests/sandbox/test_protocol.py`

**Interfaces:**
- Produces:
  - `protocol.ArquivoAberto(call_id: str, caminho: str)`, com `method == "arquivo_aberto"`, incluído em `Mensagem`.
  - `Ponte.avisar_abertura(caminho: str) -> None`: envia `ArquivoAberto`, espera o `ToolResult` do mesmo `call_id`, levanta `ToolError` se `ok` for falso ou o canal falhar. Não conta no limite de chamadas por célula.
  - `rastreio.criar_gancho(ponte: Ponte, raiz: str = "/dados") -> Callable[[str, tuple[object, ...]], None]` e `rastreio.instalar_gancho(ponte: Ponte, raiz: str = "/dados") -> None` (chama `sys.addaudithook`).
  - `runner.serve(entrada, saida, repl=None, rastrear=False)`; `main` aceita `--rastrear-arquivos`.

- [ ] **Passo 1: escrever os testes que falham**

Em `tests/sandbox/test_protocol.py`, acrescente:

```python
def test_arquivo_aberto_ida_e_volta() -> None:
    from harness.sandbox.protocol import ArquivoAberto, decode, encode

    mensagem = ArquivoAberto(call_id="c1", caminho="/dados/vendas/a.csv")
    assert decode(encode(mensagem)) == mensagem
```

Crie `tests/sandbox/test_rastreio.py`:

```python
"""Gancho de auditoria do sandbox: avisa o host antes de abrir arquivos sob /dados."""

import io
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from harness.sandbox.protocol import ArquivoAberto, ToolResult, decode, encode
from harness.sandbox.rastreio import criar_gancho
from harness.sandbox.tools_client import Ponte


class PonteFalsa:
    """Imita a Ponte: guarda os caminhos avisados e responde como configurado."""

    def __init__(self, erro: Exception | None = None) -> None:
        self.avisos: list[str] = []
        self._erro = erro

    def avisar_abertura(self, caminho: str) -> None:
        self.avisos.append(caminho)
        if self._erro is not None:
            raise self._erro


def _gancho(ponte: Any) -> Any:
    return criar_gancho(ponte, raiz="/dados")


def test_abertura_sob_dados_avisa_o_host() -> None:
    ponte = PonteFalsa()
    _gancho(ponte)("open", ("/dados/vendas/a.csv", "r", 0))
    assert ponte.avisos == ["/dados/vendas/a.csv"]


@pytest.mark.parametrize(
    "caminho", ["/tmp/x", "/saida/r.txt", "/proc/self/status", "/usr/lib/python3/os.py"]
)
def test_abertura_fora_de_dados_e_ignorada(caminho: str) -> None:
    ponte = PonteFalsa()
    _gancho(ponte)("open", (caminho, "r", 0))
    assert ponte.avisos == []


def test_outros_eventos_e_descritores_sao_ignorados() -> None:
    ponte = PonteFalsa()
    gancho = _gancho(ponte)
    gancho("socket.connect", (object(), ("1.2.3.4", 80)))
    gancho("open", (7, "r", 0))
    assert ponte.avisos == []


def test_caminho_com_ponto_ponto_e_resolvido_antes_de_avisar() -> None:
    ponte = PonteFalsa()
    _gancho(ponte)("open", ("/dados/vendas/../marketing/a.csv", "r", 0))
    assert ponte.avisos == ["/dados/marketing/a.csv"]


def test_falha_do_aviso_bloqueia_a_abertura() -> None:
    ponte = PonteFalsa(erro=RuntimeError("canal fechado"))
    with pytest.raises(PermissionError):
        _gancho(ponte)("open", ("/dados/vendas/a.csv", "r", 0))


def test_o_aviso_nao_dispara_o_gancho_de_novo() -> None:
    avisos: list[str] = []

    class Reentrante:
        def __init__(self) -> None:
            self.gancho: Any = None

        def avisar_abertura(self, caminho: str) -> None:
            avisos.append(caminho)
            # Se o próprio aviso abrir um arquivo de /dados, o gancho não pode recursar.
            self.gancho("open", ("/dados/x/y.csv", "r", 0))

    ponte = Reentrante()
    ponte.gancho = _gancho(ponte)
    ponte.gancho("open", ("/dados/a/b.csv", "r", 0))
    assert avisos == ["/dados/a/b.csv"]


def test_ponte_real_envia_arquivo_aberto_e_espera_a_liberacao() -> None:
    resposta = encode(ToolResult(call_id="c1", ok=True))
    entrada = io.StringIO(resposta)
    saida = io.StringIO()
    Ponte(entrada, saida).avisar_abertura("/dados/vendas/a.csv")
    enviada = decode(saida.getvalue())
    assert isinstance(enviada, ArquivoAberto)
    assert (enviada.call_id, enviada.caminho) == ("c1", "/dados/vendas/a.csv")


def test_ponte_real_levanta_quando_o_host_recusa() -> None:
    from harness.sandbox.tools_client import ToolError

    resposta = encode(ToolResult(call_id="c1", ok=False, error="caminho fora do formato"))
    ponte = Ponte(io.StringIO(resposta), io.StringIO())
    with pytest.raises(ToolError):
        ponte.avisar_abertura("/dados/x")


def test_gancho_instalado_no_processo_do_runner_bloqueia_leitura_sem_host() -> None:
    """Num subprocesso, com --rastrear-arquivos e sem host que responda, abrir /dados falha."""
    codigo = textwrap.dedent(
        """
        import io, sys
        from harness.sandbox.protocol import Exec, ExecDone, decode, encode
        from harness.sandbox.runner import serve

        celula = "try:\\n    open('/dados/x.txt').read()\\nexcept PermissionError:\\n    print('bloqueado')"
        entrada = io.StringIO(encode(Exec(cell_id=1, code=celula, timeout_s=5.0)))
        saida = io.StringIO()
        serve(entrada, saida, rastrear=True)
        for linha in saida.getvalue().splitlines():
            msg = decode(linha + "\\n")
            if isinstance(msg, ExecDone):
                print("STDOUT=" + msg.stdout.strip())
        """
    )
    processo = subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, timeout=60, check=False
    )
    assert "STDOUT=bloqueado" in processo.stdout, processo.stderr
```

(Por que o último teste funciona: com `entrada` esgotada, o `Ponte` do `serve` lê `""` ao esperar a resposta e levanta `ToolError`; o gancho converte em `PermissionError`. O gancho nunca foi instalado no processo do pytest, só no subprocesso.)

- [ ] **Passo 2: rodar e ver falhar**

Run: `uv run pytest tests/sandbox/test_rastreio.py tests/sandbox/test_protocol.py -q`
Expected: FAIL (`ImportError: ArquivoAberto` / `rastreio`).

- [ ] **Passo 3: protocolo.** Em `harness/sandbox/protocol.py`, depois de `ContextoOk`:

```python
class ArquivoAberto(_Mensagem):
    """sandbox → host: o código vai abrir um arquivo de /dados e espera a liberação.

    O host responde com um `tool_result` de mesmo `call_id`: `ok=True` libera a abertura;
    `ok=False` (com `error`) a bloqueia.
    """

    method: Literal["arquivo_aberto"] = "arquivo_aberto"
    call_id: str
    caminho: str
```

e amplie a união: `Mensagem = Exec | ToolCall | ToolResult | ExecDone | Contexto | ContextoOk | ArquivoAberto`.

- [ ] **Passo 4: ponte.** Em `harness/sandbox/tools_client.py`, importe `ArquivoAberto` junto dos outros e acrescente em `Ponte`, depois de `chamar`:

```python
    def avisar_abertura(self, caminho: str) -> None:
        """Avisa o host que o código vai abrir `caminho` e espera a liberação.

        Não conta no limite de chamadas por célula. Levanta ToolError se o host recusar ou o
        canal falhar: quem chama deve tratar isso como abertura bloqueada.
        """
        self._emitidas += 1
        call_id = f"c{self._emitidas}"
        quadro = encode(ArquivoAberto(call_id=call_id, caminho=caminho))
        try:
            self._saida.write(quadro)
            self._saida.flush()
        except (OSError, ValueError) as erro:
            raise ToolError("o canal com o host está fechado; a abertura não foi autorizada") from erro
        self._esperar_resultado(call_id, "abertura de arquivo")
```

- [ ] **Passo 5: gancho.** Crie `harness/sandbox/rastreio.py`:

```python
"""Gancho de auditoria do REPL: avisa o host antes de cada `open` sob /dados (Fase 4).

O host decide a confiança do arquivo e marca a exposição da sessão. O gancho falha fechado:
se o aviso não for liberado, a abertura levanta PermissionError. Um gancho de auditoria não
pode ser removido depois de instalado.

Limite assumido: o gancho não detém código que fuja do interpretador (por exemplo, `ctypes`).
É defesa em camadas; o isolamento forte é o contêiner e, depois, o gVisor.

Este módulo só importa o cliente da ponte: nada de `harness.host`.
"""

import os
import sys
import threading
from collections.abc import Callable
from typing import Any, Final

RAIZ_DADOS: Final = "/dados"


def criar_gancho(ponte: Any, raiz: str = RAIZ_DADOS) -> Callable[[str, tuple[object, ...]], None]:
    """Monta a função de auditoria. Separada de `instalar_gancho` para ser testável sem
    registrar nada no processo."""
    prefixo = raiz.rstrip("/") + "/"
    local = threading.local()

    def gancho(evento: str, argumentos: tuple[object, ...]) -> None:
        if evento != "open" or not argumentos:
            return
        alvo = argumentos[0]
        if not isinstance(alvo, str | bytes | os.PathLike):
            return
        try:
            caminho = os.path.realpath(os.fsdecode(alvo))
        except (TypeError, ValueError):
            return
        if not caminho.startswith(prefixo):
            return
        if getattr(local, "avisando", False):
            return
        local.avisando = True
        try:
            ponte.avisar_abertura(caminho)
        except Exception as erro:
            raise PermissionError("a leitura em /dados foi bloqueada pelo rastreio") from erro
        finally:
            local.avisando = False

    return gancho


def instalar_gancho(ponte: Any, raiz: str = RAIZ_DADOS) -> None:
    """Registra o gancho no processo atual. Não há como desfazer."""
    sys.addaudithook(criar_gancho(ponte, raiz))
```

- [ ] **Passo 6: runner.** Em `harness/sandbox/runner.py`:

6a. Importe `from harness.sandbox.rastreio import instalar_gancho`.

6b. Troque a assinatura e o início de `serve`. ATENÇÃO ao formato: a linha `def serve(...) -> None:` abaixo tem exatamente 100 caracteres, que é o limite do `ruff`; ela DEVE ficar em UMA linha só (o `ruff format --check` do gate reprova a versão quebrada em três linhas):

```python
def serve(entrada: TextIO, saida: TextIO, repl: Repl | None = None, rastrear: bool = False) -> None:
    ...docstring existente, mais: "Com `rastrear`, instala o gancho de auditoria de /dados."
    ponte = Ponte(entrada, saida)
    ambiente = repl if repl is not None else Repl(ponte)
    if rastrear:
        instalar_gancho(ponte)
    while True:
        ...
```

6c. `_serve_por_stdio(rastrear: bool = False)` passa `serve(entrada, saida, rastrear=rastrear)`.

6d. Em `main`, antes de `args = analisador.parse_args(argv)`, acrescente:

```python
    analisador.add_argument(
        "--rastrear-arquivos",
        action="store_true",
        help="instala o gancho que avisa o host antes de abrir arquivos de /dados",
    )
```

e passe `rastrear=args.rastrear_arquivos` às duas chamadas (`_serve_por_stdio(...)` e `serve(...)`).

- [ ] **Passo 7: Dockerfile.** Na linha `COPY harness/sandbox/protocol.py harness/sandbox/runner.py harness/sandbox/tools_client.py harness/sandbox/contexto.py /app/harness/sandbox/`, acrescente `harness/sandbox/rastreio.py` antes do destino.

- [ ] **Passo 8: rodar**

Run: `uv run ruff format harness tests && uv run pytest tests/sandbox -q -m "not docker" && uv run ruff check harness tests && uv run ruff format --check harness tests && uv run mypy harness tests`
Expected: passam, inclusive os testes antigos do runner e do protocolo. O `ruff format` (sem `--check`) vem primeiro, para gravar a formatação nos arquivos; o gate reprova a tarefa se `ruff format --check` ainda acusar algum arquivo.

- [ ] **Passo 9: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness/sandbox tests/sandbox
git commit -m "T20: gancho de auditoria avisa o host antes de abrir arquivos de /dados"
```

---

### Task 5: Sandbox do host entende o aviso

**Files:**
- Modify: `harness/sandbox/docker_sandbox.py`
- Modify: `harness/orchestrator/loop.py`
- Test: `tests/sandbox/test_docker_sandbox.py`

**Depends on:** Task 4

**Interfaces:**
- Consumes: `protocol.ArquivoAberto`.
- Produces:
  - `DockerSandbox(..., rastrear_arquivos: bool = False)`: com `True`, o contêiner sobe com `command=["--rastrear-arquivos"]`.
  - `DockerSandbox.executar_detalhado(cell_id, code, timeout_s, despachante=None, ao_abrir: Callable[[str], str | None] | None = None)`.
  - `_resposta_a_abertura(aviso: ArquivoAberto, ao_abrir) -> ToolResult` (função de módulo).
  - `SandboxLike.executar_detalhado` ganha o mesmo parâmetro opcional `ao_abrir`.

- [ ] **Passo 1: teste que falha** em `tests/sandbox/test_docker_sandbox.py` (acrescente ao fim; a função é pura, sem Docker):

```python
def test_resposta_a_abertura_libera_quando_o_rastreador_aceita() -> None:
    from harness.sandbox.docker_sandbox import _resposta_a_abertura
    from harness.sandbox.protocol import ArquivoAberto

    aviso = ArquivoAberto(call_id="c1", caminho="/dados/vendas/a.csv")
    resposta = _resposta_a_abertura(aviso, lambda caminho: None)
    assert (resposta.call_id, resposta.ok) == ("c1", True)


def test_resposta_a_abertura_bloqueia_com_a_mensagem_do_rastreador() -> None:
    from harness.sandbox.docker_sandbox import _resposta_a_abertura
    from harness.sandbox.protocol import ArquivoAberto

    aviso = ArquivoAberto(call_id="c2", caminho="/dados/x")
    resposta = _resposta_a_abertura(aviso, lambda caminho: "fora do formato")
    assert (resposta.ok, resposta.error) == (False, "fora do formato")


def test_resposta_a_abertura_sem_rastreador_bloqueia() -> None:
    from harness.sandbox.docker_sandbox import _resposta_a_abertura
    from harness.sandbox.protocol import ArquivoAberto

    resposta = _resposta_a_abertura(ArquivoAberto(call_id="c3", caminho="/dados/x"), None)
    assert resposta.ok is False


def test_resposta_a_abertura_com_rastreador_que_quebra_bloqueia() -> None:
    from harness.sandbox.docker_sandbox import _resposta_a_abertura
    from harness.sandbox.protocol import ArquivoAberto

    def quebra(caminho: str) -> str | None:
        raise RuntimeError("segredo em /Users/alguem")

    resposta = _resposta_a_abertura(ArquivoAberto(call_id="c4", caminho="/dados/x"), quebra)
    assert resposta.ok is False
    assert "/Users" not in (resposta.error or "")
```

- [ ] **Passo 2: rodar e ver falhar**

Run: `uv run pytest tests/sandbox/test_docker_sandbox.py -q -k resposta_a_abertura`
Expected: FAIL (`ImportError: _resposta_a_abertura`).

- [ ] **Passo 3: implementar em `harness/sandbox/docker_sandbox.py`**

3a. Importe `ArquivoAberto` do protocolo (junto de `ToolCall`).

3b. Construtor: novo parâmetro `rastrear_arquivos: bool = False` depois de `saida_mb`, com `self._rastrear = rastrear_arquivos`.

3c. Em `containers.create(...)`, acrescente o argumento `command=["--rastrear-arquivos"] if self._rastrear else None,` (o `ENTRYPOINT` da imagem já traz `--stdio`).

3d. `executar_detalhado`: novo parâmetro `ao_abrir: Callable[[str], str | None] | None = None` depois de `despachante` (e `executar` não muda). No laço, logo depois do bloco `if isinstance(msg, ToolCall): ... continue`, insira:

```python
            if isinstance(msg, ArquivoAberto):
                resposta_abertura = _resposta_a_abertura(msg, ao_abrir)
                self._enviar(encode(resposta_abertura).encode("utf-8"))
                continue
```

3e. Função de módulo, ao lado de `_resposta_a_ferramenta`:

```python
def _resposta_a_abertura(
    aviso: ArquivoAberto, ao_abrir: Callable[[str], str | None] | None
) -> ToolResult:
    """Resposta do host ao aviso de abertura de arquivo. Falha fechada em qualquer dúvida.

    Sem rastreador, ou se ele levantar exceção, a abertura é bloqueada. Só `None` do
    rastreador libera. A mensagem de bloqueio nunca carrega detalhe de exceção.
    """
    if ao_abrir is None:
        return ToolResult(
            call_id=aviso.call_id, ok=False, error="o rastreio de arquivos não está ligado"
        )
    try:
        erro = ao_abrir(aviso.caminho)
    except Exception:
        return ToolResult(
            call_id=aviso.call_id, ok=False, error="falha ao registrar a abertura do arquivo"
        )
    if erro is not None:
        return ToolResult(call_id=aviso.call_id, ok=False, error=erro)
    return ToolResult(call_id=aviso.call_id, ok=True)
```

3f. Em `harness/orchestrator/loop.py`, no `SandboxLike.executar_detalhado`, acrescente depois de `despachante`: `ao_abrir: Callable[[str], str | None] | None = None,`.

- [ ] **Passo 4: rodar**

Run: `uv run pytest tests/sandbox/test_docker_sandbox.py -q -m "not docker" && uv run ruff check harness tests && uv run ruff format harness tests && uv run mypy harness tests`
Expected: passam. Fakes de sandbox nos testes do orquestrador continuam válidos porque o novo parâmetro só é passado na Tarefa 6, e apenas quando existe rastreador.

- [ ] **Passo 5: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness/sandbox/docker_sandbox.py harness/orchestrator/loop.py tests/sandbox/test_docker_sandbox.py
git commit -m "T20: sandbox do host responde ao aviso de abertura de arquivo"
```

---

### Task 6: Orquestradores zeram a exposição e emitem a decisão

**Files:**
- Modify: `harness/orchestrator/loop.py`
- Modify: `harness/orchestrator/loop_json.py`
- Test: `tests/orchestrator/test_politica_loop.py`

**Depends on:** Task 3, Task 5

**Interfaces:**
- Consumes: `Registro.exposicao.zerar()`, `Registro.observar_politica(...)`, `Registro.politica_ligada`.
- Produces: `Orquestrador(..., rastreador: Callable[[str], str | None] | None = None)`; evento de trace `politica_decisao` com os campos do callback; `ao_abrir` repassado ao sandbox só quando há rastreador.

- [ ] **Passo 1: ler os fakes existentes.** Em `tests/orchestrator/`, abra o teste do loop (ex.: `test_loop.py`) e copie o fake de modelo e o fake de sandbox que ele usa. Os testes abaixo os reutilizam (nomes ilustrativos `ModeloRoteirizado` e `SandboxFalso`; use os nomes reais).

- [ ] **Passo 2: escrever os testes que falham** em `tests/orchestrator/test_politica_loop.py`:

```python
"""O orquestrador zera a exposição, emite `politica_decisao` e repassa o rastreador."""

from typing import Any

from harness.host.perfil import perfil_por_nome
from harness.host.politica import Decisao
from harness.host.registry import Registro
from harness.orchestrator.loop import Evento, Orquestrador

# Reutilize os fakes do teste do loop (ajuste os imports aos nomes reais).
from tests.orchestrator.test_loop import ModeloRoteirizado, SandboxFalso  # noqa: F401


def _rodar(registro: Registro, **opcoes: Any) -> tuple[list[Evento], Any]:
    eventos: list[Evento] = []
    sandbox = SandboxFalso()
    orquestrador = Orquestrador(
        ModeloRoteirizado(["```python\nFINAL('ok', [])\n```"]),
        sandbox,
        registro,
        perfil_por_nome("Vendas"),
        eventos=eventos.append,
        **opcoes,
    )
    orquestrador.executar_pedido("qualquer pedido")
    return eventos, sandbox


def test_exposicao_e_zerada_no_inicio_do_pedido() -> None:
    registro = Registro(politica=True)
    registro.exposicao.viu_dado_privado = True
    registro.exposicao.viu_nao_confiavel = True
    _rodar(registro)
    assert (registro.exposicao.viu_dado_privado, registro.exposicao.viu_nao_confiavel) == (
        False,
        False,
    )


def test_decisao_da_politica_vira_evento_de_trace() -> None:
    registro = Registro(politica=True)
    eventos, _ = _rodar(registro)
    registro.exposicao.viu_dado_privado = True
    registro.exposicao.viu_nao_confiavel = True
    registro._notificar("teste.externa", Decisao.PEDIR_APROVACAO)
    tipos = [e.tipo for e in eventos]
    assert "politica_decisao" in tipos
    evento = next(e for e in eventos if e.tipo == "politica_decisao")
    assert evento.dados["ferramenta"] == "teste.externa"
    assert evento.dados["decisao"] == "pedir_aprovacao"


def test_rastreador_so_chega_ao_sandbox_quando_existe() -> None:
    _, sem = _rodar(Registro())
    assert all(chamada.get("ao_abrir") is None for chamada in sem.chamadas_executar)
    rastreador = lambda caminho: None  # noqa: E731
    _, com = _rodar(Registro(politica=True), rastreador=rastreador)
    assert any(chamada.get("ao_abrir") is rastreador for chamada in com.chamadas_executar)
```

Observação para quem implementa: o fake `SandboxFalso` precisa registrar os argumentos de `executar_detalhado` em `chamadas_executar` (lista de dicts com as chaves nomeadas); se o fake do teste do loop ainda não faz isso, amplie-o no próprio arquivo novo com uma subclasse. O segundo teste acima, como escrito, chama `_notificar` direto; troque-o por uma chamada real de ferramenta com canal externo se o fake permitir, para o teste exercitar o caminho completo.

- [ ] **Passo 3: rodar e ver falhar**

Run: `uv run pytest tests/orchestrator/test_politica_loop.py -q`
Expected: FAIL (`TypeError` por `rastreador` ou ausência do evento).

- [ ] **Passo 4: implementar em `harness/orchestrator/loop.py`**

4a. No `__init__` do `Orquestrador`, novo parâmetro nomeado `rastreador: Callable[[str], str | None] | None = None` (depois de `eventos`), com `self._rastreador = rastreador`.

4b. Em `_laco`, logo depois de `estado.despachante = ...`:

```python
        # Pedido novo, exposição nova; cada decisão da política vira evento de trace.
        self._registro.exposicao.zerar()
        self._registro.observar_politica(self._emitir_politica)
```

4c. Método novo, ao lado de `_emitir`:

```python
    def _emitir_politica(self, dados: dict[str, Any]) -> None:
        self._emitir("politica_decisao", **dados)
```

4d. Na chamada a `executar_detalhado` (hoje `self._sandbox.executar_detalhado(cell_id, codigo, timeout_s=timeout, despachante=estado.despachante)`), passe o rastreador só quando existir, para não quebrar fakes antigos:

```python
            opcoes: dict[str, Any] = {}
            if self._rastreador is not None:
                opcoes["ao_abrir"] = self._rastreador
            execucao = self._sandbox.executar_detalhado(
                cell_id, codigo, timeout_s=timeout, despachante=estado.despachante, **opcoes
            )
```

- [ ] **Passo 5: implementar em `harness/orchestrator/loop_json.py`.** No início de `executar_pedido` (antes do primeiro turno do modelo), acrescente `self._registro.exposicao.zerar()` e `self._registro.observar_politica(self._emitir_politica)`, e o mesmo método `_emitir_politica` ao lado de `_emitir`. Não há rastreador nessa variante.

- [ ] **Passo 6: rodar**

Run: `uv run pytest tests/orchestrator -q && uv run ruff check harness tests && uv run ruff format harness tests && uv run mypy harness tests`
Expected: passam, incluindo todos os testes antigos dos dois laços.

- [ ] **Passo 7: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness/orchestrator tests/orchestrator/test_politica_loop.py
git commit -m "T20: orquestradores zeram a exposicao e emitem politica_decisao"
```

---

### Task 7: Configuração, relatório e CLIs

**Files:**
- Modify: `harness/evals/executor.py`
- Modify: `harness/evals/relatorio.py`
- Modify: `harness/evals/ferramentas_json.py`
- Modify: `harness/evals/__main__.py`
- Modify: `harness/cli.py`
- Test: `tests/evals/test_politica_trifecta.py`

**Depends on:** Task 3, Task 5, Task 6

**Interfaces:**
- Produces:
  - `ConfigExecucao.politica_trifecta: bool = False`.
  - `registro_json(raiz, variante, *, politica: bool = False)`.
  - `Relatorio.politica_trifecta: bool = False` (some do JSON quando `False`) e `agregar(..., politica_trifecta: bool = False)`.
  - Flag `--politica-trifecta` em `python -m harness.evals` e em `python -m harness`.
  - `--salvar-linha-de-base` recusa a flag.

- [ ] **Passo 1: escrever os testes que falham** em `tests/evals/test_politica_trifecta.py`:

```python
"""`--politica-trifecta` nas avaliações: configuração, relatório e recusa na linha de base."""

import io
import json
from pathlib import Path

import pytest

from harness.evals.__main__ import CODIGO_USO, _parser, main
from harness.evals.executor import ConfigExecucao
from harness.evals.ferramentas_json import registro_json
from harness.evals.relatorio import agregar


def test_configuracao_padrao_nao_liga_a_politica() -> None:
    assert ConfigExecucao().politica_trifecta is False


def test_parser_aceita_a_flag() -> None:
    assert _parser().parse_args(["--politica-trifecta"]).politica_trifecta is True
    assert _parser().parse_args([]).politica_trifecta is False


def test_registro_json_repassa_a_politica(tmp_path: Path) -> None:
    assert registro_json(tmp_path, "json").politica_ligada is False
    assert registro_json(tmp_path, "json", politica=True).politica_ligada is True


def test_relatorio_sem_politica_nao_tem_o_campo_no_json() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1)
    assert "politica_trifecta" not in json.loads(relatorio.model_dump_json())


def test_relatorio_com_politica_marca_o_campo() -> None:
    relatorio = agregar([], {}, semente=42, rodadas_por_caso=1, politica_trifecta=True)
    assert json.loads(relatorio.model_dump_json())["politica_trifecta"] is True


def test_linha_de_base_recusa_a_politica(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    saida = io.StringIO()
    codigo = main(
        ["--politica-trifecta", "--salvar-linha-de-base", "x", "--saida", str(tmp_path)],
        saida=saida,
    )
    assert codigo == CODIGO_USO
    assert "--politica-trifecta" in saida.getvalue()
```

- [ ] **Passo 2: rodar e ver falhar**

Run: `uv run pytest tests/evals/test_politica_trifecta.py -q`
Expected: FAIL.

- [ ] **Passo 3: implementar.**

3a. `harness/evals/executor.py`: em `ConfigExecucao`, depois de `com_ataques_avancados`:

```python
    # Liga a política da trifecta (Fase 4): recusa ferramenta com canal externo depois de a
    # sessão ver dado privado e conteúdo não confiável, e liga o rastreio de arquivos no REPL.
    # A execução não é comparável a linhas de base existentes.
    politica_trifecta: bool = False
```

3b. No mesmo arquivo, na rodada REPL: `registro = Registro()` vira `registro = Registro(politica=config.politica_trifecta)`; ao criar o `Orquestrador`, acrescente `rastreador=RastreadorDeArquivos(raiz, registro.exposicao) if config.politica_trifecta else None,` e importe `from harness.host.politica import RastreadorDeArquivos`. Na variante JSON: `registro_j = registro_json(raiz, config.variante, politica=config.politica_trifecta)`. Em `criar_docker`: `DockerSandbox(imagem=config.imagem, dados=montagens, rastrear_arquivos=config.politica_trifecta)`. Na chamada final a `agregar(...)`, acrescente `politica_trifecta=config.politica_trifecta,`.

3c. `harness/evals/ferramentas_json.py`: `def registro_json(raiz: Path, variante: str, *, politica: bool = False) -> Registro:` e `registro = Registro(politica=politica)`.

3d. `harness/evals/relatorio.py`: campo `politica_trifecta: bool = False` em `Relatorio` (depois de `taxa_obediencia`), com o comentário "Execução com a política da trifecta ligada (Fase 4). Some do JSON quando False."; em `_CAMPOS_DE_ATAQUE_PADRAO` acrescente `"politica_trifecta": False,`; em `agregar`, parâmetro `politica_trifecta: bool = False` (depois de `ataques`) e `politica_trifecta=politica_trifecta,` na construção.

3e. `harness/evals/__main__.py`: `parser.add_argument("--politica-trifecta", action="store_true", help="liga a política da trifecta e o rastreio de arquivos (Fase 4); não vale para linha de base")`; em `_config(args)` passe `politica_trifecta=args.politica_trifecta` ao `ConfigExecucao`; em `_validar_linha_de_base`, junto das outras recusas, acrescente:

```python
    if args.politica_trifecta:
        raise ErroDeUso("linha de base não aceita --politica-trifecta: a execução não é comparável")
```

3f. `harness/cli.py`: `parser.add_argument("--politica-trifecta", action="store_true", help="liga a política da trifecta e o rastreio de arquivos")`; em `main`, `registro = Registro(politica=args.politica_trifecta)`; no `Orquestrador(...)` acrescente `rastreador=RastreadorDeArquivos(args.dados, registro.exposicao) if args.politica_trifecta else None,`; na criação do sandbox, quando a flag estiver ligada, chame `fabrica(args.imagem, dados=montagens_do_perfil(perfil, args.dados), rastrear_arquivos=True)`, e sem a flag mantenha a chamada atual (assim as fábricas falsas dos testes antigos seguem válidas).

- [ ] **Passo 4: rodar**

Run: `uv run pytest tests/evals tests/test_cli.py tests/test_cli_mcp.py -q -m "not docker" && uv run ruff check harness tests && uv run ruff format harness tests && uv run mypy harness tests`
Expected: passam, e nenhum relatório existente muda de bytes.

- [ ] **Passo 5: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add harness tests
git commit -m "T20: --politica-trifecta nas avaliacoes e na CLI, com marca no relatorio"
```

---

### Task 8: Integração com Docker, documentação e gates

**Files:**
- Create: `tests/sandbox/test_docker_rastreio.py`
- Modify: `docs/especificacao-v0.md`
- Modify: `docs/diagramas-seguranca.md`

**Depends on:** Task 7

**Interfaces:**
- Consumes: tudo das tarefas 1 a 7.

- [ ] **Passo 1: escrever o teste de integração** em `tests/sandbox/test_docker_rastreio.py`:

```python
"""Rastreio de arquivos e política da trifecta ponta a ponta, com Docker (marcador `docker`)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from harness.host.perfil import Perfil, montagens_do_perfil, perfil_por_nome
from harness.host.politica import RastreadorDeArquivos
from harness.host.registry import Nivel, Registro
from harness.host.tools import registrar_todas
from harness.sandbox.docker_sandbox import DockerSandbox

pytestmark = pytest.mark.docker


class Vazio(BaseModel):
    model_config = ConfigDict(extra="forbid")


@pytest.fixture
def raiz(tmp_path: Path) -> Path:
    raiz = tmp_path / "dados"
    (raiz / "vendas").mkdir(parents=True)
    (raiz / "vendas" / "interno.csv").write_text("a\n1\n", encoding="utf-8")
    (raiz / "vendas" / "externo.txt").write_text("texto de fora\n", encoding="utf-8")
    (raiz / "manifesto.json").write_text('{"vendas/externo.txt": "externo"}', encoding="utf-8")
    return raiz


def _registro(raiz: Path, politica: bool) -> Registro:
    registro = Registro(politica=politica)
    registrar_todas(registro, raiz)

    @registro.ferramenta(
        nome="teste.enviar", nivel=Nivel.LEITURA, descricao="envia", args=Vazio, canal_externo=True
    )
    def enviar(args: Vazio, perfil: Perfil) -> Any:
        return "enviado"

    return registro


def _executar(
    raiz: Path,
    criar_sandbox: Callable[..., DockerSandbox],
    codigo: str,
    politica: bool,
) -> dict[str, Any]:
    perfil = perfil_por_nome("Vendas").com_concessoes(frozenset({"teste.*"}))
    registro = _registro(raiz, politica)
    sandbox = criar_sandbox(dados=montagens_do_perfil(perfil, raiz), rastrear_arquivos=politica)
    rastreador = RastreadorDeArquivos(raiz, registro.exposicao) if politica else None
    opcoes: dict[str, Any] = {"ao_abrir": rastreador} if rastreador is not None else {}
    resultado = sandbox.executar_detalhado(
        1, codigo, 30.0, despachante=registro.despachante(perfil), **opcoes
    )
    return {
        "stdout": resultado.done.stdout,
        "excecao": resultado.done.exception,
        "viu_nao_confiavel": registro.exposicao.viu_nao_confiavel,
    }


CODIGO = """
import json
open('/dados/vendas/externo.txt').read()
try:
    tools.teste.enviar()
    print(json.dumps('enviou'))
except Exception as erro:
    print(json.dumps('recusou'))
"""


def test_com_a_politica_ligada_ler_externo_e_enviar_e_recusado(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    saida = _executar(raiz, criar_sandbox, CODIGO, politica=True)
    assert json.loads(saida["stdout"].strip().splitlines()[-1]) == "recusou"
    assert saida["viu_nao_confiavel"] is True


def test_com_a_politica_desligada_o_mesmo_codigo_envia(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    saida = _executar(raiz, criar_sandbox, CODIGO, politica=False)
    assert json.loads(saida["stdout"].strip().splitlines()[-1]) == "enviou"


def test_ler_so_arquivo_confiavel_nao_impede_o_envio(
    raiz: Path, criar_sandbox: Callable[..., DockerSandbox]
) -> None:
    codigo = CODIGO.replace("externo.txt", "interno.csv")
    saida = _executar(raiz, criar_sandbox, codigo, politica=True)
    assert json.loads(saida["stdout"].strip().splitlines()[-1]) == "enviou"
    assert saida["viu_nao_confiavel"] is False
```

- [ ] **Passo 2: rodar o teste de Docker**

Run: `uv run pytest tests/sandbox/test_docker_rastreio.py -q`
Expected: passam (a imagem é reconstruída pela fixture `imagem` e já leva `rastreio.py`). Se `tools.teste.enviar` não for acessível por causa da concessão, confira que o perfil usado tem `teste.*`.

- [ ] **Passo 3: documentação.** Em `docs/especificacao-v0.md`:
  - Regra de segurança 3: troque a frase final de estado por: "*Estado: o rastreio por gancho de auditoria e a política (`--politica-trifecta`, desligada por padrão) estão implementados; a decisão `pedir_aprovacao` vale como recusa até a aprovação humana (Fase 4, peça 2).*"
  - Tabela de estado, linha `4. Segurança`: separe em duas linhas, "Rastreio de arquivos abertos e política da trifecta (spec de 27/11/2026)" como **Implementado**, e "Aprovação humana, gVisor" como **Não iniciado**.
  - Em `docs/diagramas-seguranca.md`, acrescente um diagrama `mermaid` do fluxo: o código abre um arquivo em `/dados`, o gancho avisa o host, o host marca a exposição, e a chamada de uma ferramenta com canal externo é recusada.

- [ ] **Passo 4: gates e suíte completa (sem execuções reais em paralelo)**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy harness tests && uv run pytest -q -p no:cacheprovider`
Expected: lint e tipos limpos; a suíte passa inteira (2296 antes desta peça, mais os testes novos), sem falhas nem contêineres sobrando.

- [ ] **Passo 5: ponto de salvamento sugerido (só com ordem do usuário)**

```bash
git add tests/sandbox/test_docker_rastreio.py docs
git commit -m "T20: teste de integracao com Docker e documentacao da politica da trifecta"
```

---

## Autorrevisão

**Cobertura da spec.** Estado da sessão e `avaliar` (Tarefa 2); registro, aprovador, observador, mensagem sem detalhe e evento `politica_decisao` (Tarefas 3 e 6); gancho com falha fechada, recursão, `/dados` apenas, mensagem de protocolo própria (Tarefas 4 e 5); confiança compartilhada com o catálogo (Tarefa 1); variante JSON sem gancho (Tarefa 6); desligada por padrão e `--politica-trifecta` com marca no relatório e recusa na linha de base (Tarefa 7); testes unitários e de integração (Tarefas 1 a 8). A mitigação de desempenho por cache de caminhos já reportados ficou **de fora** de propósito (YAGNI): a spec a condiciona a uma medição que só acontece depois.

**Ajuste em relação à spec.** A spec dizia que o orquestrador cria a `Exposicao` de cada pedido. O plano a deixa no `Registro` (que já é uma instância por sessão) e o orquestrador chama `exposicao.zerar()` no início do pedido. Mesmo efeito, menos fiação.

**Pontos que exigem atenção de quem executa.**
- A Tarefa 6 depende dos fakes do teste do loop; os nomes `ModeloRoteirizado` e `SandboxFalso` no plano são ilustrativos e precisam ser trocados pelos reais.
- A Tarefa 7 espelha o encadeamento de `com_ataques_avancados`: se o executor tiver outro ponto de montagem do relatório além do indicado, aplique a mesma passagem de `politica_trifecta`.
- O teste do subprocesso na Tarefa 4 confia em `Ponte` ler `""` quando a entrada acaba. Se o comportamento mudar, troque por uma ponte que levante `ToolError`.
