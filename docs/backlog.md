# Backlog

Itens aprovados para desenvolver no futuro, com o motivo, o desenho provável e como medir o resultado. Cada item tem um estado. Quando um item for feito, o registro fica aqui como histórico e a implementação entra na [especificação](especificacao-v0.md).

| Id | Item | Estado |
| --- | --- | --- |
| B1 | Dicionário de dados (`catalogo.dicionario`) | Não iniciado |

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
