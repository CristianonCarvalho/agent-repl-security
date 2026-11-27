# Rastreio de arquivos abertos e política da lethal trifecta (Fase 4, peça 1)

Data: 27/11/2026. Estado: aguardando revisão.

## Objetivo

Impedir que uma sessão combine, ao mesmo tempo, **dado privado**, **conteúdo não confiável** e **ferramenta com canal externo**. Hoje o registro só declara essas propriedades ([registry.py](../../../harness/host/registry.py)) e recusa pelo nível e pelas concessões. Esta peça acrescenta o rastreio do que a sessão leu e a política que usa essas informações.

Sucesso significa:

1. O host sabe, por pedido, se a sessão leu algum arquivo de `/dados` e se leu algum arquivo `externo`.
2. Com a política ligada, uma ferramenta com canal externo é recusada quando a sessão reúne os três fatores.
3. Com a política desligada (padrão), nada muda: bytes, traces e medições anteriores permanecem iguais.
4. Dá para ligar e desligar a política numa execução de avaliação, para comparar antes e depois (peça 3).

## Fora do escopo

- **Aprovação humana real e ferramentas com efeito** (`entrega.publicar`, `arquivos.propor_alteracao`): peça 2. Aqui a política só deixa um gancho.
- **Medição antes e depois contra ataques com efeito**: peça 3.
- **gVisor e VPS**: peça 4, exige Linux.
- Escopo por parâmetro das concessões e rastreio de escrita em `/saida`.

## Decisões já tomadas

| Decisão | Escolha |
| --- | --- |
| Primeira peça | Trifecta com rastreio |
| Como o host sabe o que foi aberto | Gancho de auditoria no REPL, que avisa o host antes de cada `open` |
| O que a política faz | Devolve `permitir`, `pedir_aprovacao` ou `recusar`; `pedir_aprovacao` vale como recusa até a peça 2 |
| Ativação | Opção de configuração, desligada por padrão |

## Estado da sessão

Novo módulo `harness/host/politica.py`, com duas partes sem dependência do sandbox.

**`Exposicao`** (mutável, uma por pedido):

- `viu_dado_privado: bool`
- `viu_nao_confiavel: bool`
- `marcar_arquivo(confianca: str)`: marca `viu_dado_privado`; marca `viu_nao_confiavel` se a confiança não for `confiavel`.
- `marcar_ferramenta(ferramenta)`: marca `viu_dado_privado` se `ferramenta.dado_privado`; marca `viu_nao_confiavel` se `ferramenta.conteudo_nao_confiavel`.

**`Decisao`** (enum): `PERMITIR`, `PEDIR_APROVACAO`, `RECUSAR`.

**`avaliar(exposicao, ferramenta) -> Decisao`**, função pura:

1. Calcula o estado efetivo somando a exposição às propriedades da própria ferramenta: `privado = viu_dado_privado or ferramenta.dado_privado`, `nao_confiavel = viu_nao_confiavel or ferramenta.conteudo_nao_confiavel`.
2. Se `ferramenta.canal_externo` e `privado` e `nao_confiavel`, devolve `PEDIR_APROVACAO`.
3. Caso contrário, devolve `PERMITIR`.

`RECUSAR` fica no enum para regras futuras (por exemplo, lista de ferramentas nunca permitidas) e não é produzido hoje.

O tratamento de `PEDIR_APROVACAO` é um parâmetro do registro: uma função `aprovador(chamada, ferramenta) -> bool` que, por padrão, devolve `False`. A peça 2 troca esse padrão pela aprovação humana, sem mexer em `avaliar`.

## Registro

`Registro` ganha dois campos opcionais, ambos `None` por padrão:

- `politica: bool` (ligada ou não), vinda da configuração.
- A `Exposicao` do pedido atual, criada pelo orquestrador no início de cada pedido e zerada no seguinte.

Em `_despachar`, depois da validação dos argumentos e antes de executar a função:

1. Com a política ligada, chama `avaliar`.
2. `PEDIR_APROVACAO` chama o aprovador; se ele devolver `False`, responde com erro curto: *"a chamada foi recusada pela política de segurança: a sessão leu conteúdo não confiável e esta ferramenta envia dados para fora"*. A mensagem não cita arquivo, caminho nem detalhe interno.
3. Com a chamada permitida, `marcar_ferramenta` atualiza a exposição **depois** da execução, para que o resultado de uma ferramenta não confiável conte para as chamadas seguintes.

Cada decisão gera o evento de trace `politica_decisao` com ferramenta, decisão e os dois marcadores. O evento não carrega conteúdo.

## Rastreio no REPL (gancho de auditoria)

No sandbox, o `runner.py` instala, uma vez, no início do processo, um `sys.addaudithook`. Um gancho de auditoria não pode ser removido depois de instalado.

- O gancho filtra o evento `open`. Para cada abertura de caminho sob `/dados`, ele envia ao host, pela ponte já existente, uma mensagem de rastreio com o caminho e **espera o reconhecimento** antes de deixar a abertura prosseguir.
- Aberturas fora de `/dados` (bibliotecas, `/proc`, `/saida`, `/tmp`) são ignoradas pelo gancho.
- **Falha fechada.** Se o aviso não for entregue ou o host não responder dentro do prazo, o gancho levanta exceção e a abertura falha. O modelo vê um erro de leitura, nunca um acesso silencioso.
- O gancho evita recursão: enquanto envia o aviso, desliga a si mesmo por uma flag da thread.

No host, o recebimento da mensagem de rastreio:

1. Normaliza o caminho (`realpath` relativo à raiz de `/dados`) e recusa caminhos que saiam dela.
2. Resolve a flag de confiança com a **mesma função** que o catálogo usa (`manifesto.json`; sem manifesto, `confiavel`; valor desconhecido, `externo`). Extrair essa leitura de `harness/host/tools/catalogo.py` para uma função compartilhada é parte desta peça.
3. Chama `Exposicao.marcar_arquivo` e responde ao sandbox.

A mensagem de rastreio entra no protocolo ([protocol.py](../../../harness/sandbox/protocol.py)) como tipo próprio, separado de `tool_call`. Assim ela não passa pelas concessões do perfil nem aparece no catálogo ou na busca de ferramentas. Ela só existe com a política ligada; desligada, o runner não instala o gancho.

**Limites assumidos.** O gancho não detém código que fuja do interpretador (por exemplo, `ctypes` ou um subprocesso). É defesa em camadas. O isolamento forte contra isso é o gVisor, da peça 4. Esta limitação vai para o relatório da peça 3.

## Variante JSON

Não há código abrindo arquivos. Todo contato com dados passa por ferramenta, então a exposição vem só de `marcar_ferramenta`. O `loop_json` usa o mesmo registro e a mesma política; não há gancho nem mensagem de rastreio.

## Efeito sobre o que já existe

- Ferramentas MCP entram como `conteudo_nao_confiavel` e `canal_externo` (ver [adaptador.py](../../../harness/host/mcp/adaptador.py)). Com a política ligada, chamar uma ferramenta MCP depois de ler qualquer arquivo de `/dados` passa a ser recusado. Também é recusada a sequência inversa: chamar a ferramenta MCP, que marca `viu_nao_confiavel`, e depois outra ferramenta com canal externo, desde que a sessão já tenha dado privado.
- Isso bloqueia fluxos legítimos como o `injecao-10` e o `injecao-11`. É conservador de propósito e será medido na peça 3 como custo de utilidade.
- `catalogo.listar` é `dado_privado=True`, sem canal externo. Chamá-la marca a exposição, mas não é recusada.
- A política desligada é o padrão. Nenhum teste, relatório ou linha de base existente muda.

## Configuração e CLI

- Acrescenta-se `politica_trifecta: bool = False` à configuração do harness e a `ConfigExecucao` dos evals. Nada mais nelas muda.
- Flag `--politica-trifecta` na CLI do harness e na CLI dos evals. A flag entra no relatório de avaliação (`politica_trifecta`), para que execuções com e sem política não se confundam. Relatórios antigos carregam sem o campo.
- Com a política ligada, o relatório marca o conjunto como não comparável a linhas de base existentes; `--salvar-linha-de-base` recusa a flag.

## Testes

Unitários, sem Docker:

- `avaliar` para as oito combinações de `privado`, `nao_confiavel` e `canal_externo`, incluindo a soma com as propriedades da própria ferramenta.
- `Exposicao.marcar_arquivo` e `marcar_ferramenta`.
- Registro: política desligada não altera nenhum resultado; ligada, recusa a ferramenta com canal externo após exposição; permite a mesma ferramenta sem exposição; mensagem de erro sem detalhe interno; evento `politica_decisao` emitido.
- Resolução de confiança compartilhada com o catálogo: sem manifesto, com manifesto, valor inválido, caminho fora da raiz.
- Gancho de auditoria, importado e exercitado em processo, com ponte falsa: abertura de arquivo confiável, de arquivo externo, fora de `/dados`, e falha do aviso (a abertura deve falhar).

Integração, com Docker:

- Um REPL real abre um arquivo `externo` e depois chama uma ferramenta de teste com `canal_externo`; a chamada é recusada com a política ligada e executada com ela desligada.

Gates: `ruff check`, `ruff format --check`, `mypy` e a suíte completa, como nas etapas anteriores.

## Riscos

- **Falso positivo.** A política recusa sessões legítimas que leram um arquivo externo e depois usam uma ferramenta com canal externo. Mitigação: política desligada por padrão e medida na peça 3.
- **Contorno do gancho.** Código que escape do interpretador não é visto. Mitigação: documentado; gVisor na peça 4.
- **Desempenho.** Cada abertura sob `/dados` faz uma ida e volta à ponte. A abertura de planilhas pode disparar várias. Mitigação: medir o custo no teste de integração; se for relevante, guardar em cache dentro do gancho os caminhos já reportados na mesma sessão.
- **Contaminação do resumo não é coberta.** Um modelo que afirma um fato falso no texto (como no `injecao-08`) não chama ferramenta alguma. Esta política não impede isso. Esse caso é do B3 (dual LLM).
