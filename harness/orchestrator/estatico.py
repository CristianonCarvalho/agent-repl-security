"""Verificação estática do código gerado pelo modelo (spec v0, passo 5 do ciclo).

Roda no HOST, sobre a árvore sintática (AST) do Python, antes de qualquer execução. Reprovar
aqui é barato: o código nem chega ao contêiner, e o modelo recebe a lista de problemas.

ATENÇÃO: esta verificação NÃO é fronteira de segurança. Ela pega o uso direto e literal de
funções perigosas, mas não pega acesso indireto (por `getattr`, por atributos de
`__builtins__`, por `object.__getattribute__` etc.). O isolamento é o contêiner (D4): sem
rede, sem privilégios, sistema de arquivos somente leitura. A spec chama esta camada de
"extra", e os testes registram os desvios conhecidos.

Decisões deste módulo (não estão na spec):
- A lista de imports proibidos inclui `builtins` e `sys`. Sem `builtins`, o código poderia
  importar o módulo e chamar `eval` por atributo, contornando a checagem de `eval` por nome.
  `sys` dá acesso a `sys.modules`, pelo mesmo motivo.
- Chamadas proibidas só valem pelo nome nu (`eval(...)`). `df.eval(...)` do pandas é legítimo.
- Qualquer falha inesperada dentro da verificação vira um problema, e o código não roda
  (falha fechada). A função nunca levanta exceção.
"""

import ast
from collections.abc import Collection
from typing import Final

# Módulos cujo import é recusado, em `import x` e em `from x import y`, inclusive submódulos.
IMPORTS_PROIBIDOS: Final = frozenset(
    {
        "subprocess",
        "socket",
        "ctypes",
        "multiprocessing",
        "pty",
        "signal",
        "gc",
        "importlib",
        "resource",
        "inspect",
        "builtins",
        "sys",
    }
)
# Funções que executam ou compilam código dinâmico, quando chamadas pelo nome nu.
CHAMADAS_PROIBIDAS: Final = frozenset({"__import__", "eval", "exec", "compile"})


def verificar(
    codigo: str, ferramentas: Collection[str], limite_caracteres: int = 8000
) -> list[str]:
    """Devolve a lista de problemas do código, em português. Lista vazia significa aprovado.

    `ferramentas` são os nomes "grupo.nome" registrados. Chamadas literais `tools.grupo.nome(...)`
    com nome fora dessa coleção são reprovadas. Nunca levanta exceção.
    """
    try:
        return _verificar(codigo, ferramentas, limite_caracteres)
    except Exception:
        # Falha fechada: se a própria verificação quebrar, o código não é executado.
        return ["falha interna na verificação estática; o código não foi executado"]


def _verificar(codigo: str, ferramentas: Collection[str], limite_caracteres: int) -> list[str]:
    if len(codigo) > limite_caracteres:
        return [
            f"o código tem {len(codigo)} caracteres e passa do limite de {limite_caracteres}; "
            "divida o trabalho em células menores"
        ]
    try:
        arvore = ast.parse(codigo)
    except SyntaxError as erro:
        return [f"linha {erro.lineno}: erro de sintaxe: {erro.msg}"]
    except (ValueError, RecursionError, MemoryError) as erro:
        return [f"o código não pôde ser analisado: {type(erro).__name__}"]

    achados: list[tuple[int, str]] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for alias in no.names:
                raiz = alias.name.split(".")[0]
                if raiz in IMPORTS_PROIBIDOS:
                    achados.append((no.lineno, _import_proibido(alias.name)))
        elif isinstance(no, ast.ImportFrom):
            if no.module is not None and no.module.split(".")[0] in IMPORTS_PROIBIDOS:
                achados.append((no.lineno, _import_proibido(no.module)))
        elif isinstance(no, ast.Call):
            if isinstance(no.func, ast.Name) and no.func.id in CHAMADAS_PROIBIDAS:
                achados.append((no.lineno, f"chamada proibida: '{no.func.id}(...)'"))
            nome = _nome_de_ferramenta(no.func)
            if nome is not None and nome not in ferramentas:
                achados.append(
                    (
                        no.lineno,
                        f"ferramenta desconhecida: 'tools.{nome}'; confira os nomes no "
                        "catálogo de ferramentas das instruções",
                    )
                )

    achados.sort(key=lambda achado: achado[0])
    return [f"linha {linha}: {mensagem}" for linha, mensagem in achados]


def _import_proibido(modulo: str) -> str:
    return f"import proibido: '{modulo}' não é permitido no REPL"


def _nome_de_ferramenta(funcao: ast.expr) -> str | None:
    """Devolve "grupo.nome" se a chamada for `tools.grupo.nome`, senão None."""
    if (
        isinstance(funcao, ast.Attribute)
        and isinstance(funcao.value, ast.Attribute)
        and isinstance(funcao.value.value, ast.Name)
        and funcao.value.value.id == "tools"
    ):
        return f"{funcao.value.attr}.{funcao.attr}"
    return None
