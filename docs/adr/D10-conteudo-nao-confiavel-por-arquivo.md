# D10 — Conteúdo não confiável marcado por arquivo

- Status: aceita
- Data: 20/11/2026
- Relacionadas: D5 (ferramentas), regra de segurança 3 (lethal trifecta), spec v0

## Contexto

A regra 3 proíbe combinar numa sessão dado privado, conteúdo não confiável e ferramenta com canal externo. A D5 define que arquivos são lidos como código comum no sandbox, não por ferramenta. A política só enxerga chamadas que atravessam a ponte, então ela não sabe quando o texto de uma proposta externa (com instrução escondida) entra na sessão.

## Decisão

Cada arquivo tem uma flag de confiança (`confiavel` ou `externo`), declarada pelo host e exposta em `catalogo.listar`.

- O host registra quais arquivos o sandbox abriu na sessão (caminho e hash).
- Ao abrir um arquivo `externo`, a sessão passa a ter a propriedade "conteúdo não confiável".
- A política recusa, ou exige aprovação humana, a chamada de ferramenta de escrita (`arquivos.propor_alteracao`, `arquivos.reverter`, `entrega.publicar`) quando a sessão tiver ao mesmo tempo dado privado, conteúdo não confiável e a ferramenta com canal externo.
- O trace grava a flag e a decisão de política.

## Alternativas descartadas

- Marcar por departamento: simples demais, bloquearia pedidos legítimos sobre o Comercial.
- Só aprovação humana: sem rastreio de origem, é mais fraco contra a trifecta.

## Consequências

- O host precisa interceptar ou auditar a abertura de arquivos em `/dados` (por exemplo, via registro de leituras na ponte ou montagem controlada).
- O gerador de dados sintéticos deve marcar a proposta com injeção como `externo`.
- Entra no conjunto de testes: o pedido "Resuma as propostas da semana" deve ter a alteração não pedida bloqueada.
