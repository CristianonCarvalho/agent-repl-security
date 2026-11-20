"""Interpretação do turno do modelo (contrato 3 da spec).

Regras do formato:
- Um turno tem no máximo UM bloco de código ```python.
- O texto fora do bloco é raciocínio visível: vai para o trace e não é executado.
- Blocos de outra linguagem, ou sem linguagem, não são código: ficam no raciocínio.
"""

from pydantic import BaseModel, ConfigDict


class TurnParseError(Exception):
    """Turno malformado. A mensagem é em português e volta ao modelo como observação."""


class Turn(BaseModel):
    """Um turno do modelo já separado em raciocínio e código."""

    model_config = ConfigDict(frozen=True)

    # Texto fora do bloco de código, sem espaços nas pontas.
    reasoning: str
    # Código do bloco ```python; None quando o turno não tem bloco.
    code: str | None = None


_CERCA = "```"


def parse_turn(texto: str) -> Turn:
    """Separa o turno do modelo em raciocínio e código.

    Levanta TurnParseError quando há mais de um bloco python ou quando o bloco python
    não é fechado. Nenhuma outra exceção sai daqui.
    """
    raciocinio: list[str] = []
    blocos: list[str] = []
    # Linhas do bloco python em curso; None quando não há bloco python aberto.
    bloco_atual: list[str] | None = None
    # Verdadeiro dentro de bloco de outra linguagem (ou sem linguagem): vai para o raciocínio.
    em_bloco_de_texto = False

    for linha in texto.split("\n"):
        marca = linha.strip()

        if bloco_atual is not None:
            # Dentro do bloco python: só a cerca de fechamento encerra o bloco.
            if marca == _CERCA:
                blocos.append("\n".join(bloco_atual))
                bloco_atual = None
            else:
                bloco_atual.append(linha)
        elif em_bloco_de_texto:
            # Dentro de bloco que não é python: tudo fica no raciocínio até a cerca de fechamento.
            raciocinio.append(linha)
            if marca == _CERCA:
                em_bloco_de_texto = False
        elif marca.startswith(_CERCA):
            # Cerca de abertura: a primeira palavra depois das crases é a linguagem.
            linguagem = marca[len(_CERCA) :].split()
            if linguagem and linguagem[0].lower() == "python":
                bloco_atual = []
            else:
                em_bloco_de_texto = True
                raciocinio.append(linha)
        else:
            raciocinio.append(linha)

    if bloco_atual is not None:
        raise TurnParseError(
            "O bloco ```python não foi fechado: falta a linha ``` que encerra o código."
        )
    if len(blocos) > 1:
        raise TurnParseError(
            f"O turno tem {len(blocos)} blocos ```python; envie no máximo um por turno."
        )

    return Turn(
        reasoning="\n".join(raciocinio).strip(),
        code=blocos[0] if blocos else None,
    )
