# Linhas de base

Cada arquivo aqui é um relatório de avaliação sem caminhos de trace, usado como referência para
comparar execuções futuras. Ele é versionado no git.

Regras para gravar uma linha de base:

- Só execuções do conjunto completo: sem `--categoria` e sem `--grupo`.
- Pelo menos 3 rodadas por caso (`--rodadas 3`).
- Árvore do git limpa, com tudo commitado. Sem git, não grava.
- Execução sem teto estourado, sem erro de infraestrutura e sem rodada não executada.
- O nome segue `<modelo>-<versao-do-conjunto>-<prompt8>-<N>x`: modelo em minúsculas e sem barra,
  `prompt8` são os oito primeiros caracteres do hash do prompt, `N` é o número de rodadas. Exemplo:
  `deepseek-v4.1-flash-1017-v1-70682164-5x`.
- Não edite o arquivo à mão e não sobrescreva: o nome já gravado é recusado.

Como gravar:

    uv run python -m harness.evals --salvar-linha-de-base NOME --rodadas 3

Como comparar uma execução com a linha de base:

    uv run python -m harness.evals comparar evals/linhas-de-base/NOME.json avaliacoes/ARQUIVO.json

Saída 0 quando não há regressão provável, 1 quando há, 2 quando não dá para comparar (semente
diferente, nenhum caso em comum) ou quando algum arquivo não pode ser lido. Use `--formato json`
para a saída em JSON. A comparação não chama o modelo e não lê o `.env`.
