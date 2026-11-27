"""Laço do agente de tool calling em JSON (T18a): a comparação com o REPL (Code Mode).

Tem a mesma interface pública do `Orquestrador` (`executar_pedido`), o mesmo `Orcamento`, o mesmo
`ResultadoPedido` e os mesmos `MotivoTermino`. Não há sandbox nem verificação estática: cada
chamada de ferramenta vai direto ao despachante do registro. Nunca levanta exceção.

Decisões deste módulo (não estão na spec):
- Um turno é uma chamada ao modelo. As chamadas da resposta rodam em ordem, uma por vez. Antes
  de despachar, confere-se que o nome é de uma ferramenta visível ao perfil e que os argumentos
  são um objeto JSON que casa com o modelo pydantic da ferramenta. Com isso, a chamada inválida
  nunca chega à função. A ferramenta negada responde como inexistente, pelo próprio registro.
- Turno inválido é o que não tem NENHUMA chamada válida: texto sem chamada, ou só chamadas
  inválidas. Turno com uma chamada válida zera o contador de inválidos seguidos. O limite é o
  mesmo `max_invalidos_seguidos` do REPL.
- `finalizar` é tratado pelo laço, não é ferramenta do registro. Válido: encerra com FINAL e as
  chamadas que vieram depois dele nesse turno não são executadas. Inválido: vira erro de chamada.
- Argumentos em branco valem como objeto vazio (alguns provedores mandam texto vazio para
  ferramenta sem parâmetros). Qualquer outro texto que não seja um objeto JSON é inválido.
- O orçamento de tempo também é conferido depois da chamada ao modelo, como no REPL. Se o tempo
  acabou, nenhuma ferramenta da resposta roda: cada chamada recebe uma mensagem de "não
  executada", porque a API exige uma resposta para cada id.
- O resumo final (orçamento esgotado) usa `tool_choice="none"` com o mesmo esquema de
  ferramentas. Sem o esquema, alguns provedores recusam o histórico que já tem `tool_calls`. O
  texto do resumo é a resposta, com `completo=False`.
- Resultado de ferramenta: JSON compacto (ou `erro: <motivo>` para erro), no envelope
  `<saida_nao_confiavel>` com a truncagem de `envelopar_resultado` (limite padrão de 8.000
  caracteres, com aviso e dica de paginar).
- O agente JSON não tem REPL para carregar contexto. Um pedido com `contexto` termina com
  ERRO_IRRECUPERAVEL sem chamar o modelo. O executor nunca passa contexto.
- `pedido_iniciado` traz `variante`. Os demais eventos têm os nomes e campos do REPL, sem
  `verificacao_estatica` nem `execucao`.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from harness.host.perfil import Perfil
from harness.host.registry import Registro
from harness.llm.base import ChamadaFerramenta, ErroLLM, Mensagem, ModeloComFerramentas, RespostaLLM
from harness.llm.ferramentas import EsquemaFerramenta, nome_do_registro, nome_para_api
from harness.orchestrator.loop import (
    TEXTO_RESUMO_PADRAO,
    Evento,
    MotivoTermino,
    Orcamento,
    Passo,
    ResultadoPedido,
)
from harness.orchestrator.observation import envelopar_resultado
from harness.orchestrator.prompt import montar_prompt_sistema
from harness.sandbox.contexto import ValorContexto
from harness.sandbox.protocol import ToolCall, ToolResult

NOME_FINALIZAR: Final = "finalizar"
NOME_PROMPT_JSON: Final = "sistema_json.md"
LIMITE_RESULTADO_PADRAO: Final = 8000
MENSAGEM_SEM_FERRAMENTA: Final = (
    "[sem chamada] Este turno não chamou nenhuma ferramenta, então nada foi executado. Chame uma "
    "ferramenta ou, para terminar, chame finalizar(resposta, fontes). Texto fora de finalizar não "
    "é resposta final."
)
MENSAGEM_NAO_EXECUTADA: Final = "[orçamento de tempo esgotado; a ferramenta não foi executada]"
MENSAGEM_ARGUMENTOS_NAO_OBJETO: Final = (
    "argumentos inválidos: os argumentos precisam ser um objeto JSON"
)
MENSAGEM_FINALIZAR_INVALIDO: Final = (
    "argumentos inválidos: confira 'resposta' (texto não vazio) e 'fontes' (lista não vazia de "
    "textos não vazios)"
)
TEXTO_FINAL_RECEBIDO: Final = "FINAL recebido."
TAMANHO_NOME_NO_PASSO: Final = 80
_NOME_SEM_NOME: Final = "sem_nome"
_INSTRUCAO_RESUMO: Final = (
    "Faça um resumo, em português e em texto simples: o que você já obteve, com as fontes "
    "usadas, e o que faltou para responder ao pedido. Não chame ferramentas."
)
_PEDIDO_RESUMO: Final = {
    MotivoTermino.ORCAMENTO_ITERACOES: "Você chegou ao limite de iterações deste pedido.",
    MotivoTermino.ORCAMENTO_TOKENS: "Você chegou ao limite de tokens deste pedido.",
    MotivoTermino.ORCAMENTO_TEMPO: "Você chegou ao limite de tempo deste pedido.",
}


class ArgsFinalizar(BaseModel):
    """Argumentos de `finalizar`: a mesma regra do FINAL do REPL (resposta e fontes não vazias)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resposta: str = Field(
        min_length=1,
        description="Resposta em português, com os números e fatos que você verificou.",
    )
    fontes: list[str] = Field(
        min_length=1,
        description=(
            "Lista não vazia de arquivos de onde saíram os dados, com o caminho como aparece "
            "no catálogo."
        ),
    )

    @field_validator("resposta")
    @classmethod
    def _resposta_nao_vazia(cls, valor: str) -> str:
        if not valor.strip():
            raise ValueError("a resposta não pode ser vazia")
        return valor

    @field_validator("fontes")
    @classmethod
    def _fontes_nao_vazias(cls, valor: list[str]) -> list[str]:
        if not all(fonte.strip() for fonte in valor):
            raise ValueError("cada fonte precisa ser um texto não vazio")
        return valor


def _esquema_finalizar() -> EsquemaFerramenta:
    return EsquemaFerramenta(
        nome=NOME_FINALIZAR,
        descricao=(
            "Encerra o pedido com a resposta final e as fontes. Chame só quando tiver os valores "
            "que o pedido exige."
        ),
        parametros=ArgsFinalizar.model_json_schema(),
    )


def esquemas_do_registro(registro: Registro, perfil: Perfil) -> tuple[EsquemaFerramenta, ...]:
    """Esquemas enviados ao modelo: as ferramentas visíveis ao perfil, mais `finalizar`.

    Cada nome passa por `nome_para_api`, então um nome sem mapeamento levanta ValueError.
    """
    esquemas = [
        EsquemaFerramenta(
            nome=nome_para_api(ferramenta.nome),
            descricao=" ".join(ferramenta.descricao.split()),
            parametros=ferramenta.args.model_json_schema(),
        )
        for ferramenta in registro.visiveis(perfil)
    ]
    esquemas.append(_esquema_finalizar())
    return tuple(esquemas)


@dataclass
class _Estado:
    """Tudo o que um pedido acumula. Fica fora da pilha de chamadas para sobreviver a erros."""

    inicio: float = 0.0
    espera_s: float = 0.0
    mensagens: list[Mensagem] = field(default_factory=list)
    hash_prompt: str = ""
    iteracoes: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    invalidos_seguidos: int = 0
    passos: list[Passo] = field(default_factory=list)


class OrquestradorJson:
    """Executa um pedido com tool calling nativo: modelo, ferramentas do registro e orçamento."""

    def __init__(
        self,
        modelo: ModeloComFerramentas,
        registro: Registro,
        perfil: Perfil,
        orcamento: Orcamento | None = None,
        relogio: Callable[[], float] = time.monotonic,
        eventos: Callable[[Evento], None] | None = None,
        limite_resultado_caracteres: int = LIMITE_RESULTADO_PADRAO,
        variante: str = "json",
    ) -> None:
        if limite_resultado_caracteres < 1:
            raise ValueError("limite_resultado_caracteres precisa ser pelo menos 1")
        self._modelo = modelo
        self._registro = registro
        self._perfil = perfil
        self._orcamento = orcamento if orcamento is not None else Orcamento()
        self._relogio = relogio
        self._eventos = eventos
        self._limite = limite_resultado_caracteres
        self._variante = variante
        self._esquemas = esquemas_do_registro(registro, perfil)

    def executar_pedido(
        self, pedido: str, contexto: ValorContexto | None = None
    ) -> ResultadoPedido:
        """Responde ao pedido. Nunca levanta exceção.

        `contexto` não é suportado nesta variante (não há REPL para carregá-lo): quem o passa
        recebe ERRO_IRRECUPERAVEL, sem chamada ao modelo.
        """
        estado = _Estado()
        try:
            self._registro.exposicao.zerar()
            self._registro.observar_politica(self._emitir_politica)
            if contexto is not None:
                return self._encerrar(estado, MotivoTermino.ERRO_IRRECUPERAVEL, "", ())
            return self._laco(pedido, estado)
        except Exception:
            # Defesa em profundidade: um erro interno vira motivo, nunca exceção.
            return self._encerrar(estado, MotivoTermino.ERRO_IRRECUPERAVEL, "", ())

    # --- laço -----------------------------------------------------------------------------

    def _laco(self, pedido: str, estado: _Estado) -> ResultadoPedido:
        estado.inicio = self._relogio()
        prompt = montar_prompt_sistema(self._registro, self._perfil, nome_arquivo=NOME_PROMPT_JSON)
        estado.hash_prompt = prompt.hash_sha256
        estado.mensagens = [
            Mensagem(role="system", content=prompt.texto),
            Mensagem(role="user", content=pedido),
        ]
        self._emitir(
            "pedido_iniciado",
            hash_prompt=prompt.hash_sha256,
            perfil=self._perfil.nome,
            pedido=pedido,
            orcamento=self._orcamento.model_dump(),
            contexto=None,
            variante=self._variante,
        )
        while True:
            motivo = self._motivo_orcamento(estado)
            if motivo is not None:
                return self._resumo_final(estado, motivo)
            encerrado = self._turno(estado)
            if encerrado is not None:
                return encerrado

    def _turno(self, estado: _Estado) -> ResultadoPedido | None:
        """Uma iteração: uma chamada ao modelo e as chamadas da resposta. Pode encerrar o pedido."""
        estado.iteracoes += 1
        try:
            resposta = self._modelo.responder_com_ferramentas(
                list(estado.mensagens), self._esquemas, escolha="auto"
            )
        except Exception as erro:
            return self._falha_do_modelo(estado, erro)
        self._registrar_resposta(estado, resposta, resumo=False)

        if not resposta.chamadas_ferramenta:
            estado.mensagens.append(Mensagem(role="assistant", content=resposta.texto))
            return self._invalido_sem_chamada(estado, resposta.texto)

        chamadas = _numerar(resposta.chamadas_ferramenta)
        estado.mensagens.append(
            Mensagem(role="assistant", content=resposta.texto, chamadas_ferramenta=chamadas)
        )
        raciocinio = resposta.texto.strip()
        if self._restante(estado) <= 0:
            return self._tempo_esgotado(estado, raciocinio, chamadas)
        return self._executar_chamadas(estado, raciocinio, chamadas)

    def _executar_chamadas(
        self, estado: _Estado, raciocinio: str, chamadas: tuple[ChamadaFerramenta, ...]
    ) -> ResultadoPedido | None:
        """Roda as chamadas em ordem. Devolve o resultado final se `finalizar` for válido."""
        validas = 0
        observacoes: list[str] = []
        contagem: dict[str, int] = {}
        respostas: list[tuple[ChamadaFerramenta, str]] = []
        for chamada in chamadas:
            if chamada.nome == NOME_FINALIZAR:
                final = _validar_finalizar(chamada.argumentos_brutos)
                if isinstance(final, ArgsFinalizar):
                    estado.passos.append(
                        Passo(
                            cell_id=None,
                            raciocinio=raciocinio,
                            codigo=None,
                            observacao="\n".join([*observacoes, TEXTO_FINAL_RECEBIDO]),
                            ferramentas=dict(contagem),
                        )
                    )
                    return self._encerrar(
                        estado, MotivoTermino.FINAL, final.resposta, tuple(final.fontes)
                    )
                valida = False
                conteudo = envelopar_resultado(f"erro: {final}", self._limite)
                nome = NOME_FINALIZAR
                self._emitir("observacao", cell_id=None, texto=conteudo)
            else:
                valida, conteudo, nome, espera_s = self._processar_chamada(chamada)
                estado.espera_s += espera_s
            validas += 1 if valida else 0
            respostas.append((chamada, conteudo))
            observacoes.append(conteudo)
            contagem[nome] = contagem.get(nome, 0) + 1

        for chamada, conteudo in respostas:
            estado.mensagens.append(
                Mensagem(
                    role="tool",
                    content=conteudo,
                    tool_call_id=chamada.id,
                    name=chamada.nome,
                )
            )
        estado.passos.append(
            Passo(
                cell_id=None,
                raciocinio=raciocinio,
                codigo=None,
                observacao="\n".join(observacoes),
                ferramentas=dict(contagem),
            )
        )
        estado.invalidos_seguidos = 0 if validas else estado.invalidos_seguidos + 1
        if estado.invalidos_seguidos >= self._orcamento.max_invalidos_seguidos:
            return self._encerrar(estado, MotivoTermino.ERRO_IRRECUPERAVEL, "", ())
        return None

    def _processar_chamada(self, chamada: ChamadaFerramenta) -> tuple[bool, str, str, float]:
        """Executa uma chamada que não é `finalizar`.

        Devolve (válida, conteúdo, nome no passo, espera_s).
        """
        nome_registro = nome_do_registro(chamada.nome)
        nome = nome_registro if nome_registro is not None else chamada.nome
        argumentos = _ler_argumentos(chamada.argumentos_brutos)
        if argumentos is None:
            resultado = ToolResult(
                call_id=chamada.id, ok=False, error=MENSAGEM_ARGUMENTOS_NAO_OBJETO
            )
            valida = False
        else:
            valida = nome_registro is not None and self._casa_com_a_ferramenta(
                nome_registro, argumentos
            )
            inicio = self._relogio()
            resultado = self._registro.despachar(
                ToolCall(call_id=chamada.id, name=nome, args=argumentos), self._perfil
            )
            self._emitir_chamada(
                nome,
                argumentos,
                resultado,
                self._relogio() - inicio,
            )
        if resultado.ok:
            texto = json.dumps(
                resultado.data, ensure_ascii=False, separators=(",", ":"), default=str
            )
        else:
            texto = f"erro: {resultado.error}"
        conteudo = envelopar_resultado(texto, self._limite)
        self._emitir("observacao", cell_id=None, texto=conteudo)
        return valida, conteudo, nome[:TAMANHO_NOME_NO_PASSO], resultado.espera_s or 0.0

    def _casa_com_a_ferramenta(self, nome: str, argumentos: dict[str, Any]) -> bool:
        """Verdadeiro se a ferramenta existe para o perfil e os argumentos casam com o modelo."""
        ferramenta = self._registro.obter(nome)
        if ferramenta is None or not self._perfil.permite(nome):
            return False
        try:
            ferramenta.args.model_validate(argumentos)
        except ValidationError:
            return False
        return True

    def _tempo_esgotado(
        self, estado: _Estado, raciocinio: str, chamadas: tuple[ChamadaFerramenta, ...]
    ) -> ResultadoPedido:
        """Tempo acabou depois da resposta: nada roda, e cada chamada ganha a sua resposta."""
        for chamada in chamadas:
            estado.mensagens.append(
                Mensagem(
                    role="tool",
                    content=MENSAGEM_NAO_EXECUTADA,
                    tool_call_id=chamada.id,
                    name=chamada.nome,
                )
            )
        estado.passos.append(
            Passo(
                cell_id=None,
                raciocinio=raciocinio,
                codigo=None,
                observacao=MENSAGEM_NAO_EXECUTADA,
            )
        )
        return self._resumo_final(estado, MotivoTermino.ORCAMENTO_TEMPO)

    def _invalido_sem_chamada(self, estado: _Estado, texto: str) -> ResultadoPedido | None:
        """Turno sem nenhuma chamada: registra, avisa o modelo e confere o limite de inválidos."""
        estado.invalidos_seguidos += 1
        estado.passos.append(
            Passo(
                cell_id=None,
                raciocinio=texto.strip(),
                codigo=None,
                observacao=MENSAGEM_SEM_FERRAMENTA,
            )
        )
        estado.mensagens.append(Mensagem(role="user", content=MENSAGEM_SEM_FERRAMENTA))
        self._emitir("observacao", cell_id=None, texto=MENSAGEM_SEM_FERRAMENTA)
        if estado.invalidos_seguidos >= self._orcamento.max_invalidos_seguidos:
            return self._encerrar(estado, MotivoTermino.ERRO_IRRECUPERAVEL, "", ())
        return None

    def _falha_do_modelo(self, estado: _Estado, erro: Exception) -> ResultadoPedido:
        """Termina o pedido com ERRO_LLM, com o mesmo detalhe seguro do REPL."""
        tipo = type(erro).__name__
        if isinstance(erro, ErroLLM):
            mensagem = str(erro)
            self._emitir(
                "erro_llm", iteracao=estado.iteracoes, tipo_excecao=tipo, mensagem=mensagem
            )
            detalhe = f"{tipo}: {mensagem}"
        else:
            self._emitir("erro_llm", iteracao=estado.iteracoes, tipo_excecao=tipo)
            detalhe = tipo
        return self._encerrar(estado, MotivoTermino.ERRO_LLM, "", (), detalhe_erro=detalhe)

    def _resumo_final(self, estado: _Estado, termino: MotivoTermino) -> ResultadoPedido:
        """Último turno, sem ferramentas (`tool_choice=none`), que pede o resumo do obtido."""
        estado.iteracoes += 1
        instrucao = f"{_PEDIDO_RESUMO[termino]} {_INSTRUCAO_RESUMO}"
        mensagens = [*estado.mensagens, Mensagem(role="user", content=instrucao)]
        try:
            resposta = self._modelo.responder_com_ferramentas(
                mensagens, self._esquemas, escolha="none"
            )
        except Exception:
            estado.passos.append(
                Passo(
                    cell_id=None,
                    raciocinio="",
                    codigo=None,
                    observacao="[falha ao gerar o resumo]",
                )
            )
            return self._encerrar(estado, termino, TEXTO_RESUMO_PADRAO, ())

        self._registrar_resposta(estado, resposta, resumo=True)
        texto = resposta.texto.strip()
        estado.passos.append(
            Passo(
                cell_id=None,
                raciocinio=texto,
                codigo=None,
                observacao="[resumo final do orçamento]",
            )
        )
        return self._encerrar(estado, termino, texto or TEXTO_RESUMO_PADRAO, ())

    # --- orçamento ------------------------------------------------------------------------

    def _motivo_orcamento(self, estado: _Estado) -> MotivoTermino | None:
        """Primeiro limite esgotado, se algum, antes de uma nova chamada ao modelo."""
        if estado.iteracoes >= self._orcamento.max_iteracoes:
            return MotivoTermino.ORCAMENTO_ITERACOES
        if estado.tokens_entrada + estado.tokens_saida >= self._orcamento.max_tokens:
            return MotivoTermino.ORCAMENTO_TOKENS
        if self._restante(estado) <= 0:
            return MotivoTermino.ORCAMENTO_TEMPO
        return None

    def _restante(self, estado: _Estado) -> float:
        """Segundos que ainda restam ao pedido."""
        return self._orcamento.max_segundos - (self._relogio() - estado.inicio - estado.espera_s)

    # --- saída ----------------------------------------------------------------------------

    def _registrar_resposta(self, estado: _Estado, resposta: RespostaLLM, resumo: bool) -> None:
        estado.tokens_entrada += resposta.tokens_entrada
        estado.tokens_saida += resposta.tokens_saida
        self._emitir(
            "turno_modelo",
            iteracao=estado.iteracoes,
            resumo=resumo,
            texto=resposta.texto,
            tokens_entrada=resposta.tokens_entrada,
            tokens_saida=resposta.tokens_saida,
            modelo=resposta.modelo,
            provedor=resposta.provedor,
            custo=resposta.custo,
            motivo_fim=resposta.motivo_fim,
            tokens_raciocinio=resposta.tokens_raciocinio,
            tentativas_descartadas=resposta.tentativas_descartadas,
        )

    def _encerrar(
        self,
        estado: _Estado,
        motivo: MotivoTermino,
        resposta: str,
        fontes: tuple[str, ...],
        detalhe_erro: str | None = None,
    ) -> ResultadoPedido:
        completo = motivo is MotivoTermino.FINAL
        resultado = ResultadoPedido(
            resposta=resposta,
            fontes=fontes,
            motivo=motivo,
            completo=completo,
            iteracoes=estado.iteracoes,
            tokens_entrada=estado.tokens_entrada,
            tokens_saida=estado.tokens_saida,
            hash_prompt=estado.hash_prompt,
            passos=tuple(estado.passos),
            detalhe_erro=detalhe_erro,
        )
        self._emitir(
            "pedido_finalizado",
            motivo=motivo.value,
            completo=completo,
            iteracoes=estado.iteracoes,
            tokens_entrada=estado.tokens_entrada,
            tokens_saida=estado.tokens_saida,
            resposta=resposta,
            fontes=list(fontes),
        )
        return resultado

    def _emitir_chamada(
        self, nome: str, args: dict[str, Any], resultado: ToolResult, decorrido_s: float
    ) -> None:
        """Evento `chamada_ferramenta`, com os mesmos campos do REPL. Falha nunca sai daqui."""
        try:
            self._emitir(
                "chamada_ferramenta",
                nome=nome,
                args=args,
                ok=resultado.ok,
                tamanho_resultado=_tamanho_do_dado(resultado.data),
                latencia_ms=int(decorrido_s * 1000),
                erro=resultado.error,
            )
        except Exception:
            pass

    def _emitir_politica(self, dados: dict[str, Any]) -> None:
        self._emitir("politica_decisao", **dados)

    def _emitir(self, tipo: str, **dados: Any) -> None:
        """Entrega um evento ao consumidor. Falha do consumidor nunca derruba o laço."""
        if self._eventos is None:
            return
        try:
            self._eventos(Evento(tipo=tipo, dados=dados))
        except Exception:
            pass


def _numerar(chamadas: tuple[ChamadaFerramenta, ...]) -> tuple[ChamadaFerramenta, ...]:
    """Garante id e nome não vazios em cada chamada (a API e a mensagem `tool` exigem os dois)."""
    numeradas: list[ChamadaFerramenta] = []
    for indice, chamada in enumerate(chamadas, start=1):
        numeradas.append(
            ChamadaFerramenta(
                id=chamada.id or f"chamada_{indice}",
                nome=chamada.nome or _NOME_SEM_NOME,
                argumentos_brutos=chamada.argumentos_brutos,
            )
        )
    return tuple(numeradas)


def _ler_argumentos(bruto: str) -> dict[str, Any] | None:
    """Argumentos como objeto. Texto em branco vale como {}. Qualquer outra coisa sem ser objeto
    JSON devolve None. Nunca levanta exceção."""
    if not bruto.strip():
        return {}
    try:
        valor = json.loads(bruto)
    except (ValueError, RecursionError):
        return None
    return valor if isinstance(valor, dict) else None


def _validar_finalizar(bruto: str) -> ArgsFinalizar | str:
    """Argumentos de `finalizar` validados, ou a mensagem curta de erro para o modelo."""
    argumentos = _ler_argumentos(bruto)
    if argumentos is None:
        return MENSAGEM_ARGUMENTOS_NAO_OBJETO
    try:
        return ArgsFinalizar.model_validate(argumentos)
    except ValidationError:
        return MENSAGEM_FINALIZAR_INVALIDO


def _tamanho_do_dado(dados: Any) -> int:
    """Tamanho, em caracteres, do JSON do resultado (como no REPL). Zero quando não há dado."""
    if dados is None:
        return 0
    try:
        return len(json.dumps(dados, default=repr))
    except Exception:
        return 0
