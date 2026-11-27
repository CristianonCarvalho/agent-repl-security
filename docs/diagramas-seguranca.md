# Segurança e defesa

Diagramas do sistema de defesa do harness: onde estão as fronteiras de confiança, o que cada camada impede, e o que ainda falta. Complementam os diagramas de [ferramentas, MCP e permissões](diagramas-ferramentas-e-permissoes.md), que cobrem o registro e as concessões.

Os diagramas descrevem o código das Fases 1 e 2 (T1 a T13). O que é da Fase 4 (aprovação humana, política contra a *lethal trifecta*, rastreio de arquivos abertos, gVisor) aparece marcado como **planejado** e ainda não existe.

Premissa do projeto: o código escrito pelo modelo é hostil, e tudo que ele lê (dados, resultados de ferramentas, saídas) pode conter instruções de um atacante. Por isso a defesa não depende de o modelo se comportar.

## 1. Zonas e fronteiras de confiança

O que o modelo escreve nunca roda no host. Só mensagens do protocolo e resultados de ferramentas atravessam a fronteira.

```mermaid
flowchart LR
    subgraph EXT["Fora do harness (não confiável)"]
        ATQ["Autor de documentos e dados<br/>pode plantar instruções"]
        MCP["Servidores MCP<br/>subprocessos de terceiros"]
        LLM["Provedor do modelo<br/>OpenRouter"]
    end

    subgraph HOST["Host (zona confiável)"]
        ORQ["Orquestrador<br/>orçamento, verificação estática,<br/>envelope da observação"]
        REG["Registro de ferramentas<br/>concessões, validação, nível, limite"]
        SEG["Segredos<br/>chave do OpenRouter só no cliente LLM"]
        TRC["Trace JSONL"]
    end

    subgraph SBX["Sandbox (zona hostil): contêiner Docker sem rede"]
        COD["Código do modelo no REPL"]
        DAD["/dados<br/>somente leitura, só os departamentos do perfil"]
    end

    ATQ -. "texto dentro dos dados" .-> DAD
    COD -- "tool_call (NDJSON)" --> REG
    REG -- "tool_result até 1 MiB" --> COD
    COD -- "exec_done: stdout, stderr, exceção" --> ORQ
    ORQ -- "observação em saida_nao_confiavel" --> LLM
    LLM -- "código Python" --> ORQ
    ORQ -- "exec, só se passar na verificação" --> COD
    REG -- "stdio, ambiente mínimo" --> MCP
    ORQ --> TRC
    SEG --> LLM
```

Fronteiras que existem e onde são impostas:

| Fronteira | Quem impõe |
| --- | --- |
| Modelo para host | Verificação estática, orçamento e envelope da observação (orquestrador) |
| Sandbox para host | Contêiner endurecido e protocolo com validação (cliente do sandbox) |
| Sandbox para ferramentas | Despachante do registro (concessões, argumentos, nível, limite) |
| Host para MCP | Ambiente mínimo do subprocesso e nível padrão restrito |
| Host para o provedor | Mensagens de erro genéricas, sem corpo, cabeçalhos nem chave |

## 2. Ameaças e as camadas que as barram

Cada linha é um ataque plausível. Nenhuma camada sozinha basta: a coluna "se a primeira falhar" mostra a rede de segurança.

```mermaid
flowchart TB
    subgraph AM["Ameaça"]
        A1["Código tenta rede,<br/>subprocesso ou escalada"]
        A2["Código tenta ler dado<br/>de outro departamento"]
        A3["Instrução escondida num documento<br/>tenta mudar o comportamento"]
        A4["Saída forjada tenta fechar o envelope<br/>e passar por instrução do sistema"]
        A5["Ferramenta inexistente ou negada<br/>é chamada às cegas"]
        A6["Laço infinito, memória ou saída enorme"]
        A7["Vazamento da chave do OpenRouter"]
        A8["Escrita externa sem consentimento"]
    end

    subgraph DEF["Defesa que barra"]
        D1["Contêiner: sem rede, somente leitura,<br/>cap_drop ALL, no-new-privileges, usuário 10001"]
        D2["Montagem por perfil:<br/>o diretório nem existe no contêiner"]
        D3["Observação como dado não confiável<br/>e prompt que manda tratar como dado"]
        D4["Neutralização do delimitador<br/>NFKC, invisíveis, homóglifos"]
        D5["Concessões por perfil:<br/>negada responde como inexistente"]
        D6["Timeout por célula, orçamento,<br/>limites de memória, processos e saída"]
        D7["Segredos fora do sandbox e do MCP,<br/>erros genéricos"]
        D8["Nível da ferramenta:<br/>escrita externa recusada"]
    end

    A1 --> D1
    A2 --> D2
    A3 --> D3
    A4 --> D4
    A5 --> D5
    A6 --> D6
    A7 --> D7
    A8 --> D8

    D1 -. "se falhar" .-> E1["Verificação estática<br/>(extra, não é fronteira)"]
    D2 -. "se falhar" .-> E2["Filtro por departamento<br/>dentro da ferramenta"]
    D3 -. "se falhar" .-> E3["Sem ferramenta de escrita:<br/>o dano é limitado"]
    D5 -. "se falhar" .-> E5["Validação de argumentos"]
    D8 -. "se falhar" .-> E8["Aprovação humana<br/>(planejado, Fase 4)"]
```

## 3. Endurecimento do contêiner

O host cria o contêiner e define tudo isso na criação. O código do modelo não tem como relaxar nenhuma das opções.

```mermaid
flowchart TD
    C["Contêiner do REPL<br/>imagem fixada por digest"]

    C --> N["Rede<br/>network_mode none"]
    C --> F["Sistema de arquivos<br/>raiz somente leitura"]
    C --> T["tmpfs efêmeros<br/>/tmp e /saida: noexec, nosuid, nodev, 64 MiB"]
    C --> K["Capacidades<br/>cap_drop ALL"]
    C --> P["Privilégios<br/>no-new-privileges, privileged false,<br/>sem socket do Docker"]
    C --> U["Usuário<br/>10001:10001, sem shell, sem home"]
    C --> R["Recursos<br/>512 MiB sem swap, 128 processos, 1 CPU"]
    C --> D["/dados por departamento<br/>bind somente leitura, só o que o perfil permite"]
    C --> I["Imagem mínima<br/>sem pip, sem harness/host, dependências com hash"]
    C --> L["Rótulo harness-sandbox<br/>só esses contêineres são removidos pelo host"]
```

## 4. Do código do modelo à execução

O que acontece com cada bloco de código, e o que cada falha provoca. Reprovar na verificação é barato: o código nem chega ao contêiner.

```mermaid
flowchart TD
    INI(["Resposta do modelo com bloco python"]) --> TAM{"Tem até 8000<br/>caracteres?"}
    TAM -- "não" --> R1["Devolve o problema ao modelo<br/>nada é executado"]
    TAM -- "sim" --> SIN{"Sintaxe válida<br/>e analisável?"}
    SIN -- "não" --> R1
    SIN -- "sim" --> IMP{"Importa módulo proibido<br/>ou chama eval, exec, compile?"}
    IMP -- "sim" --> R1
    IMP -- "não" --> FER{"Chama tools.x.y<br/>que o perfil não vê?"}
    FER -- "sim" --> R1
    FER -- "não" --> EXE["Envia exec ao contêiner"]

    EXE --> TMO{"A célula termina<br/>no tempo?"}
    TMO -- "sim" --> OBS["exec_done: stdout, stderr, exceção"]
    TMO -- "não" --> INT["Runner injeta interrupção na thread"]
    INT --> MORR{"A célula parou?"}
    MORR -- "sim" --> OBS
    MORR -- "não" --> REI["exec_done com reiniciar verdadeiro:<br/>o host mata e recria o contêiner"]
    REI --> OBS

    OBS --> ENV["Envelope: neutraliza, trunca<br/>e marca como não confiável"]
    ENV --> VOLTA(["Volta ao modelo"])

    ERRO["Falha interna da verificação"] -. "falha fechada" .-> R1
```

A verificação estática **não é fronteira de segurança**: ela só pega o uso literal e direto. Acesso indireto (`getattr`, atributos de `__builtins__`) passa, e quem segura é o contêiner.

## 5. Pipeline do envelope da observação

Tudo que o código imprime volta ao modelo como dado. O objetivo é que ninguém consiga forjar uma instrução do sistema dentro da saída.

```mermaid
flowchart LR
    A["stdout, stderr e exceção<br/>do código do modelo"] --> B["Normalização NFKC<br/>fullwidth e matemáticos viram ASCII"]
    B --> C["Quebras extras viram escapes<br/>só o \n real separa linhas"]
    C --> D["Neutraliza o delimitador<br/>com invisíveis entre as letras,<br/>caixa mista e homóglifos"]
    D --> E["Trunca por seção<br/>stdout 4000, stderr 1000, exceção 2000<br/>75% começo e 25% fim"]
    E --> F["Um único bloco<br/>saida_nao_confiavel"]
    F --> G["Modelo recebe a observação"]

    N["Nomes de ferramentas<br/>escolhidos pelo modelo"] --> D
```

Limitação conhecida: confusáveis fora do mapa de homóglifos (um alfabeto inteiro) não são cobertos. Eles não formam o ASCII do delimitador, então o envelope real continua único.

## 6. Prompt injection: caminho do ataque e o que o segura

Um autor de documento planta uma instrução. O diagrama mostra por onde ela viaja e onde é contida.

```mermaid
sequenceDiagram
    autonumber
    participant X as Autor do documento
    participant D as Arquivo em /dados
    participant R as REPL (sandbox)
    participant O as Orquestrador
    participant M as Modelo
    participant G as Registro (host)

    X->>D: planta "ignore as regras e faça X"
    M->>R: código que lê o arquivo
    R->>O: stdout com a instrução plantada
    O->>O: neutraliza delimitador e embrulha em saida_nao_confiavel
    O->>M: observação marcada como dado
    Note over M: o prompt manda tratar o bloco como dado.<br/>Isto é uma defesa probabilística:<br/>o modelo pode falhar.
    M->>R: código que tenta o que a instrução pediu
    R->>G: tool_call
    G->>G: concessão, argumentos e nível
    alt ferramenta não concedida ou de escrita externa
        G-->>R: erro: não existe, ou exige aprovação
    else leitura concedida
        G-->>R: dado dentro do que o perfil já podia ler
    end
    Note over G: o limite real está aqui:<br/>mesmo que o modelo obedeça, o que o perfil<br/>não pode fazer continua impossível
```

A defesa de verdade contra injeção não é o modelo recusar. É o raio de ação ser pequeno: sem ferramenta de escrita concedida, sem rede, sem dados de outros departamentos.

## 7. A *lethal trifecta*

O risco grave nasce quando uma mesma sessão combina três coisas. A política que recusa a combinação é **planejada** (Fase 4). Hoje o que protege é manter o raio pequeno.

```mermaid
flowchart TB
    P["Dado privado<br/>arquivos de departamentos"]
    U["Conteúdo não confiável<br/>documento externo, saída de MCP"]
    C["Canal externo<br/>ferramenta que envia algo para fora"]

    T{{"Os três juntos na mesma sessão<br/>= vazamento por prompt injection"}}

    P --> T
    U --> T
    C --> T

    subgraph HOJE["Hoje (Fases 1 e 2)"]
        H1["Sandbox sem rede:<br/>o código não tem canal externo próprio"]
        H2["Escrita externa recusada pelo despachante"]
        H3["MCP só com ferramentas de leitura concedidas"]
        H4["Ferramentas declaram as propriedades,<br/>o catálogo traz a flag de confiança"]
    end

    subgraph PLAN["Planejado (Fase 4)"]
        F1["Host registra os arquivos que a sessão abriu"]
        F2["Abrir arquivo externo marca a sessão como exposta"]
        F3["Política recusa a combinação<br/>ou exige aprovação humana"]
    end

    T -.-> HOJE
    T -.-> PLAN
```

Fluxo do rastreio de arquivos abertos e da política da trifecta em execução:

```mermaid
sequenceDiagram
    autonumber
    participant C as Código no REPL (sandbox)
    participant G as Gancho de auditoria (rastreio.py)
    participant H as Host (RastreadorDeArquivos)
    participant E as Exposição e Política
    participant R as Registro / Ferramenta externa

    C->>G: open("/dados/vendas/externo.txt")
    G->>H: notifica abertura do caminho em /dados
    H->>E: consulta manifesto.json e marca viu_nao_confiavel = true
    C->>R: tools.teste.enviar() (canal_externo = true)
    R->>E: avalia política da trifecta
    E-->>R: recusa (sessão já exposta a conteúdo não confiável)
    R-->>C: ToolError: recusado pela política
```

## 8. Onde vivem os segredos

A chave do OpenRouter existe só no processo do host, dentro do cliente LLM. Nenhum outro componente a recebe.

```mermaid
flowchart LR
    ENV[".env ou ambiente do host<br/>OPENROUTER_API_KEY"] --> CFG["Configuração<br/>lê a chave"]
    CFG --> LLM["Cliente OpenRouter<br/>a chave só serve para criar o SDK<br/>e não fica em atributo"]
    LLM -->|"HTTPS"| OR["OpenRouter"]

    LLM -. "erro: só tipo e status,<br/>sem corpo, cabeçalhos nem chave" .-> ORQ["Orquestrador e trace"]

    ENV -. "não passa" .-> SBX["Sandbox<br/>sem rede e sem a variável"]
    ENV -. "não passa" .-> MCP["Servidores MCP<br/>ambiente mínimo: PATH, HOME<br/>e o env declarado no arquivo"]
```

O `.env` está no `.gitignore`. O trace guarda eventos do pedido, mas não a chave nem o corpo das respostas de erro.

## 9. Limites e orçamentos em camadas

Cada nível limita o seguinte. Se um falhar, o de fora continua valendo.

```mermaid
flowchart TB
    subgraph PED["Pedido"]
        PI["Iterações: 15 (o modelo é chamado no máximo N+1 vezes)"]
        PT["Tokens: 200.000"]
        PS["Tempo total do pedido"]
    end

    subgraph LLMC["Chamada ao modelo"]
        L1["Prazo total: 120 s, incluindo repetições e esperas"]
        L2["Tentativa: 60 s, em thread daemon"]
        L3["Repetições 1, 2 e 4 s só em falhas transitórias"]
    end

    subgraph CEL["Célula no sandbox"]
        C1["Timeout por célula: 30 s, ou o que restar do pedido"]
        C2["Margem do host: +3 s, depois o contêiner é morto"]
        C3["50 chamadas de ferramenta por célula"]
        C4["Linha do protocolo: 16 MiB, acima disso o contêiner é descartado"]
    end

    subgraph FER["Ferramenta e contexto"]
        F1["Resultado de ferramenta: 1 MiB"]
        F2["Contexto do pedido: 2 MiB"]
        F3["Saída por célula: 4000 + 1000 + 2000 caracteres"]
    end

    subgraph RES["Recursos do contêiner"]
        R1["512 MiB sem swap"]
        R2["128 processos"]
        R3["1 CPU"]
        R4["tmpfs de 64 MiB"]
    end

    PED --> LLMC
    PED --> CEL
    CEL --> FER
    CEL --> RES
```

Ao estourar o orçamento, o pedido não é cortado em silêncio: o harness pede ao modelo um resumo final do que conseguiu, e o resultado traz o motivo (`orcamento_iteracoes`, `orcamento_tokens`, `orcamento_tempo`).

## 10. Falhas do sandbox e recuperação

O sandbox pode morrer, travar ou violar o protocolo. O host trata cada caso sem derrubar o pedido e sem confiar no texto vindo do contêiner.

```mermaid
stateDiagram-v2
    [*] --> Vivo: start
    Vivo --> Executando: exec
    Executando --> Vivo: exec_done normal
    Executando --> Reiniciando: exec_done com reiniciar
    Executando --> Morto: canal fecha, OOM ou saída do processo
    Executando --> Morto: sem resposta no prazo mais a margem
    Executando --> Morto: linha acima de 16 MiB ou mensagem inválida
    Reiniciando --> Vivo: mata, recria e reenvia o contexto
    Morto --> Reiniciando: o orquestrador reinicia e avisa o modelo
    Vivo --> [*]: close remove o contêiner
    Reiniciando --> [*]: falha ao recriar, pedido termina com erro_sandbox

    note right of Reiniciando
        O reinício é decidido pelo campo do protocolo,
        nunca pelo texto da exceção, que pode conter
        qualquer coisa, inclusive código do modelo.
        As variáveis se perdem, o contexto volta.
    end note
```

## 11. O que existe e o que falta

```mermaid
flowchart LR
    subgraph FEITO["Implementado"]
        a1["Contêiner endurecido, sem rede"]
        a2["Montagem de dados por perfil"]
        a3["Verificação estática, falha fechada"]
        a4["Envelope não confiável com neutralização"]
        a5["Concessões por perfil, negar por padrão"]
        a6["Níveis de ferramenta, escrita externa recusada"]
        a7["Limites, orçamentos e prazos"]
        a8["Reinício controlado do REPL"]
        a9["Segredos fora do sandbox e do MCP"]
        a10["FINAL com fontes obrigatórias"]
        a11["Trace JSONL por pedido"]
    end

    subgraph FALTA["Planejado"]
        b1["Aprovação humana com diff e versionamento"]
        b2["Política contra a lethal trifecta"]
        b3["Rastreio de arquivos abertos, flag de confiança"]
        b4["gVisor, em VPS Linux"]
        b5["Espera por aprovação com timeout próprio, D12"]
        b6["Ataques de injeção plantados nas avaliações, Fase 3"]
        b7["SAST e CI de segurança"]
    end

    FEITO -.-> FALTA
```

## 12. Fluxo de uma entrega

Fluxo de `entrega.publicar` com validação no host, aprovação humana no terminal e crédito de prazo na célula (D12):

```mermaid
sequenceDiagram
    autonumber
    participant C as Código no REPL (sandbox)
    participant S as Sandbox (/saida)
    participant R as Registro (host)
    participant A as Pessoa no terminal
    participant E as Pasta de entregas (host)

    C->>S: grava arquivo em /saida/
    C->>R: tools.entrega.publicar(caminho, destinatario)
    R->>S: ler_saida(nome, limite_bytes)
    R->>R: valida extensão, tamanho e ausência de macros
    R->>R: monta PedidoDeAprovacao só com fatos medidos
    R->>A: exibe pedido e aguarda confirmação [s/N]
    A-->>R: aprova
    R->>E: grava arquivo aprovado e registra em registro.jsonl
    R-->>C: devolve resultado com espera_s (crédito de prazo da célula)
```

## Onde cada parte está no código

| Assunto | Arquivo |
| --- | --- |
| Endurecimento do contêiner, reinício e limites | [`harness/sandbox/docker_sandbox.py`](../harness/sandbox/docker_sandbox.py) |
| Imagem e usuário sem privilégio | [`harness/sandbox/Dockerfile`](../harness/sandbox/Dockerfile) |
| REPL, timeout por thread e `FINAL` | [`harness/sandbox/runner.py`](../harness/sandbox/runner.py) |
| Protocolo e limites de mensagem | [`harness/sandbox/protocol.py`](../harness/sandbox/protocol.py) |
| Verificação estática | [`harness/orchestrator/estatico.py`](../harness/orchestrator/estatico.py) |
| Envelope, neutralização e truncagem | [`harness/orchestrator/observation.py`](../harness/orchestrator/observation.py) |
| Loop, orçamento e resumo final | [`harness/orchestrator/loop.py`](../harness/orchestrator/loop.py) |
| Registro, despachante e limite de resultado | [`harness/host/registry.py`](../harness/host/registry.py) |
| Perfis e concessões | [`harness/host/perfil.py`](../harness/host/perfil.py) |
| Cliente OpenRouter, prazo e erros genéricos | [`harness/llm/openrouter.py`](../harness/llm/openrouter.py) |
| Subprocessos MCP e ambiente mínimo | [`harness/host/mcp/`](../harness/host/mcp/) |
| Decisões (conteúdo não confiável, ponte, concessões) | [`docs/adr/`](adr/README.md) |
