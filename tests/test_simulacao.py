from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from src.calendario import Calendario, interpretar_dias
from src.data_loader import ErroPlanilha, carregar, extrair_dash
from src.heuristics import busca_local, montar_plano, ordem_edd
from src.metrics import kpis
from src.simulation import Plano, simular

TEMPLATE = Path(__file__).resolve().parents[1] / "data" / "template_fabrica.xlsx"
INICIO = pd.Timestamp("2026-11-02 05:20")  # segunda-feira


def planilha_custom(tmp_path, **alteracoes) -> Path:
    """Copia o template trocando abas inteiras (DataFrames) ou aplicando funções nelas."""
    abas = pd.read_excel(TEMPLATE, sheet_name=None)
    for nome, valor in alteracoes.items():
        abas[nome] = valor(abas[nome]) if callable(valor) else valor
    arquivo = tmp_path / "custom.xlsx"
    with pd.ExcelWriter(arquivo) as xls:
        for nome, df in abas.items():
            df.to_excel(xls, sheet_name=nome, index=False)
    return arquivo


SEM_TURNOS = pd.DataFrame(columns=["turno", "inicio", "fim", "dias"])


def um_pedido(produto="GH781-8", metros=1500):
    return pd.DataFrame([dict(pedido="P1", produto=produto, metros=metros, data_entrega=pd.Timestamp("2026-11-30"), prioridade=None)])


def eventos(res, maquina, estado):
    ev = res.eventos
    return ev[(ev["maquina"] == maquina) & (ev["estado"] == estado)]


def duracao_min(df):
    return ((df["fim"] - df["inicio"]).dt.total_seconds() / 60).sum()


@pytest.fixture(scope="module")
def dados():
    return carregar(TEMPLATE)


@pytest.fixture(scope="module")
def resultado(dados):
    return simular(dados, montar_plano(dados, ordem_edd(dados)), INICIO)


# ------------------------------------------------------------------ leitura
def test_extrair_dash():
    assert extrair_dash("GH781-8") == 8
    assert extrair_dash("GH781-12") == 12
    assert extrair_dash("SEMDASH") is None


def test_carreteis_arredondam_para_cima(dados):
    for p in dados.pedidos:
        assert p.n_carreteis * p.metros_por_carretel >= p.metros > (p.n_carreteis - 1) * p.metros_por_carretel


def test_passagens_dependem_dos_decks(dados):
    assert dados.passagens("GH781-8-A", "TR-01") == 2  # 2 camadas, 1 deck
    assert dados.passagens("GH781-8-A", "TR-03") == 1  # 2 camadas, 2 decks
    assert dados.passagens("GH781-8-B", "TR-11") == 1  # 2 camadas, 4 decks
    assert dados.decks_em_uso("GH781-8-B", "TR-11") == 2


def test_planilha_invalida_lista_erros(tmp_path):
    arquivo = planilha_custom(tmp_path, pedidos=um_pedido(produto="SEM-CONSTRUCAO-8"))
    with pytest.raises(ErroPlanilha) as exc:
        carregar(arquivo)
    assert any("sem construção" in e for e in exc.value.erros)


# ------------------------------------------------------------------ calendário
def test_interpretar_dias():
    assert interpretar_dias("seg-sab") == {0, 1, 2, 3, 4, 5}
    assert interpretar_dias("Seg-sex") == {0, 1, 2, 3, 4}
    assert interpretar_dias("seg,qua") == {0, 2}


def test_calendario_pula_domingo():
    turnos = pd.DataFrame([dict(turno="T", inicio=time(6, 0), fim=time(14, 0), dias="seg-sab")])
    sabado_13h = pd.Timestamp("2026-11-07 13:00")
    cal = Calendario.de_turnos(turnos, sabado_13h)
    fim = sabado_13h + pd.Timedelta(minutes=cal.fim_trabalho(0, 120))
    assert fim == pd.Timestamp("2026-11-09 07:00")  # 1h no sábado + 1h na segunda


# ------------------------------------------------------------------ invariantes do modelo completo
def test_maquina_nunca_faz_duas_coisas_ao_mesmo_tempo(resultado):
    for maquina, g in resultado.eventos.groupby("maquina"):
        g = g.sort_values("inicio")
        assert (g["inicio"].values[1:] >= g["fim"].values[:-1]).all(), maquina


def test_carretel_cumpre_a_rota_em_ordem(dados, resultado):
    for (_, _), g in resultado.operacoes.groupby(["pedido", "carretel"]):
        g = g.sort_values("inicio")
        c = dados.construcoes[g["construcao"].iloc[0]]
        assert list(g["processo"]) == [e.processo for e in c.etapas]
        assert (g["inicio"].values[1:] >= g["fim"].values[:-1]).all()


def test_autoclave_respeita_capacidade_e_grupo(dados, resultado):
    assert (resultado.lotes["carreteis"] <= dados.autoclave_capacidade).all()
    vulc = resultado.operacoes[resultado.operacoes["lote"].notna()]
    grupos = vulc.groupby("lote")["produto"].apply(lambda s: {dados.autoclave_grupo[p] for p in s})
    assert grupos.map(len).eq(1).all()


def _max_simultaneos(intervalos):
    marcos = sorted([(a, 1) for a, _ in intervalos] + [(b, -1) for _, b in intervalos], key=lambda x: (x[0], x[1]))
    atual = pico = 0
    for _, delta in marcos:
        atual += delta
        pico = max(pico, atual)
    return pico


def test_pulmao_de_tubos_limita_cada_trancadeira(dados, resultado):
    ops = resultado.operacoes
    for tranc, g in resultado.pedidos.groupby("trancadeira"):
        sel = ops[ops["pedido"].isin(g["pedido"])]
        tubo = sel[sel["processo"] == "EXT_TUBO"].set_index(["pedido", "carretel"])["inicio"]
        tranca = sel[sel["processo"] == "TRANCA"].set_index(["pedido", "carretel"])["inicio"]
        intervalos = list(zip(tubo.sort_index(), tranca.sort_index()))
        assert _max_simultaneos(intervalos) <= dados.pulmao_tubos, tranc


def test_mandris_limitam_carreteis_em_processo(tmp_path):
    arquivo = planilha_custom(
        tmp_path, turnos=SEM_TURNOS, mandris=pd.DataFrame([dict(dash=8, quantidade=2)]),
        pedidos=pd.DataFrame([dict(pedido=f"P{i}", produto="GH781-8", metros=1500, data_entrega=pd.Timestamp("2026-11-30"),
                                   prioridade=None) for i in range(3)]),
    )
    d = carregar(arquivo)
    plano = Plano(["P0", "P1", "P2"], {"P0": ("GH781-8-A", "TR-03"), "P1": ("GH781-8-A", "TR-04"),
                                       "P2": ("GH781-8-A", "TR-06")})
    ops = simular(d, plano, INICIO).operacoes
    ini = ops[ops["processo"] == "EXT_TUBO"].set_index(["pedido", "carretel"])["inicio"].sort_index()
    fim = ops[ops["processo"] == "TESTE"].set_index(["pedido", "carretel"])["fim"].sort_index()
    assert _max_simultaneos(list(zip(ini, fim))) <= 2


# ------------------------------------------------------------------ regras da trançadeira
def _cenario_trancadeira(tmp_path, tranc, metros, rendimento):
    arquivo = planilha_custom(
        tmp_path, turnos=SEM_TURNOS, pedidos=um_pedido(metros=metros),
        construcoes=lambda df: df.assign(rendimento_m_por_jogo=df["rendimento_m_por_jogo"].where(
            df["construcao"] != "GH781-8-A", rendimento)),
    )
    d = carregar(arquivo)
    return d, simular(d, Plano(["P1"], {"P1": ("GH781-8-A", tranc)}), INICIO)


def test_duas_passagens_em_maquina_de_um_deck(tmp_path):
    # dash 8 = 500 m/carretel; 1500 m = 3 carretéis; 2 camadas em 1 deck = 2 passagens = 3000 m de fio
    d, res = _cenario_trancadeira(tmp_path, "TR-01", metros=1500, rendimento=1500)
    assert len(eventos(res, "TR-01", "TROCA_TUBO")) == 3 * 2
    alim = eventos(res, "TR-01", "ALIMENTACAO_FIO")
    assert len(alim) == 1  # jogo 1 rende 1500, jogo 2 termina os outros 1500
    assert duracao_min(alim) == pytest.approx(47)
    seg_m = d.construcoes["GH781-8-A"].etapa("TRANCA").alternativas["TR-01"]
    assert duracao_min(eventos(res, "TR-01", "PRODUZINDO")) == pytest.approx(3000 * seg_m / 60)


def test_alimentacao_em_dois_decks_leva_o_dobro(tmp_path):
    _, res = _cenario_trancadeira(tmp_path, "TR-03", metros=2000, rendimento=1500)
    alim = eventos(res, "TR-03", "ALIMENTACAO_FIO")
    assert len(alim) == 1
    assert duracao_min(alim) == pytest.approx(94)


def test_troca_de_fio_descarta_o_resto_do_jogo(tmp_path):
    arquivo = planilha_custom(
        tmp_path, turnos=SEM_TURNOS,
        pedidos=pd.DataFrame([dict(pedido="A", produto="GH781-8", metros=500, data_entrega=pd.Timestamp("2026-11-30"), prioridade=None),
                              dict(pedido="B", produto="GH781-12", metros=400, data_entrega=pd.Timestamp("2026-11-30"), prioridade=None)]),
    )
    d = carregar(arquivo)
    plano = Plano(["A", "B"], {"A": ("GH781-8-B", "TR-08"), "B": ("GH781-12-B", "TR-08")})
    res = simular(d, plano, INICIO)
    rendimento_a = d.construcoes["GH781-8-B"].rendimento_m_por_jogo
    assert len(res.refugo) == 1
    assert res.refugo["metros_descartados"].iloc[0] == pytest.approx(rendimento_a - 500)
    assert res.refugo["fio"].iloc[0] == d.construcoes["GH781-8-B"].tipo_fio


# ------------------------------------------------------------------ planejamento
def test_busca_local_nao_piora_o_plano_inicial(dados):
    inicial = montar_plano(dados, ordem_edd(dados))
    base = kpis(simular(dados, inicial, INICIO))
    melhor, _ = busca_local(dados, inicial, INICIO, "Menor atraso total", max_simulacoes=20)
    assert kpis(melhor)["atraso_total_h"] <= base["atraso_total_h"]
