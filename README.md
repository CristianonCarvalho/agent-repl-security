# agent-repl-security

Harness de agente em que o modelo responde a pedidos escrevendo código Python, executado num REPL persistente e isolado (contêiner sem rede), que chama ferramentas do host por uma ponte controlada e devolve ao modelo só o resultado relevante.

Projeto de estudo, com dados sintéticos. Combina duas ideias: código como espaço de ações (CodeAct) e ferramentas como API de código carregada sob demanda (Code Mode). O foco é segurança: isolamento do sandbox, política contra a *lethal trifecta*, aprovação humana para escrita e defesa contra prompt injection.

## O que é um REPL

REPL (*Read-Eval-Print Loop*) é um interpretador interativo: lê um trecho de código, executa, imprime o resultado e mantém o estado (variáveis, arquivos de trabalho) entre uma execução e a próxima. É o que acontece ao abrir `python` no terminal.

Num harness REPL, a "ação" do modelo deixa de ser uma chamada JSON por ferramenta e passa a ser um bloco de código. Esse código pode chamar várias ferramentas, guardar resultados em variáveis e filtrar os dados antes de devolver ao modelo só o que importa. Dados intermediários e arquivos grandes ficam no REPL, fora do contexto do modelo.

Como o código vem do modelo, ele é tratado como hostil: roda num contêiner sem rede, e tudo que precisa do mundo externo atravessa uma ponte controlada pelo host.

## Visão geral

```mermaid
flowchart LR
    U([Usuário]) -->|pedido| O

    subgraph HOST["Host (zona confiável)"]
        O[Orquestrador<br/>loop, orçamento,<br/>verificação estática]
        L[Cliente LLM<br/>OpenRouter]
        P[Política<br/>trifecta, níveis de ferramenta]
        R[Registro de ferramentas<br/>catálogo, arquivos, entrega]
        A[Aprovação humana<br/>diff e versionamento]
        T[("Trace<br/>JSONL e OpenTelemetry")]
    end

    subgraph SBX["Sandbox (zona não confiável): contêiner sem rede"]
        X[REPL persistente<br/>executa o código do modelo]
        D["/dados<br/>somente leitura,<br/>por perfil"]
        S["/saida<br/>efêmero"]
    end

    O <-->|prompt e resposta| L
    O -->|exec: código| X
    X -->|exec_done: saída| O
    X --- D
    X --- S
    X -->|tool_call via ponte JSON-RPC| P
    P --> R
    P -.->|escrita ou sensível| A
    R -->|tool_result| X
    O --> T
    P --> T
    O -->|resposta final com fontes| U
```

O modelo emite um bloco de código por turno; o orquestrador o verifica e o envia ao sandbox; o sandbox executa e devolve a saída truncada. Toda chamada de ferramenta passa pela ponte e pela política no host antes de chegar ao registro. Alterações em documentos só são gravadas depois de diff, validação e aprovação humana. Detalhes na [especificação](docs/especificacao-v0.md).

## Estado

**Fase 1 (fundamentos) implementada**, com dados sintéticos e sem política de trifecta nem aprovação humana (Fase 4):

- Loop do orquestrador com orçamento (iterações, tokens, tempo), verificação estática e resumo final.
- REPL persistente em contêiner Docker endurecido (sem rede, sistema de arquivos somente leitura, usuário sem privilégios), com ponte JSON-RPC para as ferramentas do host.
- Registro de ferramentas com perfis por departamento e a ferramenta `catalogo.listar`.
- `FINAL(resposta, fontes)` com fontes obrigatórias, truncagem da saída e observações marcadas como não confiáveis.
- Trace JSONL por pedido, com chamadas de ferramenta, tokens, provedor e custo.
- Cliente OpenRouter (SDK `openai`), com repetição em falhas transitórias. Validado com DeepSeek V4.1 Flash num pedido real de ponta a ponta.

Para responder a um pedido pela linha de comando (precisa da imagem do sandbox e de `OPENROUTER_API_KEY` e `OPENROUTER_MODELO` no ambiente ou no `.env`):

```bash
uv run python -m harness "Qual o total gasto pelo cliente Acme nos pedidos de vendas? Cite a fonte." --perfil Vendas --dados ./dados
```

`--dados` aponta para a pasta com um subdiretório por departamento (`vendas/`, `marketing/`...). O perfil define quais departamentos o contêiner enxerga. Opções: `--trace` (pasta do trace JSONL, padrão `traces`), `--imagem` (padrão `harness-sandbox:dev`), `--max-iteracoes` e `--contexto`. A saída traz resposta, fontes, motivo, iterações, tokens, custo e o caminho do trace. O código de saída é 0 quando o pedido termina com `FINAL`, 1 quando não termina e 2 para uso ou configuração inválidos.

Entrada grande (documento, tabela, JSON) vai com `--contexto`. O arquivo `.json` vira objeto ou lista; qualquer outro é lido como texto UTF-8. O conteúdo fica na variável `context` do REPL, e o modelo só vê o tipo, o tamanho e uma prévia curta:

```bash
uv run python -m harness "Quantos pedidos há no arquivo e qual o maior valor?" --perfil Vendas --dados ./dados --contexto ./pedidos.csv
```

O limite do contexto é de 2 MiB (JSON canônico). Acima disso, o código de saída é 2.

### Servidores MCP (`--mcp`)

`--mcp ARQUIVO.json` liga servidores MCP por stdio. Cada ferramenta de um servidor vira `servidor.ferramenta` no REPL (`tools.eco.eco(texto="oi")`), e o nome do servidor é também o grupo que aparece no prompt. O arquivo tem um objeto `servidores`:

```json
{
  "servidores": {
    "eco": {
      "comando": "uv",
      "args": ["run", "servidor.py"],
      "env": {"EXEMPLO": "1"},
      "leitura": ["eco"],
      "timeout_chamada_s": 30,
      "timeout_inicio_s": 15
    }
  }
}
```

- `comando` e `args`: o processo do servidor. Ele recebe só `PATH`, `HOME` e as variáveis de `env`; o ambiente do harness (inclusive a chave do OpenRouter) não é repassado.
- `leitura`: nomes de ferramentas tratadas como leitura. As demais são de escrita externa, e a chamada é recusada porque não há aprovação humana ainda.
- Nome de servidor: letras minúsculas, dígitos e `_`, começando por letra. `catalogo` e `descoberta` são reservados. Nomes com dígito são aceitos na configuração, mas o registro não os aceita, então esse servidor não é iniciado e aparece num aviso.
- Um servidor que não inicia gera um aviso, e o pedido continua com as demais ferramentas. Todos os servidores são fechados ao fim do pedido.
- Concessões (D14): só as ferramentas listadas em `leitura` ficam visíveis e chamáveis pelo perfil. As demais (escrita externa) não aparecem na busca e são tratadas como ferramenta inexistente.

```bash
uv run python -m harness "Quanto vendemos?" --perfil Vendas --dados ./dados --mcp ./mcp.json
```

> **Aviso.** Ferramentas MCP são conteúdo não confiável e vêm de fora do harness (canal externo). O que elas devolvem entra na sessão como qualquer dado externo, e a política contra a *lethal trifecta* só chega na Fase 4. Use só servidores confiáveis e, por enquanto, só ferramentas de leitura.

### Desenvolvimento

Requer Python 3.12, [uv](https://docs.astral.sh/uv/) e Docker.

```bash
uv sync
docker build -t harness-sandbox:dev -f harness/sandbox/Dockerfile .
uv run ruff check . && uv run mypy harness tests && uv run pytest -q
```

O teste real contra o OpenRouter fica fora da rodada padrão e gasta tokens. Ele lê `OPENROUTER_API_KEY` e `OPENROUTER_MODELO` do ambiente ou de um `.env` (ver `.env.example`):

```bash
uv run pytest -m real_api
```

## Documentação

- [Especificação v0](docs/especificacao-v0.md)
- [Decisões de arquitetura (ADRs)](docs/adr/README.md)
