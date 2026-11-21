# Ferramentas, MCP e permissões

Diagramas de como o harness chama ferramentas, liga servidores MCP e decide o que cada perfil pode ver e executar. Descrevem o código da Fase 1 e da Fase 2 (T1 a T13). A aprovação humana e a política contra a *lethal trifecta* são da Fase 4 e ainda não existem.

Os diagramas são Mermaid e aparecem renderizados no GitHub.

## 1. Como uma chamada de ferramenta acontece

O modelo nunca chama uma ferramenta diretamente: o código dele roda no contêiner, e cada chamada atravessa a ponte até o host, onde o registro decide.

```mermaid
sequenceDiagram
    autonumber
    participant M as Modelo (LLM)
    participant O as Orquestrador (host)
    participant R as REPL (contêiner, sem rede)
    participant P as Ponte JSON-RPC
    participant G as Registro (host)
    participant F as Função da ferramenta

    M->>O: bloco de código com tools.grupo.nome(...)
    O->>O: verificação estática (só ferramentas visíveis ao perfil)
    O->>R: exec (célula)
    R->>P: tool_call (nome, args)
    P->>G: despachar(chamada, perfil)
    G->>G: concessão, argumentos e nível
    G->>F: função(args, perfil)
    F-->>G: dado
    G->>G: limite de tamanho (1 MiB)
    G-->>R: tool_result
    R-->>O: exec_done (stdout, stderr, exceção)
    O-->>M: observação dentro de saida_nao_confiavel
    Note over O: cada chamada vira o evento<br/>chamada_ferramenta no trace
```

## 2. Ferramentas locais e servidores MCP

As ferramentas locais e as de MCP vivem no mesmo registro. O sandbox não sabe a diferença, e os servidores MCP rodam no host, nunca no contêiner.

```mermaid
flowchart LR
    subgraph SBX["Sandbox (zona não confiável): contêiner sem rede"]
        COD["Código do modelo<br/>tools.grupo.nome(...)"]
        APE["search_tools e describe_tool<br/>(apelidos no REPL)"]
    end

    subgraph HOST["Host (zona confiável)"]
        PON["Ponte JSON-RPC"]
        REG["Registro de ferramentas<br/>concessões, validação, nível, limite"]
        CAT["catalogo.listar<br/>(local)"]
        DES["descoberta.buscar e descoberta.descrever<br/>(locais)"]
        ADA["Adaptador MCP<br/>servidor.ferramenta"]
        CLI["Cliente MCP<br/>JSON-RPC por stdio, escrito à mão"]
    end

    subgraph EXT["Fora do harness"]
        S1["Servidor MCP 1<br/>subprocesso"]
        S2["Servidor MCP 2<br/>subprocesso"]
    end

    COD -->|tool_call| PON
    APE -->|tool_call| PON
    PON --> REG
    REG --> CAT
    REG --> DES
    REG --> ADA
    ADA --> CLI
    CLI -->|stdin e stdout| S1
    CLI -->|stdin e stdout| S2
```

## 3. Como um servidor MCP entra no registro

Acontece uma vez por execução, antes do pedido, só para os servidores que você declara em `--mcp`. Nada é descoberto sozinho.

```mermaid
sequenceDiagram
    autonumber
    participant U as Você (arquivo --mcp)
    participant C as CLI
    participant A as Adaptador
    participant K as Cliente MCP
    participant S as Servidor MCP
    participant G as Registro

    U->>C: servidores, comando, env e lista leitura
    C->>A: registrar_servidores_mcp(registro, config)
    A->>K: iniciar subprocesso
    Note right of K: ambiente mínimo: PATH, HOME<br/>e o env configurado.<br/>A chave do OpenRouter não passa.
    K->>S: initialize e initialized
    K->>S: tools/list
    S-->>K: ferramentas com esquema JSON
    K-->>A: lista
    A->>A: valida nomes, monta o modelo de argumentos<br/>e neutraliza a descrição
    A->>G: registra servidor.ferramenta
    Note right of G: nível LEITURA se estiver em leitura,<br/>senão ESCRITA_EXTERNA.<br/>Sempre conteúdo não confiável e canal externo.
    C->>C: concede ao perfil só as ferramentas LEITURA
    C->>C: roda o pedido
    C->>K: fechar (encerra os subprocessos)
```

## 4. Descoberta de ferramentas sob demanda

O prompt traz só os nomes dos grupos que o perfil enxerga. O modelo busca e descreve o resto por código.

```mermaid
sequenceDiagram
    autonumber
    participant M as Modelo
    participant R as REPL
    participant G as Registro
    participant D as descoberta.*

    M->>R: search_tools("eco", limit=5)
    R->>G: tool_call descoberta.buscar
    G->>D: buscar(query, limit)
    D->>D: filtra pelas ferramentas visíveis ao perfil
    D->>D: pontua por nome, descrição e parâmetros
    D-->>M: lista com nome e resumo de uma linha
    M->>R: describe_tool("eco.eco")
    R->>G: tool_call descoberta.descrever
    G->>D: descrever(name)
    D-->>M: parâmetros, tipos, nível e exemplo
    Note over D: ferramenta negada responde como inexistente,<br/>com a mesma mensagem
    M->>R: tools.eco.eco(texto="...")
```

## 5. Camadas de permissão

Nenhuma camada sozinha basta. Cada uma cobre uma falha possível das outras.

```mermaid
flowchart TB
    PED["Pedido com um perfil<br/>Vendas, Marketing, Financeiro ou Direção"]

    A["A. Montagem de dados<br/>só os departamentos do perfil entram em /dados, somente leitura<br/>controle estrutural"]
    B["B. Concessões do perfil<br/>decidem quais ferramentas o perfil enxerga<br/>busca, prompt e verificação estática"]
    C["C. Despachante do registro<br/>concessão, argumentos e nível<br/>nega por padrão"]
    D["D. Dentro da ferramenta<br/>filtro por departamento, PermissionError"]
    E["E. Limite de resultado<br/>1 MiB por chamada"]
    F["F. Observação não confiável<br/>saida_nao_confiavel com neutralização"]

    FUT["Ainda não existe (Fase 4)<br/>aprovação humana para escrita<br/>política contra a lethal trifecta"]

    PED --> A --> B --> C --> D --> E --> F
    C -.-> FUT
```

## 6. Decisão do despachante, passo a passo

Ordem real do `Registro.despachar`. Qualquer falha devolve um erro curto ao código do modelo, sem derrubar o pedido.

```mermaid
flowchart TD
    INI(["tool_call recebido"]) --> EXI{"A ferramenta existe<br/>e o perfil tem concessão?"}
    EXI -- "não" --> E1["erro: a ferramenta não existe<br/>(mesma mensagem para as duas causas)<br/>a função nunca é chamada"]
    EXI -- "sim" --> ARG{"Os argumentos<br/>são válidos?"}
    ARG -- "não" --> E2["erro: argumentos inválidos"]
    ARG -- "sim" --> NIV{"O nível é LEITURA?"}
    NIV -- "não" --> E3["erro: exige aprovação humana,<br/>que ainda não existe"]
    NIV -- "sim" --> EXE["executa a função da ferramenta"]
    EXE --> EXC{"A função levantou<br/>exceção?"}
    EXC -- "sim" --> E4["erro genérico<br/>(sem caminhos nem detalhes do host)"]
    EXC -- "não" --> TAM{"O resultado cabe em JSON<br/>e em até 1 MiB?"}
    TAM -- "não" --> E5["erro: resultado acima do limite"]
    TAM -- "sim" --> OK(["tool_result ok"])
```

## 7. Perfis e concessões

Os quatro perfis embutidos. As concessões base valem para todos.

| Perfil | Departamentos montados em `/dados` | Concessões |
| --- | --- | --- |
| Vendas | vendas, comercial | `catalogo.listar`, `descoberta.*` |
| Marketing | marketing | `catalogo.listar`, `descoberta.*` |
| Financeiro | financeiro, vendas, comercial | `catalogo.listar`, `descoberta.*` |
| Direção | vendas, comercial, marketing, financeiro, direcao | `catalogo.listar`, `descoberta.*` |

A CLI soma a isso as ferramentas MCP de nível `LEITURA` do arquivo `--mcp`. Uma concessão é um nome exato (`catalogo.listar`) ou um grupo inteiro (`descoberta.*`). Sem concessão, a ferramenta não existe para o perfil.

## 8. Nível e visibilidade de uma ferramenta MCP

O MCP não diz se uma ferramenta lê ou escreve. Por isso o padrão é o nível mais restrito, e só você o libera.

```mermaid
flowchart TD
    T["Ferramenta anunciada pelo servidor MCP"] --> L{"Está na lista leitura<br/>do servidor, no seu arquivo?"}
    L -- "sim" --> N1["nível LEITURA"]
    N1 --> G1["a CLI concede ao perfil"]
    G1 --> V1(["visível na busca<br/>e chamável"])
    L -- "não" --> N2["nível ESCRITA_EXTERNA"]
    N2 --> G2["a CLI não concede"]
    G2 --> V2(["invisível na busca<br/>e responde como inexistente"])
    N2 -.-> R["mesmo se fosse concedida,<br/>o despachante recusa<br/>até existir aprovação humana"]
```

## Onde cada parte está no código

| Assunto | Arquivo |
| --- | --- |
| Registro e despachante | [`harness/host/registry.py`](../harness/host/registry.py) |
| Perfis e concessões | [`harness/host/perfil.py`](../harness/host/perfil.py) |
| Descoberta de ferramentas | [`harness/host/tools/descoberta.py`](../harness/host/tools/descoberta.py) |
| Adaptador, cliente e configuração MCP | [`harness/host/mcp/`](../harness/host/mcp/) |
| Ponte no sandbox | [`harness/sandbox/tools_client.py`](../harness/sandbox/tools_client.py) |
| Decisão sobre concessões | [`docs/adr/D14-perfis-por-ferramenta.md`](adr/D14-perfis-por-ferramenta.md) |
