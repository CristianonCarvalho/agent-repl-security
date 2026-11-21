"""Testes do adaptador MCP → Registro, contra o servidor falso (sem rede, sem Docker)."""

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from harness.host.mcp.adaptador import (
    LIMITE_DESCRICAO,
    converter_resultado,
    criar_modelo_argumentos,
    registrar_servidores_mcp,
)
from harness.host.mcp.cliente import ClienteMcp
from harness.host.mcp.config import ConfigMcp, ErroConfigMcp
from harness.host.perfil import Perfil, perfil_por_nome
from harness.host.registry import ErroFerramenta, Nivel, Registro
from harness.host.tools import registrar_todas
from harness.sandbox.protocol import ToolCall, ToolResult
from tests.host.mcp.auxiliares import SERVIDOR_FALSO, ler_pid, processo_morto
from tests.host.mcp.servidor_falso import ESQUEMA_VARIADO


class _Vazio(BaseModel):
    """Modelo sem campos, para ferramentas de teste do próprio Registro."""


FERRAMENTAS_VALIDAS = {
    "falso.eco",
    "falso.ambiente",
    "falso.erro",
    "falso.estruturado",
    "falso.imagem",
    "falso.forjada",
    "falso.esquema",
    "falso.sem_esquema",
}


def _servidor(modo: str, pid: Path | None = None, **extra: Any) -> dict[str, Any]:
    args = [str(SERVIDOR_FALSO), modo]
    if pid is not None:
        args.append(str(pid))
    # Tudo que os testes chamam pelo despachante é de leitura; `ambiente` fica de escrita externa.
    leitura = ["eco", "erro", "estruturado", "imagem", "esquema", "forjada", "sem_esquema"]
    return {"comando": sys.executable, "args": args, "leitura": leitura, **extra}


def _config(**servidores: dict[str, Any]) -> ConfigMcp:
    return ConfigMcp.model_validate({"servidores": servidores})


def _despachar(registro: Registro, perfil: Perfil, nome: str, **args: Any) -> ToolResult:
    return registro.despachar(ToolCall(call_id="c1", name=nome, args=args), perfil)


@pytest.fixture
def perfil() -> Perfil:
    # Vendas com as ferramentas do servidor falso concedidas (D14): o teste chama todas elas.
    return perfil_por_nome("Vendas").com_concessoes(FERRAMENTAS_VALIDAS)


@pytest.fixture
def espiao_chamadas(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Registra o nome de cada chamada que chega ao cliente MCP (sem mudar o comportamento)."""
    chamadas: list[str] = []
    original = ClienteMcp.chamar

    def chamar(self: ClienteMcp, nome: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        chamadas.append(nome)
        return original(self, nome, argumentos)

    monkeypatch.setattr(ClienteMcp, "chamar", chamar)
    return chamadas


# --- Registro -----------------------------------------------------------------------------


def test_ferramentas_entram_como_servidor_ponto_ferramenta() -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))) as gerente:
        nomes = {ferramenta.nome for ferramenta in registro.listar()}
        assert FERRAMENTAS_VALIDAS <= nomes
        assert set(gerente.ferramentas) == nomes


def test_nomes_fora_do_padrao_sao_ignorados_e_contados() -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))) as gerente:
        assert "falso.eco2" not in {f.nome for f in registro.listar()}
        assert {"falso.eco2", "falso.Nome-Ruim", "falso._oculta"} <= set(gerente.ignoradas)


def test_nivel_padrao_e_escrita_externa_e_leitura_so_para_as_listadas() -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        eco = registro.obter("falso.eco")
        ambiente = registro.obter("falso.ambiente")
        assert eco is not None and ambiente is not None
        assert eco.nivel is Nivel.LEITURA
        assert ambiente.nivel is Nivel.ESCRITA_EXTERNA


def test_todas_as_ferramentas_mcp_sao_nao_confiaveis_e_canal_externo() -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        for nome in FERRAMENTAS_VALIDAS:
            ferramenta = registro.obter(nome)
            assert ferramenta is not None, nome
            assert ferramenta.conteudo_nao_confiavel is True, nome
            assert ferramenta.canal_externo is True, nome
            assert ferramenta.dado_privado is False, nome


def test_descricao_forjada_e_neutralizada_e_limitada() -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        ferramenta = registro.obter("falso.forjada")
        assert ferramenta is not None
        assert "</saida_nao_confiavel>" not in ferramenta.descricao
        assert "&lt;/saida_nao_confiavel>" in ferramenta.descricao
        assert len(ferramenta.descricao) <= LIMITE_DESCRICAO
        assert ferramenta.descricao.endswith("…")


def test_descricao_vazia_ganha_texto_padrao_com_o_servidor() -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        ferramenta = registro.obter("falso.ambiente")
        assert ferramenta is not None
        assert ferramenta.descricao == "Lista os nomes das variáveis de ambiente."


# --- Argumentos ---------------------------------------------------------------------------


def test_argumentos_obrigatorios_tipos_e_extra_recusados_sem_chamar_o_servidor(
    perfil: Perfil, espiao_chamadas: list[str]
) -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        casos: list[tuple[dict[str, Any], str]] = [
            ({"class": "x"}, "argumento obrigatório ausente: 'texto'"),
            ({"texto": 5, "class": "x"}, "valor inválido para 'texto'"),
            ({"texto": "a", "class": "x", "zz": 1}, "argumento não aceito: 'zz'"),
        ]
        for args, esperado in casos:
            resultado = _despachar(registro, perfil, "falso.esquema", **args)
            assert resultado.ok is False
            assert resultado.error is not None
            assert esperado in resultado.error
    assert espiao_chamadas == []


def test_so_os_argumentos_recebidos_seguem_com_o_nome_original(
    perfil: Perfil,
) -> None:
    registro = Registro()
    args = {"texto": "a", "class": "x", "campo 1": "z", "_privado": "p", "livre": [1]}
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "falso.esquema", **args)
    # `numero` tem padrão e não foi enviado, então o servidor não o recebe.
    assert resultado.ok is True
    assert resultado.data == {"recebido": args}


def test_modelo_gerado_tem_tipos_obrigatorios_e_padroes_do_schema() -> None:
    modelo = criar_modelo_argumentos(ESQUEMA_VARIADO)
    campos = modelo.model_fields
    assert campos["texto"].is_required()
    assert campos["texto"].alias == "texto"
    assert campos["numero"].default == 5
    assert campos["ativo"].default is False
    # Nome que não serve como atributo de modelo recebe um nome interno e o alias original.
    assert campos["campo_7"].alias == "class"
    assert campos["campo_8"].alias == "_privado"


def test_schema_ausente_ou_invalido_nao_aceita_argumento_nenhum() -> None:
    for esquema in (None, {"type": "string"}, {"properties": "nao_e_objeto"}):
        modelo = criar_modelo_argumentos(esquema)
        assert modelo.model_fields == {}
        with pytest.raises(ValidationError):
            modelo.model_validate({"x": 1})


# --- Resultados ---------------------------------------------------------------------------


def test_texto_dos_blocos_volta_como_string(perfil: Perfil) -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "falso.eco", texto="oi")
    assert resultado == ToolResult(call_id="c1", ok=True, data='{"texto": "oi"}')


def test_is_error_vira_erro_legivel_da_ferramenta(perfil: Perfil) -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "falso.erro")
    assert resultado.ok is False
    assert resultado.error == "falhou de propósito"


def test_structured_content_devolve_o_objeto(perfil: Perfil) -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "falso.estruturado")
    assert resultado.ok is True
    assert resultado.data == {"total": 3, "itens": ["a", "b", "c"]}


def test_bloco_de_outro_tipo_vira_marcador(perfil: Perfil) -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "falso.imagem")
    assert resultado.data == "[conteúdo do tipo image omitido]\ndepois"


def test_is_error_sem_texto_usa_mensagem_padrao_e_limita_o_tamanho() -> None:
    with pytest.raises(ErroFerramenta, match="reportou erro") as vazio:
        converter_resultado({"isError": True, "content": []}, "falso", "eco")
    assert "falso.eco" in str(vazio.value)

    grande = {"isError": True, "content": [{"type": "text", "text": "x" * 800}]}
    with pytest.raises(ErroFerramenta) as erro:
        converter_resultado(grande, "falso", "eco")
    assert len(str(erro.value)) <= 500
    assert str(erro.value).endswith("…")


def test_timeout_no_servidor_vira_erro_generico_no_registro(perfil: Perfil, tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    registro = Registro()
    config = _config(falso=_servidor("trava", pid, timeout_chamada_s=0.5))
    with registrar_servidores_mcp(registro, config) as gerente:
        resultado = _despachar(registro, perfil, "falso.eco", texto="oi")
        assert resultado.ok is False
        assert resultado.error == "o servidor MCP 'falso' não respondeu a tempo"
        assert gerente.ferramentas
    assert processo_morto(ler_pid(pid))


def test_nivel_de_escrita_externa_e_recusado_pelo_despachante(
    perfil: Perfil, espiao_chamadas: list[str]
) -> None:
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "falso.ambiente")
    assert resultado.ok is False
    assert resultado.error is not None
    assert "exige aprovação humana" in resultado.error
    assert espiao_chamadas == []


# --- Falhas de servidor e montagem --------------------------------------------------------


def test_servidor_que_falha_ao_iniciar_e_pulado_sem_derrubar_os_outros(
    perfil: Perfil, tmp_path: Path
) -> None:
    registro = Registro()
    config = _config(
        quebrado=_servidor("falha_inicio"),
        falso=_servidor("normal"),
    )
    with registrar_servidores_mcp(registro, config) as gerente:
        assert "quebrado" in gerente.falhas
        assert "falso.eco" in gerente.ferramentas
        assert _despachar(registro, perfil, "falso.eco", texto="a").ok is True


def test_nome_com_digito_nao_inicia_o_servidor(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    registro = Registro()
    with registrar_servidores_mcp(registro, _config(eco2=_servidor("normal", pid))) as gerente:
        assert "dígitos" in gerente.falhas["eco2"]
        assert gerente.ferramentas == []
    assert not pid.exists()


def test_nome_de_grupo_ja_registrado_recusa_antes_de_subir_qualquer_servidor(
    tmp_path: Path,
) -> None:
    pid = tmp_path / "pid"
    registro = Registro()
    registro.ferramenta(nome="grupo.x", nivel=Nivel.LEITURA, descricao="x", args=_Vazio)(
        lambda args, perfil: None
    )
    with pytest.raises(ErroConfigMcp, match="'grupo'"):
        registrar_servidores_mcp(registro, _config(grupo=_servidor("normal", pid)))
    assert not pid.exists()


def test_saida_do_gerente_fecha_todos_os_processos(tmp_path: Path) -> None:
    pid_a = tmp_path / "pid_a"
    pid_b = tmp_path / "pid_b"
    registro = Registro()
    config = _config(
        alfa=_servidor("normal", pid_a),
        beta=_servidor("normal", pid_b),
    )
    with registrar_servidores_mcp(registro, config):
        assert not processo_morto(ler_pid(pid_a))
        assert not processo_morto(ler_pid(pid_b))
    assert processo_morto(ler_pid(pid_a))
    assert processo_morto(ler_pid(pid_b))


def test_fechar_o_gerente_e_idempotente(tmp_path: Path) -> None:
    pid = tmp_path / "pid"
    registro = Registro()
    gerente = registrar_servidores_mcp(registro, _config(falso=_servidor("normal", pid)))
    gerente.fechar()
    gerente.fechar()
    assert processo_morto(ler_pid(pid))


# --- Descoberta ----------------------------------------------------------------------------


def test_descoberta_enxerga_as_ferramentas_mcp(perfil: Perfil, tmp_path: Path) -> None:
    registro = Registro()
    registrar_todas(registro, tmp_path)
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        busca = _despachar(registro, perfil, "descoberta.buscar", query="ambiente")
        assert busca.ok is True
        assert "falso.ambiente" in [item["nome"] for item in busca.data]


def test_descrever_usa_o_esquema_original_do_servidor(perfil: Perfil, tmp_path: Path) -> None:
    registro = Registro()
    registrar_todas(registro, tmp_path)
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "descoberta.descrever", name="falso.esquema")
    assert resultado.ok is True, resultado.error
    parametros = resultado.data["parametros"]
    # Os nomes são os originais do servidor, não os internos do modelo.
    assert [p["nome"] for p in parametros] == list(ESQUEMA_VARIADO["properties"])
    numero = next(p for p in parametros if p["nome"] == "numero")
    assert numero == {"nome": "numero", "tipo": "integer", "obrigatorio": False, "padrao": 5}
    texto = next(p for p in parametros if p["nome"] == "texto")
    assert texto["obrigatorio"] is True


def test_descrever_ferramenta_sem_esquema_mostra_nenhum_parametro(
    perfil: Perfil, tmp_path: Path
) -> None:
    registro = Registro()
    registrar_todas(registro, tmp_path)
    with registrar_servidores_mcp(registro, _config(falso=_servidor("normal"))):
        resultado = _despachar(registro, perfil, "descoberta.descrever", name="falso.sem_esquema")
    assert resultado.ok is True
    assert resultado.data["parametros"] == []
