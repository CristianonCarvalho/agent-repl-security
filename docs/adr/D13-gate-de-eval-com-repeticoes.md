# D13 — Gate de eval com várias rodadas por caso

- Status: aceita
- Data: 20/11/2026
- Relacionadas: D9 (engenharia), seção Pipeline de CI/CD, spec v0

## Contexto

O gate da etapa 5 do CI exige "sucesso ≥ linha de base − 2 p.p.". O conjunto tem 35 casos, então cada caso vale cerca de 2,9 p.p. Mesmo com temperatura de 0 a 0,2, o resultado varia entre rodadas. Com uma única rodada, o gate dá falso alarme ou deixa passar regressões reais.

## Decisão

- Cada caso roda de **3 a 5 vezes** por execução de eval; o valor inicial é 3.
- O gate compara a **média** da taxa de sucesso com a linha de base, usando uma margem calibrada ao ruído medido (desvio entre rodadas da própria linha de base), em vez de 2 p.p. fixos.
- A linha de base é gravada com o mesmo número de repetições.
- O gate dos ataques de injeção continua absoluto: zero ações proibidas em qualquer rodada.
- O teto de custo por execução de eval passa a considerar o número de repetições.

## Alternativas descartadas

- Ampliar o conjunto para 10 casos por categoria: mais gabarito e mais custo por rodada; fica como evolução futura.
- Manter uma rodada e 2 p.p.: aceita falsos alarmes.

## Consequências

- O custo e o tempo dos evals no CI multiplicam por N; os evals noturnos podem usar N maior que os de PR.
- É preciso registrar o desvio por rodada na linha de base antes de ligar o gate no CI.
- O trace e o relatório de eval anexado ao PR passam a mostrar média e variação, não um número único.
