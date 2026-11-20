# D12 — Ponte em descritor dedicado e timeout de célula por thread

- Status: aceita
- Data: 20/11/2026
- Relacionadas: D3 (REPL com `exec`), RF2, contrato 2 (ponte), spec v0

## Contexto

O contrato 2 usa o stdin/stdout do processo do sandbox para o JSON-RPC, mas `print` é o único canal de volta ao modelo e também escreve no stdout. Código do modelo pode corromper o protocolo ou forjar mensagens. Além disso, o REPL usa `exec` no mesmo processo (D3): estourar os 30 s de uma célula não pode ser interrompido sem matar o processo, o que apagaria o estado da sessão (RF2).

## Decisão

1. **Canal do protocolo separado.** O JSON-RPC trafega num descritor de arquivo dedicado (por exemplo, fd 3 ou um par de pipes), criado pelo host. O stdout e o stderr do código do modelo são capturados por célula e enviados só dentro de `exec_done`.
2. **Timeout por thread.** Cada célula roda numa thread do runner, com o limite de 30 s medido pelo runner.
   - Se a célula terminar, o estado se mantém.
   - Se estourar e não puder ser interrompida, o harness faz reinício controlado: avisa o modelo, destrói o processo e recria o REPL, informando que as variáveis foram perdidas.
3. **Espera de aprovação humana.** O tempo bloqueado em `tool_call` aguardando aprovação não conta no limite de 30 s da célula, mas conta no tempo total do pedido (5 min), a calibrar.

## Alternativas descartadas

- Matar e reexecutar o histórico a cada timeout: robusto, mas lento e não determinístico com ferramentas.
- Um processo por célula: isolamento maior, mas o estado persistente viraria serialização e dificultaria o RF2.

## Consequências

- O runner do sandbox precisa de testes específicos: código que imprime JSON falso, que fecha ou sobrescreve `sys.stdout`, e que entra em laço infinito.
- Entra no trace um evento de "reinício controlado" com o motivo.
- O parser do protocolo no host deve rejeitar qualquer mensagem fora do canal dedicado.
