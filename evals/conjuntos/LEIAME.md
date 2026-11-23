# Conjuntos congelados

Esta pasta guarda o conjunto de casos de avaliação como foi gerado pelo código, com uma semente.
O `v1-semente42.json` tem os 35 casos: 20 ativos (rodam e entram na taxa) e 15 pendentes (não rodam).

- O arquivo é a referência. O código gera o conjunto a partir dos dados; o teste
  `test_conjunto_gerado_pelo_codigo_e_igual_ao_arquivo_versionado` confere que o código ainda gera
  exatamente este arquivo, byte a byte. Se o código divergir, o teste falha.
- Quando criar uma versão nova: se a pergunta, o gabarito, o perfil, a situação ou o comportamento
  esperado de algum caso mudarem, ou se a geração de dados mudar o que os casos dizem. Mudar isso
  no v1 invalida as linhas de base já gravadas.
- Como criar: `uv run python -m harness.evals congelar --versao v2` (a semente padrão é 42). Depois,
  troque `VERSAO_CONJUNTO` em `harness/evals/conjunto.py` para `"v2"`. O comando recusa sobrescrever
  um arquivo que já existe.
- Não edite um arquivo congelado à mão.
