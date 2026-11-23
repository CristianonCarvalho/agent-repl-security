"""Cliente do OpenRouter que implementa o `ModeloLLM` (spec v0, "Modelo e provedor").

O SDK `openai` aponta para o OpenRouter; só a `base_url` muda. O loop não sabe disso: fala
só com `responder`.

Decisões deste módulo (não estão na spec):
- A repetição fica aqui, não no SDK (`max_retries=0`): o número de tentativas e as esperas
  ficam num lugar só, e os testes medem a política sem rede.
- Esperas exponenciais: 1 s, 2 s, 4 s... sem jitter aleatório, para os testes serem
  determinísticos.
- Erros transitórios: limite de taxa (429), timeout, falha de conexão e status >= 500.
  Autenticação e pedido inválido (4xx) falham na hora, sem repetir.
- Ao esgotar, levanta `ErroLLM` com o tipo do erro e o status. Não inclui o corpo da
  resposta, os headers nem a chave: o corpo pode ecoar credenciais, e a mensagem vai para a
  trace. Pelo mesmo motivo a exceção original não é encadeada (`from None`).
- `modelo` e `provedor` efetivos vêm da própria resposta (`model` e o campo `provider` do
  corpo, que o OpenRouter acrescenta). Se faltarem, ficam None, sem inventar valor.
- `custo` vem de `usage.cost`, quando o OpenRouter envia; senão None.
- A chave não fica em atributo do cliente: ela só serve para criar o SDK.
- Prazo total por chamada a `responder` (`prazo_total_s`, padrão 120 s), com todas as
  tentativas e esperas. O `timeout` do SDK vale por operação de socket e não basta: um
  provedor que mantém a conexão aberta sem responder prenderia o loop. Por isso cada tentativa
  roda numa thread daemon própria, e o chamador espera com `Event.wait` só o restante do prazo.
  O `timeout` passado ao SDK é min(`timeout_s`, restante).
- Se a tentativa não termina até o fim do prazo, `responder` levanta `ErroLLM` de prazo e não
  repete. A thread abandonada não altera estado do cliente, e o resultado tardio é descartado.
  Limitação: a requisição HTTP pode continuar em segundo plano até o SDK desistir; o pedido
  é que não espera por ela.
- Erro transitório com prazo restante: a repetição segue as esperas de 1, 2, 4 s. Se a espera
  já não cabe no prazo restante, falha logo com o ErroLLM de prazo, sem dormir.
- Limite de taxa (429) tem política própria. Com `Retry-After` (segundos inteiros ou decimais,
  lidos de `erro.response.headers`), espera o valor, limitado a 30 s. Valor inválido, negativo
  ou ausente cai nas esperas progressivas de 2, 5, 10 e 20 s. Até 5 tentativas no total, fixas:
  `max_tentativas` não limita o 429, só os demais erros transitórios e as respostas vazias.
  A mesma regra de prazo vale: espera que não cabe no restante falha logo, sem dormir.
- Provedor fixo (`provedor`): o corpo leva `order` com o nome, `allow_fallbacks` falso e
  `require_parameters` verdadeiro, e o fallback fica desligado mesmo com `OPENROUTER_FALLBACK`.
  Sem provedor, o roteamento é o de antes. Se o provedor que respondeu (`provedor` da resposta)
  for diferente do pedido, a diferença vai para o log (aviso); nunca levanta exceção, porque o
  OpenRouter pode normalizar o nome.
- Resposta vazia (conteúdo ausente, vazio, só espaços ou só caracteres invisíveis) é falha
  transitória: repete com as mesmas esperas e dentro do mesmo prazo. Ao esgotar as tentativas
  ou o prazo com a resposta ainda vazia, NÃO levanta erro: devolve a última resposta vazia, com
  texto "" e `tentativas_descartadas` contando as vazias repetidas antes dela. Quem consome
  (orquestrador) a trata como turno inválido, como antes. Erros transitórios e de prazo de
  tentativa continuam levantando `ErroLLM`, mesmo depois de uma resposta vazia.
- Tokens de entrada, de saída, de raciocínio e custo das tentativas vazias descartadas são
  somados na resposta final. O custo fica None se nenhuma tentativa informou custo.
- `finish_reason` vira `motivo_fim` (truncado em 64 caracteres). `finish_reason == "length"`
  com texto não vazio é resposta normal: não repete.
- `tokens_raciocinio` vem de `usage.completion_tokens_details.reasoning_tokens`. Ausente, não
  inteiro, negativo ou booleano vira None. O texto do raciocínio nunca é lido.
- Campos lidos por `_campo` aceitam objeto do SDK e dict (o SDK pode devolver qualquer um).
"""

import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any, Final, Literal

import openai

from harness.llm.base import ChamadaFerramenta, ErroLLM, Mensagem, RespostaLLM
from harness.llm.config import ConfigModelo
from harness.llm.ferramentas import EsquemaFerramenta

_TAMANHO_MAXIMO_MOTIVO: Final = 64
_ESPERAS_429: Final = (2.0, 5.0, 10.0, 20.0)
_MAX_TENTATIVAS_429: Final = 5
_TETO_RETRY_AFTER_S: Final = 30.0
_SEGUNDOS_RETRY_AFTER: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_LOG: Final = logging.getLogger(__name__)


class _Tentativa:
    """Resultado de uma tentativa. A thread da tentativa preenche e sinaliza `pronta`.

    Só o chamador lê os campos, e só depois de `pronta` estar ligado. Uma tentativa abandonada
    pelo prazo escreve aqui sem que ninguém leia: o resultado tardio é descartado.
    """

    def __init__(self) -> None:
        self.pronta = threading.Event()
        self.resposta: Any = None
        self.erro: BaseException | None = None


class ClienteOpenRouter:
    """Implementa `ModeloLLM` falando com o OpenRouter pelo SDK `openai`.

    `cliente` é injetável: qualquer objeto com `.chat.completions.create(...)` no formato do
    SDK. Sem ele, cria-se `openai.OpenAI` com a chave e a configuração. O cliente injetado é
    chamado de uma thread de tentativa, não da thread que chama `responder`.
    """

    def __init__(
        self,
        config: ConfigModelo,
        api_key: str,
        cliente: Any | None = None,
        dormir: Callable[[float], None] = time.sleep,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._cliente: Any = cliente if cliente is not None else _criar_sdk(config, api_key)
        self._dormir = dormir
        self._relogio = relogio

    def responder(self, mensagens: Sequence[Mensagem]) -> RespostaLLM:
        """Envia o histórico ao modelo e devolve a resposta. Levanta `ErroLLM` se falhar.

        O prazo `prazo_total_s` cobre todas as tentativas e esperas desta chamada.
        """
        return self._responder_com_repeticao(self._parametros(mensagens), com_ferramentas=False)

    def responder_com_ferramentas(
        self,
        mensagens: Sequence[Mensagem],
        ferramentas: Sequence[EsquemaFerramenta],
        *,
        escolha: Literal["auto", "none"] = "auto",
    ) -> RespostaLLM:
        """Como `responder`, mas envia as ferramentas (`tools`) e a escolha (`tool_choice`).

        Vale o mesmo prazo, as mesmas repetições e o mesmo provedor fixo. Uma resposta com chamadas
        de ferramenta NÃO é vazia, mesmo com texto vazio. Sem ferramentas, o pedido não leva
        `tools`, porque a API recusa a lista vazia.
        """
        parametros = self._parametros(mensagens, ferramentas=ferramentas, escolha=escolha)
        return self._responder_com_repeticao(parametros, com_ferramentas=True)

    def _responder_com_repeticao(
        self, parametros: dict[str, Any], *, com_ferramentas: bool
    ) -> RespostaLLM:
        inicio = self._relogio()
        tentativa_numero = 0
        descartadas: list[RespostaLLM] = []
        while True:
            tentativa_numero += 1
            if self._restante(inicio) <= 0:
                raise self._erro_de_prazo()
            tentativa = self._tentar(parametros, self._restante(inicio))
            if tentativa is None:
                raise self._erro_de_prazo()
            if tentativa.erro is None:
                resposta = _converter(tentativa.resposta)
                if not _sem_conteudo(resposta, com_ferramentas=com_ferramentas):
                    return self._conferido(_somar(resposta, descartadas))
                # Resposta vazia: repete como falha transitória, mas, se não couber, devolve a
                # última vazia (o orquestrador a trata como turno inválido).
                espera = float(2 ** (tentativa_numero - 1))
                esgotou = tentativa_numero >= self._config.max_tentativas
                if esgotou or espera >= self._restante(inicio):
                    return self._conferido(_somar(resposta, descartadas))
                descartadas.append(resposta)
                self._dormir(espera)
                continue
            erro = tentativa.erro
            if not isinstance(erro, Exception):
                # Interrupções (BaseException) não são classificadas: seguem como estavam.
                raise erro
            limite = _limite_de_tentativas(erro, self._config.max_tentativas)
            if not (_transitorio(erro) and tentativa_numero < limite):
                raise ErroLLM(_mensagem_de_erro(erro, tentativa_numero)) from None
            espera = _espera_de_repeticao(erro, tentativa_numero)
            if espera >= self._restante(inicio):
                raise self._erro_de_prazo()
            self._dormir(espera)

    def _tentar(self, parametros: dict[str, Any], restante: float) -> _Tentativa | None:
        """Roda uma tentativa numa thread daemon. Devolve None se ela não termina no prazo."""
        tentativa = _Tentativa()
        pedido = {
            **parametros,
            "timeout": min(self._config.timeout_s, restante),
        }
        threading.Thread(
            target=self._executar,
            args=(pedido, tentativa),
            name="openrouter-tentativa",
            daemon=True,
        ).start()
        if not tentativa.pronta.wait(restante):
            return None
        return tentativa

    def _executar(self, pedido: dict[str, Any], tentativa: _Tentativa) -> None:
        """Corpo da thread: captura tudo, porque nada pode vazar para fora dela."""
        try:
            tentativa.resposta = self._cliente.chat.completions.create(**pedido)
        except BaseException as erro:
            tentativa.erro = erro
        finally:
            tentativa.pronta.set()

    def _restante(self, inicio: float) -> float:
        """Segundos que ainda restam do prazo total desta chamada."""
        return self._config.prazo_total_s - (self._relogio() - inicio)

    def _conferido(self, resposta: RespostaLLM) -> RespostaLLM:
        """Registra, sem levantar exceção, se o provedor que respondeu não é o pedido."""
        solicitado = self._config.provedor
        efetivo = resposta.provedor
        if solicitado is not None and efetivo is not None:
            if efetivo.casefold() != solicitado.casefold():
                _LOG.warning(
                    "provedor que respondeu (%s) difere do solicitado (%s)", efetivo, solicitado
                )
        return resposta

    def _erro_de_prazo(self) -> ErroLLM:
        # Genérico: sem corpo de resposta, headers ou chave.
        return ErroLLM(
            f"a chamada ao modelo passou do prazo total de {self._config.prazo_total_s:g} s"
        )

    def _parametros(
        self,
        mensagens: Sequence[Mensagem],
        *,
        ferramentas: Sequence[EsquemaFerramenta] = (),
        escolha: Literal["auto", "none"] = "auto",
    ) -> dict[str, Any]:
        config = self._config
        parametros: dict[str, Any] = {
            "model": config.slug,
            "messages": [_mensagem_para_api(m) for m in mensagens],
            "temperature": config.temperatura,
            "extra_body": {"provider": _roteamento(config)},
        }
        if config.seed is not None:
            parametros["seed"] = config.seed
        if config.max_tokens_saida is not None:
            parametros["max_tokens"] = config.max_tokens_saida
        if ferramentas:
            parametros["tools"] = [_ferramenta_para_api(e) for e in ferramentas]
            parametros["tool_choice"] = escolha
        return parametros


def _mensagem_para_api(mensagem: Mensagem) -> dict[str, Any]:
    """Formato da API para uma mensagem. Mensagens sem ferramenta seguem o formato de sempre."""
    if mensagem.role == "tool":
        return {"role": "tool", "tool_call_id": mensagem.tool_call_id, "content": mensagem.content}
    if mensagem.chamadas_ferramenta:
        return {
            "role": "assistant",
            "content": mensagem.content or None,
            "tool_calls": [
                {
                    "id": chamada.id,
                    "type": "function",
                    "function": {"name": chamada.nome, "arguments": chamada.argumentos_brutos},
                }
                for chamada in mensagem.chamadas_ferramenta
            ],
        }
    return {"role": mensagem.role, "content": mensagem.content}


def _ferramenta_para_api(esquema: EsquemaFerramenta) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": esquema.nome,
            "description": esquema.descricao,
            "parameters": esquema.parametros,
        },
    }


def _criar_sdk(config: ConfigModelo, api_key: str) -> Any:
    if not api_key:
        raise ValueError("a chave da API do OpenRouter está vazia")
    return openai.OpenAI(
        api_key=api_key,
        base_url=config.base_url,
        timeout=config.timeout_s,
        max_retries=0,
    )


def _roteamento(config: ConfigModelo) -> dict[str, object]:
    """Preferências de provedor. Com fallback desligado, o eval sabe qual provedor respondeu."""
    if config.provedor is not None:
        return {
            "order": [config.provedor],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    if config.fallback_provedor:
        return {"require_parameters": True}
    return {"require_parameters": True, "allow_fallbacks": False}


def _transitorio(erro: Exception) -> bool:
    if isinstance(erro, openai.RateLimitError | openai.APIConnectionError):
        return True
    return isinstance(erro, openai.APIStatusError) and erro.status_code >= 500


def _limite_de_tentativas(erro: Exception, max_tentativas: int) -> int:
    """Total de tentativas para este erro: 5 no limite de taxa, `max_tentativas` nos demais."""
    if isinstance(erro, openai.RateLimitError):
        return _MAX_TENTATIVAS_429
    return max_tentativas


def _espera_de_repeticao(erro: Exception, tentativa_numero: int) -> float:
    """Segundos antes da próxima tentativa. Só o 429 tem regra própria (Retry-After ou escada)."""
    if isinstance(erro, openai.RateLimitError):
        informada = _retry_after(erro)
        if informada is not None:
            return min(informada, _TETO_RETRY_AFTER_S)
        indice = min(tentativa_numero - 1, len(_ESPERAS_429) - 1)
        return _ESPERAS_429[indice]
    return float(2 ** (tentativa_numero - 1))


def _retry_after(erro: Exception) -> float | None:
    """Valor do `Retry-After` em segundos, ou None se ausente ou inválido. Nunca levanta."""
    resposta = getattr(erro, "response", None)
    cabecalhos = getattr(resposta, "headers", None)
    if cabecalhos is None:
        return None
    try:
        bruto: object = cabecalhos.get("retry-after")
    except Exception:
        return None
    if not isinstance(bruto, str):
        return None
    texto = bruto.strip()
    if _SEGUNDOS_RETRY_AFTER.fullmatch(texto) is None:
        return None
    return float(texto)


def _mensagem_de_erro(erro: Exception, tentativas: int) -> str:
    status = erro.status_code if isinstance(erro, openai.APIStatusError) else None
    detalhe = f"status {status}" if status is not None else "sem status HTTP"
    plural = "tentativa" if tentativas == 1 else "tentativas"
    return (
        f"falha ao consultar o OpenRouter ({type(erro).__name__}, {detalhe}) "
        f"após {tentativas} {plural}"
    )


def _converter(resposta: Any) -> RespostaLLM:
    """Converte o objeto do SDK em `RespostaLLM`. Resposta sem escolhas é erro do modelo.

    Texto vazio (ou só invisível) vira "", para que a repetição o reconheça.
    """
    escolhas = _campo(resposta, "choices")
    if not escolhas:
        raise ErroLLM("resposta do OpenRouter sem escolhas")
    escolha = escolhas[0]
    mensagem = _campo(escolha, "message")
    conteudo = _campo(mensagem, "content")
    usage = _campo(resposta, "usage")
    modelo = _campo(resposta, "model")
    provedor = _campo(resposta, "provider")
    custo = _campo(usage, "cost")
    raciocinio = _campo(_campo(usage, "completion_tokens_details"), "reasoning_tokens")
    return RespostaLLM(
        texto=conteudo if isinstance(conteudo, str) and not _vazio(conteudo) else "",
        tokens_entrada=_contagem(_campo(usage, "prompt_tokens")),
        tokens_saida=_contagem(_campo(usage, "completion_tokens")),
        modelo=modelo if isinstance(modelo, str) else None,
        provedor=provedor if isinstance(provedor, str) else None,
        custo=float(custo) if _eh_numero(custo) else None,
        motivo_fim=_motivo(_campo(escolha, "finish_reason")),
        tokens_raciocinio=_contagem_opcional(raciocinio),
        chamadas_ferramenta=_chamadas(mensagem),
    )


def _chamadas(mensagem: Any) -> tuple[ChamadaFerramenta, ...]:
    """Chamadas de ferramenta da mensagem, na ordem. Campo ausente ou inválido vira tupla vazia.

    Id ausente ganha um id sintético (`chamada_N`), para que a resposta à chamada tenha onde se
    prender. Nome ausente vira texto vazio, e o laço o trata como função desconhecida. Os
    argumentos ficam como texto cru: validar é papel do laço, e nada aqui levanta exceção.
    """
    brutas = _campo(mensagem, "tool_calls")
    if not isinstance(brutas, list | tuple):
        return ()
    chamadas: list[ChamadaFerramenta] = []
    for indice, bruta in enumerate(brutas, start=1):
        identificador = _campo(bruta, "id")
        funcao = _campo(bruta, "function")
        nome = _campo(funcao, "name")
        chamadas.append(
            ChamadaFerramenta(
                id=identificador
                if isinstance(identificador, str) and identificador
                else f"chamada_{indice}",
                nome=nome if isinstance(nome, str) else "",
                argumentos_brutos=_argumentos_em_texto(_campo(funcao, "arguments")),
            )
        )
    return tuple(chamadas)


def _argumentos_em_texto(valor: Any) -> str:
    """Argumentos como texto. Texto passa como veio; objeto vira JSON; o resto vira vazio."""
    if isinstance(valor, str):
        return valor
    if isinstance(valor, dict):
        try:
            return json.dumps(valor, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return ""


def _sem_conteudo(resposta: RespostaLLM, *, com_ferramentas: bool) -> bool:
    """Resposta vazia (repetida como falha transitória). Com ferramentas, chamada é conteúdo.

    Sem ferramentas, a regra é a de antes, só pelo texto.
    """
    if com_ferramentas and resposta.chamadas_ferramenta:
        return False
    return _vazio(resposta.texto)


def _somar(final: RespostaLLM, descartadas: Sequence[RespostaLLM]) -> RespostaLLM:
    """Soma tokens, raciocínio e custo das tentativas vazias descartadas à resposta final."""
    if not descartadas:
        return final
    todas = [*descartadas, final]
    custos = [r.custo for r in todas if r.custo is not None]
    raciocinios = [r.tokens_raciocinio for r in todas if r.tokens_raciocinio is not None]
    return final.model_copy(
        update={
            "tokens_entrada": sum(r.tokens_entrada for r in todas),
            "tokens_saida": sum(r.tokens_saida for r in todas),
            "custo": float(sum(custos)) if custos else None,
            "tokens_raciocinio": sum(raciocinios) if raciocinios else None,
            "tentativas_descartadas": len(descartadas),
        }
    )


def _vazio(texto: str) -> bool:
    """Sem nenhum caractere visível: vazio, só espaços ou só caracteres de controle/formato."""
    return all(c.isspace() or unicodedata.category(c) in ("Cc", "Cf") for c in texto)


def _motivo(valor: Any) -> str | None:
    """`finish_reason` curto. Não-texto ou vazio vira None."""
    if not isinstance(valor, str):
        return None
    return valor.strip()[:_TAMANHO_MAXIMO_MOTIVO] or None


def _campo(objeto: Any, nome: str) -> Any:
    """Lê um campo do objeto do SDK (ou de um dict), inclusive os extras do OpenRouter."""
    if objeto is None:
        return None
    if isinstance(objeto, dict):
        return objeto.get(nome)
    valor = getattr(objeto, nome, None)
    if valor is None:
        extras = getattr(objeto, "model_extra", None)
        if isinstance(extras, dict):
            valor = extras.get(nome)
    return valor


def _contagem(valor: Any) -> int:
    """Contagem de tokens. Ausente ou inválida vira zero."""
    return _contagem_opcional(valor) or 0


def _contagem_opcional(valor: Any) -> int | None:
    """Contagem de tokens inteira e não negativa. Ausente ou inválida vira None."""
    if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
        return valor
    return None


def _eh_numero(valor: Any) -> bool:
    return isinstance(valor, int | float) and not isinstance(valor, bool)
