"""Servidor MCP de teste `contas` ligado à avaliação (T19-c).

O conjunto `injecao-avancada` mede injeção que chega por um canal que não é arquivo: a resposta e a
descrição de uma ferramenta MCP. Este módulo liga o servidor de teste
(`fixtures/servidor_contas.py`) ao registro e ao perfil de uma rodada, nas duas variantes de
agente (REPL e JSON).

Decisões deste módulo (não estão na spec):
- Os fatos de que o servidor precisa saem de `FatosVerdadeiros` para um JSON na pasta temporária da
  execução, FORA da pasta de dados: o arquivo não aparece no catálogo nem monta no sandbox. O
  caminho vai ao servidor por `env` (o ambiente do harness não é herdado).
- Política de concessão igual à da CLI (`--mcp`): toda ferramenta MCP entra como escrita externa,
  salvo as listadas em `leitura` na configuração, e só as de leitura são concedidas ao perfil. O
  servidor de teste lista as duas ferramentas como leitura, para o agente poder chamá-las; a
  política global não muda, e o perfil só ganha essas concessões nas rodadas que usam o servidor.
- Ciclo de vida: `servidor_de_contas` sobe o servidor antes do pedido e o encerra sempre depois,
  inclusive com erro. Falha ao iniciar (ou ferramenta que o registro não aceitou) levanta
  `ErroMcpDeAvaliacao`, e o executor a trata como erro de infraestrutura daquela rodada.
- O servidor roda com o Python do próprio harness (`sys.executable`) e só a biblioteca padrão.
"""

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from harness.evals.dados import FatosVerdadeiros
from harness.evals.fixtures import servidor_contas
from harness.host.mcp.adaptador import registrar_servidores_mcp
from harness.host.mcp.config import ConfigMcp
from harness.host.perfil import Perfil
from harness.host.registry import Nivel, Registro

NOME_ARQUIVO_FATOS: Final = "fatos_contas.json"
SUFIXO_CONJUNTO_MCP: Final = "+mcp-contas"
FERRAMENTAS_DO_SERVIDOR: Final = frozenset(
    {
        f"{servidor_contas.NOME_SERVIDOR}.{servidor_contas.FERRAMENTA_VENCIDOS}",
        f"{servidor_contas.NOME_SERVIDOR}.{servidor_contas.FERRAMENTA_CONTRATOS}",
    }
)


class ErroMcpDeAvaliacao(Exception):
    """O servidor MCP de teste não subiu como esperado. A mensagem não traz texto do servidor."""


def gravar_fatos_de_contas(fatos: FatosVerdadeiros, pasta: Path) -> Path:
    """Grava em `pasta` o JSON que o servidor lê (valores em reais, como texto). Devolve o caminho.

    A pasta precisa ficar fora da pasta de dados da avaliação.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / NOME_ARQUIVO_FATOS
    conteudo = {
        "vencido_por_cliente": {
            nome: str(valor) for nome, valor in fatos.vencido_por_cliente.items()
        },
        "valor_mensal_por_cliente": {
            nome: str(valor) for nome, valor in fatos.valor_mensal_por_cliente.items()
        },
    }
    caminho.write_text(
        json.dumps(conteudo, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return caminho


def config_mcp_de_contas(arquivo_fatos: Path) -> ConfigMcp:
    """Configuração MCP do servidor `contas`: stdio, fatos pelo `env`, consultas como leitura."""
    return ConfigMcp.model_validate(
        {
            "servidores": {
                servidor_contas.NOME_SERVIDOR: {
                    "comando": sys.executable,
                    "args": [str(Path(servidor_contas.__file__).resolve())],
                    "env": {servidor_contas.VARIAVEL_FATOS: str(arquivo_fatos)},
                    "leitura": [
                        servidor_contas.FERRAMENTA_VENCIDOS,
                        servidor_contas.FERRAMENTA_CONTRATOS,
                    ],
                }
            }
        }
    )


@contextmanager
def servidor_de_contas(registro: Registro, perfil: Perfil, config: ConfigMcp) -> Iterator[Perfil]:
    """Sobe o servidor, registra as ferramentas e devolve o perfil com as concessões de leitura.

    O servidor é encerrado ao sair do bloco, sempre. Levanta `ErroMcpDeAvaliacao` se ele não subir
    ou se faltar alguma das duas ferramentas no registro.
    """
    gerente = registrar_servidores_mcp(registro, config)
    try:
        if gerente.falhas:
            raise ErroMcpDeAvaliacao("o servidor MCP de teste não iniciou")
        registradas = set(gerente.ferramentas)
        if not FERRAMENTAS_DO_SERVIDOR <= registradas:
            raise ErroMcpDeAvaliacao("o servidor MCP de teste não registrou as duas ferramentas")
        concedidas = [
            ferramenta.nome
            for ferramenta in registro.listar()
            if ferramenta.nome in registradas and ferramenta.nivel is Nivel.LEITURA
        ]
        yield perfil.com_concessoes(concedidas)
    finally:
        gerente.fechar()
