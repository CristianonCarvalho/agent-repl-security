# Fluxo do pedido, protocolo e módulos

Diagramas de como um pedido anda do começo ao fim: o loop do orquestrador, o protocolo da ponte, o contexto, o cliente do modelo, o trace e o mapa de módulos. Completam os diagramas de [ferramentas, MCP e permissões](diagramas-ferramentas-e-permissoes.md) e de [segurança e defesa](diagramas-seguranca.md), e descrevem o código das Fases 1 e 2 (T1 a T13).

Os diagramas são Mermaid e aparecem renderizados no GitHub.

## 1. Um pedido, de ponta a ponta

O caminho feliz: o modelo escreve código, o sandbox executa, uma ferramenta do host é chamada, e o `FINAL` encerra o pedido.

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuário
    participant C as CLI
    participant O as Orquestrador
    participant M as Modelo (OpenRouter)
    participant S as Sandbox (contêiner)
    participant R as Registro (host)

    U->>C: pedido, perfil, dados e opções
    C->>O: monta orquestrador, sandbox e registro
    O->>O: prompt de sistema com os grupos do perfil
    O->>S: carregar contexto, se houver
    loop até FINAL ou término por orçamento
        O->>M: histórico de mensagens
        M-->>O: raciocínio e um bloco python
        O->>O: verificação estática
        O->>S: exec (célula, timeout)
        S->>R: tool_call
        R-->>S: tool_result
        S-->>O: exec_done (stdout, stderr, exceção, final)
        O->>O: observação em saida_nao_confiavel
    end
    S-->>O: FINAL(resposta, fontes)
    O-->>C: ResultadoPedido
    C-->>U: resposta, fontes, motivo, tokens, custo e trace
    O->>S: fechar o contêiner
```

## 2. Estados de um pedido

Um pedido sempre termina com um destes motivos. Só `final` é um pedido completo.

```mermaid
stateDiagram-v2
    [*] --> Iniciado: executar_pedido
    Iniciado --> Carregando: há contexto
    Iniciado --> Turno: sem contexto
    Carregando --> Turno: contexto carregado
    Carregando --> ErroSandbox: falha ao carregar

    Turno --> Turno: célula executada, volta ao modelo
    Turno --> Turno: turno inválido, menos de 3 seguidos
    Turno --> Resumo: orçamento esgotado
    Turno --> Final: FINAL com fontes
    Turno --> ErroLLM: modelo falhou após as repetições
    Turno --> ErroSandbox: reinício do REPL falhou
    Turno --> ErroIrrecuperavel: 3 turnos inválidos seguidos

    Resumo --> Orcamento: texto do resumo vira a resposta
    Final --> [*]: motivo final, completo
    Orcamento --> [*]: orcamento_iteracoes, tokens ou tempo
    ErroLLM --> [*]: erro_llm
    ErroSandbox --> [*]: erro_sandbox
    ErroIrrecuperavel --> [*]: erro_irrecuperavel

    note right of Final
        Só o FINAL produz completo verdadeiro.
        Os erros não têm resposta.
    end note
```

## 3. O que acontece em cada turno

Cada resposta do modelo passa por filtros baratos antes de chegar ao contêiner. Turno inválido não executa nada e conta para o limite de três seguidos.

```mermaid
flowchart TD
    INI(["Resposta do modelo"]) --> FMT{"Formato válido?<br/>um bloco python no máximo"}
    FMT -- "não" --> INV["Turno inválido<br/>erro de formato"]
    FMT -- "sim" --> COD{"Tem código?"}
    COD -- "não" --> INV2["Turno inválido<br/>pede um bloco de código"]
    COD -- "sim" --> EST{"Passa na verificação<br/>estática?"}
    EST -- "não" --> INV3["Turno inválido<br/>lista de problemas ao modelo"]
    EST -- "sim" --> TMP{"Ainda há tempo<br/>no pedido?"}
    TMP -- "não" --> RES["Vai direto ao resumo final"]
    TMP -- "sim" --> EXE["Executa a célula no sandbox<br/>timeout = menor entre 30 s e o tempo restante"]

    INV --> CNT{"3 inválidos<br/>seguidos?"}
    INV2 --> CNT
    INV3 --> CNT
    CNT -- "sim" --> ERR(["erro_irrecuperavel"])
    CNT -- "não" --> VOLTA(["Observação volta ao modelo"])

    EXE --> OK{"O sandbox<br/>respondeu?"}
    OK -- "sim, mesmo com exceção do código" --> ZER["Zera o contador de inválidos<br/>formata a observação"]
    OK -- "morreu ou estourou o prazo" --> REI["Reinicia o REPL e avisa o modelo<br/>sem o texto cru da exceção"]
    ZER --> FIN{"Tem FINAL?"}
    FIN -- "sim" --> OKF(["final, completo"])
    FIN -- "não" --> VOLTA
    REI --> VOLTA
```

## 4. Orçamento e resumo final

O orçamento é conferido antes de cada chamada ao modelo. Ao estourar, o pedido não é cortado em silêncio: o modelo recebe um último turno, sem código, para resumir o que conseguiu.

```mermaid
flowchart TD
    A(["Antes de cada chamada ao modelo"]) --> B{"Iterações >= 15?"}
    B -- "sim" --> X["orcamento_iteracoes"]
    B -- "não" --> C{"Tokens >= 200.000?"}
    C -- "sim" --> Y["orcamento_tokens"]
    C -- "não" --> D{"Tempo >= 300 s?"}
    D -- "sim" --> Z["orcamento_tempo"]
    D -- "não" --> OK(["Chama o modelo"])

    X --> R["Turno de resumo<br/>pede o que foi obtido e o que faltou"]
    Y --> R
    Z --> R
    R --> S{"O resumo veio?"}
    S -- "sim" --> T["Resposta é o texto do resumo<br/>completo falso, fontes vazias"]
    S -- "não" --> U["Resposta é um texto padrão em português"]
```

Com `max_iteracoes` igual a N, o modelo é chamado no máximo N+1 vezes (as N do laço e o resumo).

## 5. Protocolo da ponte

Mensagens JSON-RPC, uma por linha (NDJSON), entre o host e o runner dentro do contêiner. Cada método tem exatamente um modelo validado.

```mermaid
sequenceDiagram
    autonumber
    participant H as Host (DockerSandbox)
    participant R as Runner (contêiner)

    H->>R: contexto (valor)
    R-->>H: contexto_ok (tipo, tamanho)

    H->>R: exec (cell_id, code, timeout_s)
    Note over R: executa numa thread com limite de tempo
    R->>H: tool_call (call_id, name, args)
    H->>H: despachar no registro do host
    H-->>R: tool_result (call_id, ok, data ou error)
    R->>H: tool_call (outra chamada, até 50 por célula)
    H-->>R: tool_result
    R-->>H: exec_done (stdout, stderr, exception, duration_ms, final, reiniciar)

    Note over H,R: reiniciar verdadeiro: o host mata e recria o contêiner,<br/>e reenvia o contexto
```

```mermaid
classDiagram
    class Mensagem {
        jsonrpc 2.0
        method
    }
    class Exec {
        cell_id
        code
        timeout_s
    }
    class ToolCall {
        call_id
        name
        args
    }
    class ToolResult {
        call_id
        ok
        data
        error
    }
    class ExecDone {
        cell_id
        stdout
        stderr
        exception
        duration_ms
        final
        reiniciar
    }
    class RespostaFinal {
        answer
        sources
    }
    class Contexto {
        valor
    }
    class ContextoOk {
        tipo
        tamanho
    }
    Mensagem <|-- Exec
    Mensagem <|-- ToolCall
    Mensagem <|-- ToolResult
    Mensagem <|-- ExecDone
    Mensagem <|-- Contexto
    Mensagem <|-- ContextoOk
    ExecDone --> RespostaFinal : final
```

Direções: `exec` e `contexto` vão do host ao sandbox. `tool_call`, `exec_done` e `contexto_ok` vão do sandbox ao host. `tool_result` volta ao sandbox. Mensagem com campo extra, método desconhecido ou JSON quebrado vira erro de protocolo e o contêiner é descartado.

## 6. Contexto grande como variável

Entrada grande é dado do pedido, não código. O modelo vê só os metadados e uma prévia curta, e lê o resto com código.

```mermaid
sequenceDiagram
    autonumber
    participant U as Usuário
    participant C as CLI
    participant O as Orquestrador
    participant S as Sandbox
    participant M as Modelo

    U->>C: --contexto arquivo (json ou texto)
    C->>C: valida e confere o teto de 2 MiB
    C->>O: executar_pedido(pedido, contexto)
    O->>S: contexto (valor)
    S-->>O: contexto_ok (tipo, tamanho)
    O->>M: primeira mensagem com tipo, tamanho e prévia de 200 caracteres<br/>dentro de saida_nao_confiavel
    M->>S: código usa context e context_info()
    Note over S: o valor fica na variável context do REPL
    S-->>O: pedaços pequenos que o código imprimiu
    Note over O,S: em reinício do REPL, o host reenvia o contexto.<br/>As outras variáveis se perdem e a observação avisa.
```

## 7. Cliente do modelo: repetições e prazo

Duas defesas diferentes: repetição para falha transitória e prazo total para a chamada nunca prender o loop.

```mermaid
flowchart TD
    INI(["responder(mensagens)"]) --> TENT["Nova tentativa numa thread daemon<br/>timeout = menor entre 60 s e o prazo restante"]
    TENT --> RES{"Resultado"}
    RES -- "resposta" --> OK(["RespostaLLM<br/>texto, tokens, modelo, provedor, custo"])
    RES -- "401, 400 ou outro 4xx" --> E1(["ErroLLM genérico, sem repetir"])
    RES -- "429, timeout, conexão ou 5xx" --> TRA{"Restam tentativas<br/>e a espera cabe no prazo?"}
    RES -- "sem resposta até o fim do prazo" --> E2(["ErroLLM de prazo, sem repetir<br/>resultado tardio é descartado"])
    TRA -- "sim" --> ESP["Espera 1 s, 2 s, 4 s"] --> TENT
    TRA -- "não" --> E3(["ErroLLM com tipo e status<br/>sem corpo, cabeçalhos nem chave"])
```

O prazo total padrão é 120 s (`OPENROUTER_PRAZO_TOTAL`) e precisa ser maior ou igual ao timeout por tentativa (60 s). O orquestrador transforma qualquer `ErroLLM` em `erro_llm`, sem o texto da exceção.

## 8. `FINAL` com fontes obrigatórias

O `FINAL` é a única forma de um pedido terminar com sucesso, e ele só vale com uma resposta e pelo menos uma fonte.

```mermaid
flowchart TD
    A(["Código chama FINAL(answer, sources)"]) --> B{"answer é texto<br/>não vazio?"}
    B -- "não" --> E["Exceção ValueError<br/>com exemplo de uso"]
    B -- "sim" --> C{"sources é lista<br/>com pelo menos um item?"}
    C -- "não" --> E
    C -- "sim" --> D{"Cada fonte é texto<br/>não vazio?"}
    D -- "não" --> E
    D -- "sim" --> F["Grava a resposta no estado da célula<br/>e interrompe a célula"]
    F --> G["exec_done com final preenchido"]
    G --> H(["Orquestrador encerra com motivo final"])
    E --> I(["O modelo vê o erro e tenta de novo"])
```

Um `FINAL` guardado de uma célula anterior não grava nada na célula atual: cada célula tem o seu estado de `FINAL`.

## 9. Eventos do trace

Cada pedido grava um arquivo JSONL, uma linha por evento, com flush a cada linha. Um pedido interrompido deixa um trace parcial mas válido.

```mermaid
flowchart LR
    A["pedido_iniciado<br/>perfil, pedido, orçamento,<br/>hash do prompt, contexto"] --> LOOP

    subgraph LOOP["A cada iteração"]
        B["turno_modelo<br/>modelo, provedor, tokens, custo"] --> C["verificacao_estatica<br/>código e problemas"]
        C --> D["chamada_ferramenta<br/>nome, args, resultado, latência"]
        D --> E["execucao<br/>duração, ferramentas, exceção, final"]
        E --> F["observacao<br/>texto devolvido ao modelo"]
    end

    LOOP --> G["pedido_finalizado<br/>motivo, resposta, fontes, tokens"]
```

Toda linha traz `seq`, `ts`, `session_id`, `tipo` e `dados`. Falha de escrita nunca derruba o pedido: ela é contada em `falhas_de_escrita`, e o `seq` avança mesmo assim, então um buraco no arquivo sempre corresponde a uma falha. O trace não guarda a chave do OpenRouter nem o conteúdo do contexto (só tipo, tamanho e SHA-256).

## 10. Como a CLI monta tudo

```mermaid
flowchart TD
    A["Argumentos<br/>pedido, --perfil, --dados, --contexto, --mcp,<br/>--trace, --imagem, --max-iteracoes"] --> B["Lê .env e o ambiente<br/>OPENROUTER_API_KEY, OPENROUTER_MODELO, prazo"]
    B --> C{"Configuração válida?"}
    C -- "não" --> X(["Código de saída 2"])
    C -- "sim" --> D["Perfil e montagens de /dados"]
    D --> E["Registro com as ferramentas locais"]
    E --> F{"--mcp?"}
    F -- "sim" --> G["Inicia servidores MCP<br/>registra servidor.ferramenta<br/>concede só as de leitura"]
    F -- "não" --> H
    G --> H["Cria o sandbox e o trace"]
    H --> I["Cliente OpenRouter"]
    I --> J["Orquestrador.executar_pedido"]
    J --> K["Fecha sandbox e servidores MCP"]
    K --> L{"Terminou com FINAL?"}
    L -- "sim" --> OK(["Código de saída 0"])
    L -- "não" --> NO(["Código de saída 1"])
```

## 11. Mapa de módulos

Quem depende de quem. O sandbox é a única parte que roda no contêiner, e a imagem só leva o necessário.

```mermaid
flowchart TB
    CLI["cli.py<br/>composição e saída"]

    subgraph ORQ["orchestrator"]
        LOOP["loop.py"]
        EST["estatico.py"]
        OBS["observation.py"]
        PRM["prompt.py e sistema.md"]
        TRN["turn.py"]
    end

    subgraph HST["host"]
        REG["registry.py"]
        PER["perfil.py"]
        subgraph TLS["tools"]
            CAT["catalogo.py"]
            DSC["descoberta.py"]
        end
        subgraph MCP["mcp"]
            CFG["config.py"]
            CLM["cliente.py"]
            ADA["adaptador.py"]
        end
    end

    subgraph LLMP["llm"]
        BAS["base.py"]
        CNF["config.py"]
        OPR["openrouter.py"]
    end

    TEL["telemetry/trace.py"]

    subgraph SBXH["sandbox: lado do host"]
        DKR["docker_sandbox.py"]
    end

    subgraph SBXC["sandbox: dentro do contêiner"]
        RUN["runner.py"]
        TCL["tools_client.py"]
        CTX["contexto.py"]
        PRO["protocol.py"]
    end

    CLI --> LOOP
    CLI --> REG
    CLI --> OPR
    CLI --> DKR
    CLI --> TEL
    CLI --> MCP
    LOOP --> EST
    LOOP --> OBS
    LOOP --> PRM
    LOOP --> TRN
    LOOP --> BAS
    LOOP --> DKR
    LOOP --> REG
    PRM --> REG
    REG --> PER
    REG --> TLS
    ADA --> CLM
    ADA --> REG
    OPR --> BAS
    OPR --> CNF
    DKR --> PRO
    RUN --> PRO
    RUN --> TCL
    RUN --> CTX
    TCL --> PRO
    TEL --> LOOP
```

A imagem do sandbox leva só `runner`, `protocol`, `tools_client` e `contexto`. Nada de `harness/host`: o registro, as ferramentas e os perfis executam no host e nunca entram no contêiner.

## 12. Ciclo de vida de uma ferramenta, do cadastro à chamada

```mermaid
flowchart LR
    A["Ferramenta local<br/>ou servidor MCP"] --> B["Registro<br/>nome, parâmetros, nível, propriedades"]
    B --> C["Perfil concede?<br/>nome ou grupo.*"]
    C -- "sim" --> D["Visível: busca, prompt<br/>e verificação estática"]
    C -- "não" --> E["Invisível: responde como inexistente"]
    D --> F["Modelo descobre<br/>search_tools e describe_tool"]
    F --> G["Código chama tools.grupo.nome"]
    G --> H["Despachante: concessão, argumentos, nível"]
    H --> I["Função executa no host"]
    I --> J["Limite de 1 MiB e tool_result"]
    J --> K["Evento chamada_ferramenta no trace"]
```

## Onde cada parte está no código

| Assunto | Arquivo |
| --- | --- |
| Loop, orçamento, estados e resumo final | [`harness/orchestrator/loop.py`](../harness/orchestrator/loop.py) |
| Formato dos turnos do modelo | [`harness/orchestrator/turn.py`](../harness/orchestrator/turn.py) |
| Prompt de sistema | [`harness/orchestrator/prompt.py`](../harness/orchestrator/prompt.py) |
| Protocolo da ponte | [`harness/sandbox/protocol.py`](../harness/sandbox/protocol.py) |
| Cliente do sandbox, reinício e contexto | [`harness/sandbox/docker_sandbox.py`](../harness/sandbox/docker_sandbox.py) |
| Runner e `FINAL` | [`harness/sandbox/runner.py`](../harness/sandbox/runner.py) |
| Cliente OpenRouter | [`harness/llm/openrouter.py`](../harness/llm/openrouter.py) |
| Trace JSONL | [`harness/telemetry/trace.py`](../harness/telemetry/trace.py) |
| Composição e saída da CLI | [`harness/cli.py`](../harness/cli.py) |
