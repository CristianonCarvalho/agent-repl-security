# Aprovação humana e `entrega.publicar` (Fase 4, peça 2)

Data: 27/11/2026. Estado: aguardando revisão.

## Objetivo

Dar à política da lethal trifecta e à regra 4 ("escrita externa exige aprovação humana por chamada") uma pessoa que responde, e dar ao harness uma primeira ação com efeito real para essas regras protegerem: entregar um arquivo gerado a um departamento.

Hoje o `Registro` recusa qualquer ferramenta que não seja de leitura com a mensagem "a aprovação ainda não existe", e a decisão `pedir_aprovacao` da trifecta usa um aprovador que sempre nega ([registry.py](../../../harness/host/registry.py)). O sandbox também não consegue entregar nada: o host não lê `/saida`.

Sucesso significa:

1. Uma chamada de ferramenta que exige aprovação mostra o pedido a uma pessoa no terminal e só executa se ela aprovar.
2. A mesma interface atende a regra 4 (nível da ferramenta) e a decisão `pedir_aprovacao` da trifecta. Uma chamada que cai nas duas pergunta uma vez só.
3. `entrega.publicar` entrega um arquivo validado de `/saida/` a um departamento, depois da aprovação.
4. Esperar uma pessoa não estoura o limite de 30 s da célula nem consome o orçamento de tempo do pedido.
5. Sem aprovador configurado, nada muda: mesmas mensagens, mesmos relatórios e mesmos traces.
6. As avaliações podem usar um aprovador roteirizado (sempre aprova, sempre recusa), para a peça 3 comparar.

## Fora do escopo

- `arquivos.propor_alteracao`, `arquivos.reverter`, diff e versionamento de documentos: peça própria, depois.
- Aprovação por arquivo de pendências ou painel web: entram pela mesma interface `Aprovador`, mais tarde.
- Medir a política e a aprovação contra ataques: peça 3.
- gVisor: peça 4.

## Decisões já tomadas

| Decisão | Escolha |
| --- | --- |
| Escopo | Aprovação humana mais `entrega.publicar` |
| Onde a pessoa aprova | Terminal, por uma interface plugável |
| Origem do texto do pedido | Só fatos verificados pelo host, nunca texto do modelo |
| Espera por aprovação | Desconta do prazo da célula e do orçamento, com teto próprio de 120 s |

## Aprovador

Novo módulo `harness/host/aprovacao.py`, sem dependência do sandbox.

**`PedidoDeAprovacao`** (dataclass imutável):

- `ferramenta: str`
- `motivo: str`: `"nivel"` (regra 4), `"trifecta"` ou `"nivel+trifecta"`.
- `linhas: tuple[tuple[str, str], ...]`: pares rótulo e valor que a pessoa vê (por exemplo `("Arquivo", "relatorio.xlsx")`, `("Tamanho", "48 KiB")`, `("Hash", "ab12...")`, `("Destinatário", "financeiro")`).

Quem monta as `linhas` é a função da ferramenta, com dados que o host mediu (nome, tamanho, hash, destinatário já validado). Nenhum campo livre do modelo entra. Todo valor passa por `limpar_para_exibicao`, que remove caracteres de controle e quebras de linha e corta em 200 caracteres.

**`RespostaDeAprovacao`** (enum): `APROVADO`, `RECUSADO`, `EXPIRADO`.

**`Aprovador`** (`Protocol`): `__call__(pedido: PedidoDeAprovacao, prazo_s: float) -> RespostaDeAprovacao`.

Implementações:

- `AprovadorDeTerminal(entrada: TextIO, saida: TextIO)`: imprime o pedido, pergunta `Aprovar? [s/N]` e lê uma linha. `s` ou `sim` (sem diferenciar maiúsculas) aprova; qualquer outra coisa recusa, inclusive linha vazia e fim da entrada. A leitura usa uma thread com `join(prazo_s)`; se o prazo passar, devolve `EXPIRADO` e a thread de leitura é deixada para trás (daemon).
- `AprovadorFixo(resposta: RespostaDeAprovacao)`: devolve sempre a mesma resposta, para as avaliações e os testes. Atalhos `sempre_aprova()` e `sempre_recusa()`.

A aprovação vale uma chamada. Não há "lembrar a escolha".

## Registro

O `Registro` passa a receber `aprovador: Aprovador | None` e `prazo_aprovacao_s: float = 120.0`, no lugar do parâmetro `aprovador` atual (um `Callable[[ToolCall, Ferramenta], bool]`, que só existia para a Tarefa 3 da peça 1 e não é usado fora dela e dos seus testes, que serão atualizados).

Em `_despachar`, depois da validação dos argumentos:

1. Calcula os motivos: `nivel` se `ferramenta.nivel is not Nivel.LEITURA`; `trifecta` se a política estiver ligada e `avaliar` devolver `PEDIR_APROVACAO`.
2. Sem motivo, segue.
3. Com motivo e **sem aprovador**: comportamento de hoje. Nível não leitura devolve a mensagem atual ("exige aprovação humana; a aprovação ainda não existe"); trifecta devolve `MENSAGEM_POLITICA`.
4. Com motivo e aprovador: a função da ferramenta monta o pedido (ver `entrega.publicar`); ferramentas sem montador usam um pedido genérico só com o nome e o motivo. O registro chama o aprovador e mede o tempo gasto.
   - `APROVADO`: executa a função.
   - `RECUSADO`: erro curto ao modelo, "a chamada foi recusada pela pessoa responsável".
   - `EXPIRADO`: erro curto, "a aprovação não chegou a tempo".
5. O `ToolResult` da chamada traz `espera_s` com o tempo medido (ver abaixo). Chamadas sem aprovação não trazem o campo.

A ferramenta pode declarar um `montar_pedido: Callable[[Any, Perfil], tuple[tuple[str, str], ...]] | None` no `Ferramenta`. Sem ele, o pedido genérico traz só `("Ferramenta", nome)`.

Eventos de trace: `aprovacao_pedida` (ferramenta, motivo) e `aprovacao_resposta` (ferramenta, resposta, `espera_s`), emitidos pelo mesmo observador do `politica_decisao`. Nenhum evento carrega o conteúdo do arquivo.

## `entrega.publicar`

Registrada por `registrar_entrega(registro, ler_saida, pasta_entregas)` em `harness/host/tools/entrega.py`.

- Nome: `entrega.publicar`. Nível: `ESCRITA_EXTERNA`. `canal_externo=True`; `dado_privado=False` e `conteudo_nao_confiavel=False`, porque a ferramenta não devolve conteúdo ao modelo.
- Argumentos (pydantic, `extra="forbid"`): `caminho: str`, `destinatario: str` (um dos departamentos conhecidos: `vendas`, `comercial`, `marketing`, `financeiro`, `direcao`).
- Concessão: o perfil precisa ter `entrega.publicar`. Os quatro perfis embutidos **não** a recebem por padrão; a CLI soma a concessão quando `--entregas` é passado, como faz com as ferramentas MCP de leitura.

Fluxo da função:

1. Valida `caminho`: começa com `/saida/`, normalizado (sem `..`, sem `//`), um nível só (sem subpastas) e nome com caracteres seguros (`[A-Za-z0-9._-]`).
2. Busca o conteúdo com `ler_saida(nome, limite)` (abaixo). Falha vira erro curto.
3. Valida o arquivo: extensão em `{.xlsx, .docx, .pptx, .pdf, .csv, .txt, .png}`; tamanho até 5 MiB; para `.xlsx`, `.docx` e `.pptx` (que são zip), recusa se houver `vbaProject.bin` ou se o zip for inválido.
4. Monta o pedido: `Arquivo`, `Tamanho`, `Hash` (SHA-256 curto), `Destinatário`, `Tipo`.
5. A aprovação acontece no registro (ponto anterior). A função só roda depois do `APROVADO`.
6. Grava em `<pasta_entregas>/<destinatario>/<UTC aaaammdd-hhmmss>-<nome>` com permissão de gravação exclusiva (não sobrescreve) e acrescenta uma linha a `<pasta_entregas>/registro.jsonl` com data, arquivo, destinatário, hash e perfil. Devolve `{"entregue": true, "arquivo": "<destinatario>/<nome gravado>", "sha256": "<hash>"}`.

A ordem importa: a validação (passos 1 a 3) roda **antes** do pedido de aprovação, para a pessoa nunca aprovar um arquivo que seria recusado depois. Por isso o montador do pedido executa os passos 1 a 3 e guarda o conteúdo validado numa memória por chamada (indexada pelo `call_id`). A função da ferramenta grava exatamente esse conteúdo, sem ler `/saida` de novo, e a memória é descartada no fim da chamada. Assim o hash que a pessoa viu é o do arquivo que será gravado.

Só existe na variante REPL. A variante JSON não produz arquivos e não registra `entrega.publicar`.

## Ler `/saida` no host

`DockerSandbox.ler_saida(nome: str, limite_bytes: int) -> bytes` lê o arquivo por um `exec` dentro do contêiner.

O desenho inicial previa `container.get_archive`, mas a API de arquivo do Docker (`docker cp`) não enxerga tmpfs, e o `/saida` é um tmpfs: ela devolve 404 mesmo com o arquivo lá. O teste de integração com Docker real pegou o problema; os testes unitários, com um tar em memória, não.

- O nome passa por `[A-Za-z0-9._-]+` antes de qualquer coisa. O caminho e o limite vão como argumentos do processo, nunca por shell.
- Um programa Python curto (constante `_LEITOR_DE_SAIDA`) abre o caminho com `O_NOFOLLOW`, confere por `fstat` que é arquivo regular e que cabe no limite, e escreve o conteúdo na saída. Os códigos de saída são 0 (ok), 2 (não existe), 3 (link simbólico, diretório ou outro tipo) e 4 (acima do limite).
- `_interpretar_leitura(codigo, saida, limite)` traduz o código em `SandboxErro` com mensagem curta e confere o tamanho de novo no host.
- Funciona entre células, com o contêiner vivo. Sem contêiner, levanta `SandboxErro`.

`ler_saida` é passado à `registrar_entrega` como função, para a ferramenta não conhecer o sandbox.

## Espera por aprovação e prazos (D12)

O limite de 30 s da célula é imposto no sandbox (`Repl.run_cell`) e reforçado no host (`prazo` em `DockerSandbox.executar_detalhado`). Uma pessoa não responde nesse tempo.

O primeiro desenho creditava o tempo na resposta da ferramenta (`espera_s`), mas o prazo da célula vence **durante** a espera, antes de a resposta chegar. Um teste com Docker real mostrou a célula interrompida. O desenho final avisa o sandbox por mensagens:

- Nova mensagem host → sandbox, `EsperaHumana(ativa: bool)`: `ativa=True` quando a espera por aprovação começa e `ativa=False` quando termina.
- O `Registro` as dispara ao redor da chamada do aprovador (`observar_espera`), com o aviso de fim num `finally`, de modo que ele sai mesmo se o aprovador levantar. O orquestrador liga `observar_espera` ao `avisar_espera` do `DockerSandbox`, quando ele existe.
- A `Ponte` do sandbox guarda o instante em que a espera abriu. A propriedade `credito_s` soma o crédito fechado e o tempo decorrido da espera aberta, e `run_cell` a lê a cada volta de 50 ms, então o prazo da célula cresce enquanto a espera dura. Uma espera que o host esqueceu de fechar é fechada quando o `tool_result` chega.
- `ToolResult.espera_s` continua existindo, mas agora só serve ao **host**: `DockerSandbox.executar_detalhado` soma o valor ao seu próprio prazo e o devolve em `ResultadoExecucao.espera_aprovacao_s`, e o orquestrador o subtrai do orçamento de tempo do pedido. Os orçamentos de iterações e de tokens não mudam.
- A espera tem teto próprio: `prazo_aprovacao_s` (120 s). Passou, `EXPIRADO`.

Quem dispara os avisos é só o host. O código do modelo não consegue pedir crédito: `EsperaHumana` só chega pelo canal do host, e `espera_s` só vem dentro de um `tool_result` montado pelo registro.

## Configuração e CLI

- CLI do harness: `--entregas PASTA` liga `entrega.publicar` (e a concessão). Com a entrada em terminal interativo, o aprovador é o `AprovadorDeTerminal`; sem terminal, `sempre_recusa`. `--prazo-aprovacao N` ajusta o teto (padrão 120).
- Avaliações: `--aprovador {recusar,aprovar}` (padrão `recusar`) em `ConfigExecucao.aprovador`. A escolha entra no relatório (`aprovador`, some do JSON quando é o padrão), e `--salvar-linha-de-base` a recusa quando for `aprovar`. As avaliações não ligam `--entregas`; a ferramenta existe para a peça 3.

## Testes

Unitários, sem Docker:

- `AprovadorDeTerminal`: aprova com `s`/`sim`, recusa com `n`, vazio, lixo e fim da entrada, expira com a thread presa, limpa caracteres de controle e corta em 200.
- `limpar_para_exibicao` com quebras de linha, escapes ANSI e texto longo.
- Registro: sem aprovador não muda nada; com `sempre_aprova` executa; com `sempre_recusa` e com `EXPIRADO` devolve as mensagens certas; chamada com `nivel+trifecta` pergunta uma vez; eventos `aprovacao_pedida` e `aprovacao_resposta`; `espera_s` presente só com aprovação.
- `entrega.publicar`: caminho inválido (`..`, subpasta, fora de `/saida/`), destinatário desconhecido, extensão fora da lista, arquivo grande, zip com macro, zip inválido, gravação sem sobrescrever, linha no `registro.jsonl`, e que a validação roda antes do pedido.
- `ToolResult.espera_s`: valida finito e não negativo; a `Ponte` soma o crédito; `run_cell` com crédito não estoura em uma célula que espera mais que `timeout_s`.

Integração, com Docker:

- Célula grava um `.xlsx` em `/saida/`, chama `entrega.publicar` e o arquivo chega a `<entregas>/financeiro/` com `sempre_aprova`; com `sempre_recusa` não chega.
- Uma aprovação que demora mais que `timeout_s` da célula não derruba a célula.
- `ler_saida` recusa link simbólico, diretório e arquivo acima do limite.

Gates: `ruff check`, `ruff format --check`, `mypy harness tests` e a suíte completa, como nas etapas anteriores.

## Riscos

- **Aprovação por hábito.** Uma pessoa que aprova tudo anula a regra. Mitigação: o pedido é curto, verificado pelo host e sem texto do modelo; medir na peça 3 com o aprovador roteirizado.
- **Crédito de prazo mal medido.** O contador do sandbox depende do `espera_s` do host. Mitigação: o prazo do host é recalculado por ele mesmo e vale como segunda barreira; o teto de 120 s limita o dano.
- **Código do modelo escrevendo `/saida` durante a leitura.** O host lê o arquivo uma vez, por `exec` com `O_NOFOLLOW`; o hash mostrado é o do mesmo conteúdo que será gravado, guardado em memória entre o pedido e a gravação.
- **Entrega não reversível.** Uma entrega gravada não tem `reverter`. Mitigação: nunca sobrescreve, registro em `registro.jsonl`, e a reversão fica para a peça de versionamento.
- **Terminal sem TTY.** Sem terminal interativo o aprovador recusa tudo. Isso é seguro, mas `entrega.publicar` fica inutilizável fora do terminal até haver outro aprovador.
