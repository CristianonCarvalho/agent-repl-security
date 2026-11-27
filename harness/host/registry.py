"""Registro de ferramentas do host (spec v0, "Ferramentas do host" e regras 3 e 4; D10).

As ferramentas executam aqui, no host. O sandbox só envia `tool_call` e recebe `tool_result`.
O registro é uma instância (não um dicionário global), para que cada sessão tenha o seu.

Nesta etapa as propriedades da trifecta são só declaradas: a política que as usa é a Fase 4.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from harness.host.aprovacao import Aprovador, PedidoDeAprovacao, RespostaDeAprovacao
from harness.host.perfil import Perfil
from harness.host.politica import Decisao, Exposicao, avaliar
from harness.sandbox.protocol import ToolCall, ToolResult


class Nivel(Enum):
    """Classificação de uma ferramenta (regra 4). Só LEITURA roda sem aprovação humana."""

    LEITURA = "leitura"
    ESCRITA_LOCAL = "escrita_local"
    ESCRITA_EXTERNA = "escrita_externa"


class ErroFerramenta(Exception):
    """Erro esperado de uma ferramenta, com mensagem já pensada para o modelo.

    Só o texto desta exceção (e o de PermissionError) chega ao modelo. Qualquer outra
    exceção vira uma mensagem genérica, para não vazar caminhos e detalhes do host.
    """


# Assinatura de uma ferramenta: recebe os argumentos já validados e o perfil do solicitante.
type Funcao = Callable[[Any, Perfil], Any]

_NOME_VALIDO: Final = re.compile(r"[a-z_]+\.[a-z_]+")
# Limite de caracteres do nome da ferramenta que volta ao modelo em mensagens de erro.
_TAMANHO_NOME_NA_MENSAGEM: Final = 80
# Teto do JSON serializado (utf-8) do resultado de uma ferramenta: 1 MiB por padrão.
LIMITE_RESULTADO_PADRAO: Final = 1024 * 1024
MENSAGEM_POLITICA: Final = (
    "a chamada foi recusada pela política de segurança: a sessão leu conteúdo não confiável "
    "e esta ferramenta envia informações para fora"
)


@dataclass(frozen=True)
class Ferramenta:
    """Descrição imutável de uma ferramenta registrada."""

    nome: str
    nivel: Nivel
    descricao: str
    args: type[BaseModel]
    funcao: Funcao
    dado_privado: bool = False
    conteudo_nao_confiavel: bool = False
    canal_externo: bool = False
    # Exemplo de uso em Python, mostrado por `descoberta.descrever`. Vazio quando não há.
    exemplo: str = ""
    # JSON Schema original (inputSchema) de uma ferramenta MCP. Nas ferramentas do host é None,
    # e `descoberta.descrever` usa o modelo pydantic. Quando existe, ele é a fonte da assinatura.
    esquema: dict[str, Any] | None = None
    montar_pedido: Callable[[Any, Perfil], tuple[tuple[str, str], ...]] | None = None


def _resultado_de_erro(call_id: str, mensagem: str) -> ToolResult:
    return ToolResult(call_id=call_id, ok=False, error=mensagem)


def _mensagem_de_validacao(erro: ValidationError) -> str:
    """Traduz os erros do Pydantic para uma frase curta em português."""
    partes: list[str] = []
    for item in erro.errors():
        campo = ".".join(str(p) for p in item["loc"]) or "argumentos"
        if item["type"] == "missing":
            partes.append(f"argumento obrigatório ausente: '{campo}'")
        elif item["type"] == "extra_forbidden":
            partes.append(f"argumento não aceito: '{campo}'")
        else:
            partes.append(f"valor inválido para '{campo}'")
    return "; ".join(partes) if partes else "argumentos inválidos"


class Registro:
    """Conjunto de ferramentas de uma sessão, com despacho que nunca levanta exceção."""

    def __init__(
        self,
        limite_resultado_bytes: int = LIMITE_RESULTADO_PADRAO,
        *,
        politica: bool = False,
        aprovador: Aprovador | None = None,
        prazo_aprovacao_s: float = 120.0,
    ) -> None:
        # bool é subclasse de int; um True/False aqui seria um erro de quem chama.
        if (
            isinstance(limite_resultado_bytes, bool)
            or not isinstance(limite_resultado_bytes, int)
            or limite_resultado_bytes <= 0
        ):
            raise ValueError("limite_resultado_bytes precisa ser um inteiro maior que zero")
        self._limite_resultado = limite_resultado_bytes
        self._ferramentas: dict[str, Ferramenta] = {}
        self._politica = politica
        self._aprovador = aprovador
        self._prazo_aprovacao_s = prazo_aprovacao_s
        self.exposicao = Exposicao()
        self._ao_decidir: Callable[[dict[str, Any]], None] | None = None
        self._ao_aprovar: Callable[[str, dict[str, Any]], None] | None = None
        self._ao_esperar: Callable[[bool], None] | None = None

    @property
    def politica_ligada(self) -> bool:
        """Verdadeiro quando a política da trifecta vale para este registro."""
        return self._politica

    def observar_politica(self, ao_decidir: Callable[[dict[str, Any]], None] | None) -> None:
        """Registra o callback que recebe cada decisão da política (para o trace)."""
        self._ao_decidir = ao_decidir

    def observar_aprovacao(self, ao_evento: Callable[[str, dict[str, Any]], None] | None) -> None:
        """Registra o callback que recebe eventos de aprovação (para o trace)."""
        self._ao_aprovar = ao_evento

    def observar_espera(self, ao_esperar: Callable[[bool], None] | None) -> None:
        """Registra o callback avisado quando uma aprovação humana começa ou termina."""
        self._ao_esperar = ao_esperar

    def _avisar_espera(self, ativa: bool) -> None:
        if self._ao_esperar is None:
            return
        try:
            self._ao_esperar(ativa)
        except Exception:
            # Falha do consumidor nunca muda o resultado da chamada.
            pass

    def _notificar_aprovacao(self, tipo: str, dados: dict[str, Any]) -> None:
        if self._ao_aprovar is None:
            return
        try:
            self._ao_aprovar(tipo, dados)
        except Exception:
            # Falha do consumidor nunca muda o resultado da chamada.
            pass

    def _notificar(self, nome: str, decisao: Decisao) -> None:
        if self._ao_decidir is None:
            return
        try:
            self._ao_decidir(
                {
                    "ferramenta": nome,
                    "decisao": decisao.value,
                    "viu_dado_privado": self.exposicao.viu_dado_privado,
                    "viu_nao_confiavel": self.exposicao.viu_nao_confiavel,
                }
            )
        except Exception:
            # Falha do consumidor nunca muda o resultado da chamada.
            pass

    def ferramenta(
        self,
        *,
        nome: str,
        nivel: Nivel,
        descricao: str,
        args: type[BaseModel],
        dado_privado: bool = False,
        conteudo_nao_confiavel: bool = False,
        canal_externo: bool = False,
        exemplo: str = "",
        esquema: dict[str, Any] | None = None,
        montar_pedido: Callable[[Any, Perfil], tuple[tuple[str, str], ...]] | None = None,
    ) -> Callable[[Funcao], Funcao]:
        """Decorador que registra uma função como ferramenta `grupo.nome`.

        A função original é devolvida sem alteração. Nome fora do formato ou repetido levanta
        ValueError na hora do registro.
        """
        if not _NOME_VALIDO.fullmatch(nome):
            raise ValueError(f"nome de ferramenta inválido: {nome!r} (use grupo.nome)")

        def registrar(funcao: Funcao) -> Funcao:
            if nome in self._ferramentas:
                raise ValueError(f"ferramenta já registrada: {nome!r}")
            self._ferramentas[nome] = Ferramenta(
                nome=nome,
                nivel=nivel,
                descricao=descricao,
                args=args,
                funcao=funcao,
                dado_privado=dado_privado,
                conteudo_nao_confiavel=conteudo_nao_confiavel,
                canal_externo=canal_externo,
                exemplo=exemplo,
                esquema=esquema,
                montar_pedido=montar_pedido,
            )
            return funcao

        return registrar

    def obter(self, nome: str) -> Ferramenta | None:
        """Devolve a ferramenta registrada com esse nome, ou None."""
        return self._ferramentas.get(nome)

    def listar(self) -> tuple[Ferramenta, ...]:
        """Todas as ferramentas registradas, em ordem de nome (base do catálogo do prompt)."""
        return tuple(sorted(self._ferramentas.values(), key=lambda ferramenta: ferramenta.nome))

    def visiveis(self, perfil: Perfil) -> tuple[Ferramenta, ...]:
        """Só as ferramentas que o perfil pode chamar, em ordem de nome (D14).

        É a lista que o prompt, a descoberta e a verificação estática usam.
        """
        return tuple(ferramenta for ferramenta in self.listar() if perfil.permite(ferramenta.nome))

    def despachar(self, chamada: ToolCall, perfil: Perfil) -> ToolResult:
        """Executa uma chamada e devolve o resultado. Nunca levanta exceção.

        Qualquer falha (ferramenta inexistente ou não concedida ao perfil, argumentos inválidos,
        nível que exige aprovação, exceção na ferramenta ou resultado que não vira JSON) volta
        como ToolResult com ok=False e uma mensagem em português.
        """
        try:
            return self._despachar(chamada, perfil)
        except Exception:
            # Última barreira: nenhum erro inesperado pode sair do host para o sandbox.
            return _resultado_de_erro(
                chamada.call_id, "erro interno do host ao atender a ferramenta"
            )

    def _despachar(self, chamada: ToolCall, perfil: Perfil) -> ToolResult:
        nome = chamada.name
        ferramenta = self._ferramentas.get(nome)
        # Ferramenta não concedida responde exatamente como a inexistente: o perfil não descobre
        # o que existe. A função nunca é chamada nesse caso.
        if ferramenta is None or not perfil.permite(nome):
            exibido = nome[:_TAMANHO_NOME_NA_MENSAGEM]
            return _resultado_de_erro(chamada.call_id, f"a ferramenta {exibido!r} não existe")
        try:
            argumentos = ferramenta.args.model_validate(chamada.args)
        except ValidationError as erro:
            return _resultado_de_erro(
                chamada.call_id, f"argumentos inválidos: {_mensagem_de_validacao(erro)}"
            )

        motivos: list[str] = []
        if ferramenta.nivel is not Nivel.LEITURA:
            motivos.append("nivel")
        if self._politica:
            decisao = avaliar(self.exposicao, ferramenta)
            self._notificar(nome, decisao)
            if decisao is not Decisao.PERMITIR:
                motivos.append("trifecta")

        espera: float | None = None
        if motivos:
            if self._aprovador is None:
                if ferramenta.nivel is not Nivel.LEITURA:
                    return _resultado_de_erro(
                        chamada.call_id,
                        f"a ferramenta '{nome}' é de nível {ferramenta.nivel.value} e exige "
                        "aprovação humana; a aprovação ainda não existe, então a chamada foi "
                        "recusada",
                    )
                return _resultado_de_erro(chamada.call_id, MENSAGEM_POLITICA)

            if ferramenta.montar_pedido is not None:
                try:
                    linhas = ferramenta.montar_pedido(argumentos, perfil)
                except ErroFerramenta as erro:
                    return _resultado_de_erro(chamada.call_id, str(erro))
                except PermissionError as erro:
                    return _resultado_de_erro(chamada.call_id, f"acesso negado: {erro}")
                except Exception:
                    return _resultado_de_erro(
                        chamada.call_id, f"falha ao executar a ferramenta '{nome}'"
                    )
            else:
                linhas = (("Ferramenta", nome),)

            motivo = "+".join(motivos)
            pedido = PedidoDeAprovacao(ferramenta=nome, motivo=motivo, linhas=linhas)
            self._notificar_aprovacao("aprovacao_pedida", {"ferramenta": nome, "motivo": motivo})
            inicio = time.monotonic()
            self._avisar_espera(True)
            try:
                resposta = self._aprovador(pedido, self._prazo_aprovacao_s)
            except Exception:
                resposta = RespostaDeAprovacao.RECUSADO
            finally:
                self._avisar_espera(False)
            espera = min(max(0.0, time.monotonic() - inicio), self._prazo_aprovacao_s)
            self._notificar_aprovacao(
                "aprovacao_resposta",
                {"ferramenta": nome, "resposta": resposta.value, "espera_s": espera},
            )
            if resposta is RespostaDeAprovacao.RECUSADO:
                return _resultado_de_erro(
                    chamada.call_id, "a chamada foi recusada pela pessoa responsável"
                ).model_copy(update={"espera_s": espera})
            if resposta is RespostaDeAprovacao.EXPIRADO:
                return _resultado_de_erro(
                    chamada.call_id, "a aprovação não chegou a tempo"
                ).model_copy(update={"espera_s": espera})

        resultado = self._executar_ferramenta(chamada.call_id, nome, ferramenta, argumentos, perfil)
        if espera is not None:
            return resultado.model_copy(update={"espera_s": espera})
        return resultado

    def _executar_ferramenta(
        self,
        call_id: str,
        nome: str,
        ferramenta: Ferramenta,
        argumentos: Any,
        perfil: Perfil,
    ) -> ToolResult:
        try:
            resultado = ferramenta.funcao(argumentos, perfil)
        except ErroFerramenta as erro:
            return _resultado_de_erro(call_id, str(erro))
        except PermissionError as erro:
            return _resultado_de_erro(call_id, f"acesso negado: {erro}")
        except Exception:
            # Detalhes da exceção ficam de fora da mensagem: podem conter caminhos do host.
            return _resultado_de_erro(call_id, f"falha ao executar a ferramenta '{nome}'")
        finally:
            if self._politica:
                self.exposicao.marcar_ferramenta(ferramenta)
        try:
            # Medido em bytes utf-8 do JSON: é o que o resultado ocupa na ponte.
            tamanho = len(
                json.dumps(resultado, allow_nan=False, ensure_ascii=False).encode("utf-8")
            )
        except (TypeError, ValueError):
            return _resultado_de_erro(
                call_id, f"o resultado de '{nome}' não pode ser convertido em JSON"
            )
        if tamanho > self._limite_resultado:
            # A mensagem não traz o conteúdo nem o tamanho real: só o limite configurado.
            return _resultado_de_erro(
                call_id,
                f"resultado da ferramenta excede o limite de {self._limite_resultado} bytes",
            )
        return ToolResult(call_id=call_id, ok=True, data=resultado)

    def despachante(self, perfil: Perfil) -> Callable[[ToolCall], ToolResult]:
        """Devolve a função que o DockerSandbox chama para cada `tool_call`, já ligada ao perfil."""

        def despachar(chamada: ToolCall) -> ToolResult:
            return self.despachar(chamada, perfil)

        return despachar
