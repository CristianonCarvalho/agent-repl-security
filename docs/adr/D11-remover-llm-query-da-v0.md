# D11 — Remover `llm_query` da v0

- Status: aceita
- Data: 20/11/2026
- Relacionadas: D6, spec v0

## Contexto

A D6 já deixava `llm_query` fora da v0, mas ele continuava em RF6, na regra de segurança 6, no contrato do REPL, no limite de profundidade e na mensagem `llm_call` da ponte. A spec ficava inconsistente.

## Decisão

Remover `llm_query` de todo o escopo da v0:

- RF6 (sub-chamada ao LLM com profundidade limitada);
- regra de segurança 6 (sub-chamadas não herdam ferramentas);
- a função `llm_query` do contrato 1 (API dentro do REPL);
- a mensagem `llm_call` do contrato 2 (ponte);
- o limite "profundidade de `llm_query`";
- a linha da tabela de ferramentas do host.

Documentos e atas são resumidos pelo modelo principal, lendo o texto extraído (como já dizia a D6). `llm_query` volta na v1 junto com as RLMs.

## Consequências

- A v0 combina duas das três ideias de partida (CodeAct e Code Mode). A parte das RLMs fica para a v1.
- O texto da spec precisa ser editado para refletir isso; esta ADR é a referência da mudança.
- Se o texto extraído passar da janela de contexto, o tratamento de "entrada maior que o contexto" fica só com o contexto como variável (`context`), sem sub-chamadas.
