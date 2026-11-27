"""Ferramenta `entrega.publicar`: publica arquivos validados de `/saida` para um departamento."""

import hashlib
import io
import json
import posixpath
import re
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from harness.host.perfil import Perfil
from harness.host.registry import ErroFerramenta, Nivel, Registro

DEPARTAMENTOS: Final = ("vendas", "comercial", "marketing", "financeiro", "direcao")
EXTENSOES: Final = frozenset({".xlsx", ".docx", ".pptx", ".pdf", ".csv", ".txt", ".png"})
LIMITE_BYTES: Final = 5 * 1024 * 1024

_PREFIXO_SAIDA: Final = "/saida/"
_NOME_VALIDO: Final = re.compile(r"[A-Za-z0-9._-]+")
_EXTENSOES_OFFICE: Final = frozenset({".xlsx", ".docx", ".pptx"})


class PublicarArgs(BaseModel):
    """Argumentos de `entrega.publicar`."""

    model_config = ConfigDict(extra="forbid")

    caminho: str
    destinatario: str


def _agora_utc() -> datetime:
    return datetime.now(UTC)


def _validar_caminho(caminho: str) -> tuple[str, str]:
    """Valida `/saida/<nome>` normalizado, sem subpastas, e devolve `(nome, extensao)`."""
    if not caminho.startswith(_PREFIXO_SAIDA) or posixpath.normpath(caminho) != caminho:
        raise ErroFerramenta("caminho de saída inválido")
    nome = caminho[len(_PREFIXO_SAIDA) :]
    if not nome or "/" in nome or _NOME_VALIDO.fullmatch(nome) is None:
        raise ErroFerramenta("nome de arquivo inválido")
    extensao = posixpath.splitext(nome)[1].lower()
    return nome, extensao


def _validar_office(conteudo: bytes) -> None:
    """Garante que arquivos Office são ZIPs válidos e sem macros (`vbaProject.bin`)."""
    buf = io.BytesIO(conteudo)
    if not zipfile.is_zipfile(buf):
        raise ErroFerramenta("o arquivo Office não é um ZIP válido")
    buf.seek(0)
    try:
        with zipfile.ZipFile(buf) as arquivo_zip:
            for membro in arquivo_zip.namelist():
                if membro.lower().endswith("vbaproject.bin"):
                    raise ErroFerramenta("o arquivo Office contém macros (vbaProject.bin)")
    except zipfile.BadZipFile as erro:
        raise ErroFerramenta("o arquivo Office não é um ZIP válido") from erro


def registrar(
    registro: Registro,
    ler_saida: Callable[[str, int], bytes],
    pasta_entregas: Path,
    agora: Callable[[], datetime] = _agora_utc,
) -> None:
    """Registra `entrega.publicar` no registro, lendo `/saida` via `ler_saida`."""
    # Guarda os bytes validados por `montar_pedido` para que `executar` grave exatamente o que
    # foi inspecionado e aprovado, sem reler `/saida`.
    pendentes: dict[str, bytes] = {}

    def montar_pedido(args: PublicarArgs, _perfil: Perfil) -> tuple[tuple[str, str], ...]:
        nome, extensao = _validar_caminho(args.caminho)
        if args.destinatario not in DEPARTAMENTOS:
            raise ErroFerramenta("destinatário desconhecido")
        if extensao not in EXTENSOES:
            raise ErroFerramenta("extensão de arquivo não permitida")

        try:
            conteudo = ler_saida(nome, LIMITE_BYTES)
        except Exception as erro:
            raise ErroFerramenta("não foi possível ler o arquivo de saída") from erro

        if not conteudo:
            raise ErroFerramenta("o arquivo de saída está vazio")
        if len(conteudo) > LIMITE_BYTES:
            raise ErroFerramenta("o arquivo de saída excede o limite de tamanho")
        if extensao in _EXTENSOES_OFFICE:
            _validar_office(conteudo)

        pendentes[args.caminho] = conteudo
        sha256_hex = hashlib.sha256(conteudo).hexdigest()
        tamanho_kib = (len(conteudo) + 1023) // 1024
        return (
            ("Arquivo", nome),
            ("Tipo", extensao),
            ("Tamanho", f"{tamanho_kib} KiB"),
            ("Hash", sha256_hex[:16]),
            ("Destinatário", args.destinatario),
        )

    @registro.ferramenta(
        nome="entrega.publicar",
        nivel=Nivel.ESCRITA_EXTERNA,
        descricao="Publica um arquivo de /saida para um departamento destinatário.",
        args=PublicarArgs,
        dado_privado=False,
        conteudo_nao_confiavel=False,
        canal_externo=True,
        exemplo='tools.entrega.publicar(caminho="/saida/relatorio.csv", destinatario="vendas")',
        montar_pedido=montar_pedido,
    )
    def executar(args: PublicarArgs, perfil: Perfil) -> dict[str, Any]:
        conteudo = pendentes.pop(args.caminho, None)
        if conteudo is None:
            raise ErroFerramenta("o arquivo não foi validado")

        nome = args.caminho[len(_PREFIXO_SAIDA) :]
        instante = agora()
        nome_gravado = f"{instante.strftime('%Y%m%d-%H%M%S')}-{nome}"
        destino_dir = pasta_entregas / args.destinatario
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino_arquivo = destino_dir / nome_gravado

        try:
            with destino_arquivo.open("xb") as arquivo:
                arquivo.write(conteudo)
        except FileExistsError as erro:
            raise ErroFerramenta("já existe uma entrega com esse nome neste segundo") from erro

        arquivo_relativo = f"{args.destinatario}/{nome_gravado}"
        sha256_completo = hashlib.sha256(conteudo).hexdigest()
        registro_item = {
            "data": instante.isoformat(timespec="seconds"),
            "arquivo": arquivo_relativo,
            "destinatario": args.destinatario,
            "sha256": sha256_completo,
            "perfil": perfil.nome,
        }
        with (pasta_entregas / "registro.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(registro_item, ensure_ascii=False) + "\n")

        return {
            "entregue": True,
            "arquivo": arquivo_relativo,
            "sha256": sha256_completo,
        }
