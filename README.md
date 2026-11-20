# agent-repl-security

Harness de agente em que o modelo responde a pedidos escrevendo código Python, executado num REPL persistente e isolado (contêiner sem rede), que chama ferramentas do host por uma ponte controlada e devolve ao modelo só o resultado relevante.

Projeto de estudo, com dados sintéticos. Combina duas ideias: código como espaço de ações (CodeAct) e ferramentas como API de código carregada sob demanda (Code Mode). O foco é segurança: isolamento do sandbox, política contra a *lethal trifecta*, aprovação humana para escrita e defesa contra prompt injection.

## Estado

Fase de especificação: ainda não há código.

## Documentação

- [Especificação v0](docs/especificacao-v0.md)
- [Decisões de arquitetura (ADRs)](docs/adr/README.md)
