# D14 — Perfis por ferramenta (concessões) na v1

- Status: proposta (ideia para a v1; não implementada)
- Data: 20/11/2026
- Relacionadas: D5 (ferramentas e MCP na v1), D10 (conteúdo não confiável por arquivo), seção Perfis de acesso da spec v0

## Contexto

Na v0 o perfil controla o acesso por departamento, em dois lugares:

1. **Montagem do contêiner:** só os departamentos do perfil entram em `/dados`, somente leitura. É um controle estrutural: o departamento fora do perfil não existe dentro do sandbox.
2. **Dentro de cada ferramenta:** o registro passa o perfil à função, e cada função filtra o que devolve (por exemplo, `catalogo.listar` levanta `PermissionError` fora do perfil).

Não existe lista de ferramentas permitidas por perfil, e a checagem depende de cada ferramenta lembrar de aplicá-la: uma ferramenta nova que esquecesse a checagem vazaria dados. Além disso, o host não enxerga o que o sandbox lê em `/dados`, o que obrigou o D10 a rastrear arquivos abertos.

Se na v1 os dados passarem a vir como respostas de ferramentas (MCP, D5), cada arquivo seria a resposta de uma chamada.

## Decisão (proposta)

Substituir, para dados servidos por ferramenta, o controle estrutural por **concessões** no perfil, avaliadas pelo registro antes de executar cada chamada:

```python
Perfil(
    "Vendas",
    concessoes={
        "catalogo.listar": {"departamento": {"vendas", "comercial"}},
        "pedidos.consultar": {},
        "contratos.ler": {"departamento": {"comercial"}},
    },
)
```

- O `despachar` consulta as concessões antes de chamar a função; a regra padrão é **negar**.
- Cada ferramenta declara o recurso que acessa (departamento, sensibilidade) para a concessão ter sobre o que decidir.
- A checagem fica num lugar só (o registro), não em cada ferramenta.
- Como toda leitura passa pela ponte, o host vê cada acesso e pode marcar a resposta como não confiável no momento da chamada, tornando a trifecta (regra 3) verificável sem rastrear arquivos abertos.
- Dados grandes: a ferramenta pode materializar o resultado num diretório da sessão, montado somente leitura e registrado no trace, em vez de devolvê-lo em JSON pela ponte (limite de 16 MiB por linha).

## Transição

Gradual. A montagem de `/dados` permanece como defesa em profundidade, e as concessões entram por cima. Um teste de consistência compara as duas camadas e falha se divergirem (concessão a um departamento que não está montado, ou o contrário).

## Alternativas

- Manter só o controle por montagem e filtro dentro da ferramenta (v0): simples, mas a checagem é distribuída e fácil de esquecer.
- Só uma lista de ferramentas permitidas por perfil, sem escopo por parâmetro: mais simples, mas grossa demais para departamentos.

## Consequências

- Perde-se a garantia estrutural para dados servidos por ferramenta: a política do registro vira o ponto único de falha e precisa de testes pesados, inclusive de propriedade com Hypothesis.
- O tráfego de dados pela ponte exige tratar volume (limites, materialização em arquivo).
- Depende da adoção de MCP ou de ferramentas de dados na v1; só então a decisão deve ser revisada e, se confirmada, passar a "aceita".
