# Backlog

Itens para desenvolver no futuro, com o motivo, o desenho provável e como medir o resultado. Cada item tem um estado. Os marcados como **Ideia** estão aqui para não se perderem e **podem nunca ser implementados**: só entram se uma medição mostrar que valem. Quando um item for feito, o registro fica aqui como histórico e a implementação entra na [especificação](especificacao-v0.md).

| Id | Item | Estado |
| --- | --- | --- |
| B1 | Dicionário de dados (`catalogo.dicionario`) | Não iniciado |
| B2 | Pré-carregar o catálogo de arquivos na primeira mensagem | Ideia |
| B3 | Experimento com subagente e padrão *dual LLM* | Ideia |

## B1. Dicionário de dados (`catalogo.dicionario`)

Registrado em 23/11/2026. A spec já previa a ferramenta (seção "Ferramentas do host") e ela consta como "não iniciado" na tabela de estado.

### Por que

Hoje o agente descobre a estrutura dos arquivos por tentativa, gastando turnos do loop só para olhar. No trace real do `cruzamento-03` (perfil Financeiro), o agente usou 6 células:

| Célula | O que fez |
| --- | --- |
| 1 | Listou os arquivos com `tools.catalogo.listar()` |
| 2 | Chamou `search_tools(...)`, que voltou vazio (desperdício) |
| 3 | Abriu os dois `.xlsx` para ver nomes de abas e colunas |
| 4 | Releu os dois arquivos para ver os valores de `status` e `situacao` e as grafias do cliente |
| 5 | Calculou os totais |
| 6 | Chamou `FINAL` |

As células 2, 3 e 4 são exploração. Com um dicionário, o modelo já receberia abas, colunas e valores possíveis na célula 1, e a pergunta cairia para cerca de 3 voltas. Cada turno reenvia todo o histórico, então os tokens de entrada crescem a cada volta (de 1.429 no turno 1 para 4.143 no turno 6 neste caso).

Um segundo motivo é a correção: sem o dicionário, o modelo pode supor um nome de coluna ou um valor de `status` errado e calcular um total errado sem perceber.

### O que seria

Uma ferramenta de leitura do host, concedida a todos os perfis (grupo `catalogo`), que devolve para cada arquivo **visível ao perfil**:

- planilhas: abas, número de linhas, colunas com tipo e valores distintos das colunas categóricas (até um limite), mais uma amostra curta de linhas;
- documentos e apresentações: títulos e seções;
- a flag de confiança do arquivo (a mesma que o `catalogo.listar` já mostra).

### Pontos de segurança a decidir no desenho

- **Visibilidade:** a mesma checagem do `catalogo.listar`. Um perfil nunca vê o dicionário de um arquivo que não monta.
- **Conteúdo não confiável:** valores de células e títulos são dados externos e podem conter instruções plantadas (como a proposta PR-2026-007). O resultado precisa ser marcado como não confiável e neutralizado como qualquer saída de ferramenta.
- **Onde ler os arquivos:** a spec diz que ler arquivos é código no sandbox e não ferramenta do host. Abrir `.xlsx`, `.docx` e `.pptx` no host amplia a superfície de ataque (arquivos zip malformados, XML grande). Há três caminhos a avaliar: ler no host com limites rígidos; calcular o dicionário na geração ou na ingestão dos dados e guardá-lo junto do catálogo; ou calcular dentro do sandbox por um auxiliar. Essa escolha é a principal decisão do item.
- **Tamanho e custo:** limite de resultado de 1 MiB, limite de valores distintos e de linhas de amostra, e cache pelo hash que o catálogo já calcula.

### Como medir

Rodar os 20 casos ativos antes e depois, com as mesmas rodadas e semente:

- a média de iterações por pedido deve cair (hoje 5,6 nas 5 rodadas da última execução com 20 casos);
- a taxa de acerto não pode cair;
- os tokens de entrada por rodada devem diminuir, mesmo com o dicionário no histórico;
- casos de referência: `cruzamento-03` (6 células) e `conformidade-05`.

### Alternativas mais baratas

- Ajustar o prompt para juntar listagem e inspeção de colunas na mesma célula e proibir `search_tools` quando o catálogo já mostra os arquivos.
- Uma função auxiliar `inspecionar(caminho)` dentro do REPL. É mais simples, mas fica no código do sandbox, fora do controle do host.

Vale tentar o ajuste de prompt antes, porque custa pouco e mostra quanto da exploração sobra.

### Critério de pronto

Ferramenta registrada com testes de visibilidade por perfil, de neutralização do conteúdo e de limite de tamanho; o relatório de avaliação com as iterações médias antes e depois; e a spec atualizada.

## B2. Pré-carregar o catálogo de arquivos na primeira mensagem

Registrado em 23/11/2026. **Ideia**, pode não ser implementada.

### Por que

Na linha de base (100 rodadas, 20 casos × 5), a primeira célula do agente foi **exatamente** `print(tools.catalogo.listar())` em **100 das 100 rodadas**. Medidas dessa célula:

| Medida | Valor |
| --- | --- |
| Tempo no sandbox | 12 ms em média (máximo 48 ms) |
| Tokens que o modelo gasta escrevendo a linha | 27 em média |
| Iterações por rodada | 5,3 |

O custo não está no sandbox, e sim no **turno do modelo**: uma das 5,3 iterações só serve para listar arquivos que o host já conhece. Chamar a ferramenta diretamente, sem o sandbox, não resolveria, porque o modelo continuaria precisando de um turno para pedir a chamada e de outro para ler o resultado. O que economiza o turno é entregar a lista desde o início.

### O que seria

O host monta a lista de arquivos visíveis ao perfil (a mesma que o `catalogo.listar` devolve, possivelmente sem hash e tamanho) e a inclui na primeira mensagem do pedido, marcada como dado do host. A ferramenta `catalogo.listar` continua existindo, para catálogos grandes e para o modelo usar a lista em código.

### Economia estimada (estimativa, a confirmar)

- Uma volta a menos por pedido: cerca de 19% das iterações (5,3 para perto de 4,3).
- Cerca de 9% menos tokens de entrada, porque o turno 1 custa cerca de 1.430 dos 16.300 tokens de entrada de uma rodada típica.
- Menos tempo, pela chamada de modelo que deixa de existir.

### Riscos e pontos a decidir

- O prompt cresce com o número de arquivos. Com 20 arquivos é pouco. Com centenas não serviria, e seria preciso um limite ou um resumo por departamento.
- A lista fica fixa no início do pedido.
- Nomes de arquivo são dados, então a lista precisa ir marcada como saída do host e não como instrução.
- Combina com o B1: o mesmo bloco poderia trazer abas e colunas, e aí caem também as células de exploração. Talvez o B2 seja só uma etapa do B1.

### Como medir

Rodar o conjunto completo (5 rodadas, provedor fixo) e comparar com a linha de base:

- as iterações médias devem cair de 5,3 para perto de 4,3;
- a taxa de acerto não pode cair (a margem de ruído diz se uma queda é real);
- os tokens de entrada por rodada devem diminuir.

### Critério para não fazer

Se a economia medida for pequena, ou se o acerto cair, o item é descartado.

## B3. Experimento com subagente e padrão *dual LLM*

Registrado em 23/11/2026. **Ideia**, pode não ser implementada. Depende do que a Fase 4 e a T19 mostrarem.

### Por que

A pergunta que originou o item: um subagente poderia pegar a saída de uma ferramenta, escrever o código e enviá-lo ao sandbox num só turno? A análise mostrou que **não reduz as chamadas ao modelo**: quem escreve o código precisa ler o resultado, e ler é uma chamada. O subagente só **move** a chamada para outro contexto, e as chamadas ficam em série. O ganho de eficiência é pequeno para tarefas de 5 turnos como as nossas.

Um subagente compensa em quatro situações:

| Motivo | Como ajuda | Vale aqui? |
| --- | --- | --- |
| Isolar contexto | Uma exploração grande fica no contexto do subagente, e o principal recebe só o resumo | Hoje não: as saídas são pequenas |
| Paralelismo | Tarefas independentes rodam ao mesmo tempo | Talvez nos cruzamentos |
| Modelo mais barato ou rápido para a parte mecânica | Reduz custo | Pouco: já custa cerca de US$ 0,0007 por rodada |
| **Segurança** | Um modelo em quarentena lê o conteúdo não confiável e devolve só dados estruturados | **Sim, é o motivo que sustenta o item** |

### O padrão *dual LLM*

Dois papéis:

- **Agente privilegiado:** tem as ferramentas e as permissões, mas **nunca lê o texto bruto** de fontes não confiáveis.
- **Leitor em quarentena:** lê o conteúdo não confiável (por exemplo a proposta com a instrução escondida), **não tem ferramentas** e devolve apenas campos de um esquema validado, como `{cliente, desconto}`.

Se o documento tentar dar uma ordem, ela chega a um modelo que não tem como obedecê-la. É uma defesa contra injeção por separação de quem lê e de quem age, e ataca a *lethal trifecta* de outro ângulo: em vez de decidir se uma sessão pode combinar dado privado, conteúdo não confiável e canal externo, impede que quem tem o canal externo leia o conteúdo não confiável.

### O que seria o experimento

- Reaproveitar o rótulo de confiança por arquivo do catálogo (o `manifesto.json`) para decidir o que vai ao leitor em quarentena.
- Esquema de saída do leitor validado antes de chegar ao agente privilegiado.
- Orçamento e custo contabilizados por papel.
- Traces aninhados no Langfuse (agente, leitor, células).
- Medir como uma **variante** (`subagente`) do relatório, ao lado de `repl` e `json`, com o mesmo conjunto de casos.

### Riscos e custos

- Complexidade: protocolo de passagem de dados, herança de permissões (o leitor nunca pode ter mais acesso que o perfil) e mais um ponto de falha.
- O esquema pode perder um detalhe de que o agente precisava, e o erro fica difícil de rastrear.
- A avaliação fica mais difícil: quando o resultado erra, é do agente, do leitor ou do esquema?

### Como medir

- Nos casos de injeção (quando a T19 existir): zero ações proibidas, que é o gate absoluto do D13.
- Nos casos ativos: o acerto não pode cair, e o custo e o número de chamadas devem ser registrados.

### Critério para não fazer

Se a política da *trifecta* e a aprovação humana da Fase 4 já derem a proteção necessária, ou se o custo de complexidade superar o ganho medido, o item é descartado.

