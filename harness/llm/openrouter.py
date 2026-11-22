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

import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any, Final

import openai

from harness.llm.base import ErroLLM, Mensagem, RespostaLLM
from harness.llm.config import ConfigModelo

_TAMANHO_MAXIMO_MOTIVO: Final = 64


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
        parametros = self._parametros(mensagens)
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
                if not _vazio(resposta.texto):
                    return _somar(resposta, descartadas)
                # Resposta vazia: repete como falha transitória, mas, se não couber, devolve a
                # última vazia (o orquestrador a trata como turno inválido).
                espera = float(2 ** (tentativa_numero - 1))
                esgotou = tentativa_numero >= self._config.max_tentativas
                if esgotou or espera >= self._restante(inicio):
                    return _somar(resposta, descartadas)
                descartadas.append(resposta)
                self._dormir(espera)
                continue
            erro = tentativa.erro
            if not isinstance(erro, Exception):
                # Interrupções (BaseException) não são classificadas: seguem como estavam.
                raise erro
            if not (_transitorio(erro) and tentativa_numero < self._config.max_tentativas):
                raise ErroLLM(_mensagem_de_erro(erro, tentativa_numero)) from None
            espera = float(2 ** (tentativa_numero - 1))
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

    def _erro_de_prazo(self) -> ErroLLM:
        # Genérico: sem corpo de resposta, headers ou chave.
        return ErroLLM(
            f"a chamada ao modelo passou do prazo total de {self._config.prazo_total_s:g} s"
        )

    def _parametros(self, mensagens: Sequence[Mensagem]) -> dict[str, Any]:
        config = self._config
        parametros: dict[str, Any] = {
            "model": config.slug,
            "messages": [{"role": m.role, "content": m.content} for m in mensagens],
            "temperature": config.temperatura,
            "extra_body": {"provider": _roteamento(config)},
        }
        if config.seed is not None:
            parametros["seed"] = config.seed
        if config.max_tokens_saida is not None:
            parametros["max_tokens"] = config.max_tokens_saida
        return parametros


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
    if config.fallback_provedor:
        return {"require_parameters": True}
    return {"require_parameters": True, "allow_fallbacks": False}


def _transitorio(erro: Exception) -> bool:
    if isinstance(erro, openai.RateLimitError | openai.APIConnectionError):
        return True
    return isinstance(erro, openai.APIStatusError) and erro.status_code >= 500


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
    conteudo = _campo(_campo(escolha, "message"), "content")
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
    )


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
