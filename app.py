"""Interface Streamlit do simulador.

Rodar:  streamlit run app.py
"""
from __future__ import annotations

import io
from datetime import date, time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from src.data_loader import ErroPlanilha, carregar
from src.heuristics import OBJETIVOS, REGRAS, busca_local, calendario_para, montar_plano
from src.metrics import kpis, perdas_trancadeiras, utilizacao
from src.simulation import simular

TEMPLATE = Path(__file__).parent / "data" / "template_fabrica.xlsx"

# Um sistema de cor para o app inteiro: estado da máquina (paleta categórica em ordem fixa).
ROTULO_ESTADO = {
    "PRODUZINDO": "Produzindo", "BOBINANDO": "Produzindo", "INSPECAO_SOLDA": "Produzindo",
    "VULCANIZANDO": "Produzindo", "TROCA_TUBO": "Troca de tubo", "ALIMENTACAO_FIO": "Alimentação de fio",
    "SETUP": "Setup", "FALTA_FIO": "Falta de fio", "FALTA_TUBO": "Falta de tubo", "OCIOSA": "Ociosa",
}
CORES_ESTADO = {
    "Produzindo": "#2a78d6", "Troca de tubo": "#eb6834", "Alimentação de fio": "#1baf7a", "Setup": "#eda100",
    "Falta de fio": "#e87ba4", "Falta de tubo": "#008300", "Ociosa": "#c3c2b7",
}
ORDEM_ESTADOS = list(CORES_ESTADO)

st.set_page_config(page_title="Simulador de Produção", page_icon="🏭", layout="wide")
st.title("Simulador de Produção — Mangueiras Hidráulicas")
st.caption("Simulação de eventos discretos (SimPy) com a trançadeira como gargalo + busca do melhor plano.")


@st.cache_data(show_spinner=False)
def ler_planilha(conteudo: bytes):
    return carregar(io.BytesIO(conteudo))


# ---------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("1. Dados")
    arquivo = st.file_uploader("Planilha da fábrica (.xlsx)", type=["xlsx"])
    st.download_button("Baixar planilha modelo", TEMPLATE.read_bytes(), file_name="template_fabrica.xlsx")

    st.header("2. Parâmetros")
    dia = st.date_input("Início da produção", value=date(2026, 11, 2), format="DD/MM/YYYY")
    hora = st.time_input("Hora de início", value=time(5, 20))
    objetivo = st.selectbox("Objetivo", list(OBJETIVOS))
    usar_busca = st.checkbox("Otimizar com busca local", value=True)
    max_sim = st.slider("Simulações da busca", 20, 1000, 200, step=20, disabled=not usar_busca)
    rodar = st.button("Simular", type="primary", width="stretch")

conteudo = arquivo.getvalue() if arquivo else TEMPLATE.read_bytes()
if not arquivo:
    st.info("Nenhum arquivo enviado — usando a planilha de exemplo (dados fictícios).")

try:
    dados = ler_planilha(conteudo)
except ErroPlanilha as e:
    st.error("A planilha tem problemas:")
    for erro in e.erros:
        st.write(f"- {erro}")
    st.stop()

for aviso in dados.avisos:
    st.warning(aviso)

inicio = pd.Timestamp.combine(dia, hora)

with st.expander(f"Carteira: {len(dados.pedidos)} pedidos · {sum(p.n_carreteis for p in dados.pedidos)} carretéis",
                 expanded=not rodar and "resultado" not in st.session_state):
    st.dataframe(
        pd.DataFrame([dict(pedido=p.pedido, produto=p.produto, dash=p.dash, metros=p.metros, carreteis=p.n_carreteis,
                           data_entrega=p.data_entrega.date(), construcoes_possiveis=", ".join(p.construcoes))
                      for p in dados.pedidos]),
        hide_index=True, width="stretch",
    )

if not rodar and "resultado" not in st.session_state:
    st.stop()

# ---------------------------------------------------------------- simulação
if rodar:
    nota = OBJETIVOS[objetivo]
    cal = calendario_para(dados, inicio)
    with st.spinner("Simulando regras..."):
        cenarios = {nome: simular(dados, montar_plano(dados, regra(dados, inicio)), inicio, cal)
                    for nome, regra in REGRAS.items()}
    if usar_busca:
        melhor_regra = min(cenarios, key=lambda n: nota(kpis(cenarios[n])))
        barra = st.progress(0.0, text=f"Busca local a partir de '{melhor_regra}'...")
        res, n = busca_local(dados, cenarios[melhor_regra].plano, inicio, objetivo, max_sim,
                             progresso=lambda f: barra.progress(min(f, 1.0)), calendario=cal)
        barra.empty()
        cenarios[f"Busca local ({n} simulações)"] = res
    st.session_state["resultado"] = (cenarios, objetivo)

cenarios, objetivo_usado = st.session_state["resultado"]
nota = OBJETIVOS[objetivo_usado]

comparacao = pd.DataFrame([{"cenário": n, **kpis(r)} for n, r in cenarios.items()])
comparacao["_nota"] = [nota(kpis(r)) for r in cenarios.values()]
comparacao = comparacao.sort_values("_nota").drop(columns="_nota").reset_index(drop=True)

st.subheader("Comparação de cenários")
st.caption(f"Ordenado pelo objetivo: **{objetivo_usado}**")
st.dataframe(
    comparacao.rename(columns={
        "atrasados": "pedidos atrasados", "atraso_total_h": "atraso total (h)", "atraso_max_h": "atraso máx (h)",
        "makespan_h": "makespan (h)", "setup_total_h": "setup (h)", "refugo_fio_m": "refugo fio (m)",
        "falta_fio_h": "falta de fio (h)", "falta_tubo_h": "falta de tubo (h)",
        "trancadeiras_produzindo_pct": "trançadeiras produzindo (%)", "lotes_autoclave": "lotes autoclave",
        "ocupacao_media_autoclave": "carretéis/lote",
    }),
    hide_index=True, width="stretch",
)

escolhido = st.selectbox("Detalhar cenário", comparacao["cenário"], index=0)
res = cenarios[escolhido]
k = kpis(res)

cols = st.columns(7)
cols[0].metric("Pedidos atrasados", f"{k['atrasados']} / {k['pedidos']}")
cols[1].metric("Atraso total", f"{k['atraso_total_h']} h")
cols[2].metric("Makespan", f"{k['makespan_h']} h")
cols[3].metric("Trança produzindo", f"{k['trancadeiras_produzindo_pct']}%")
cols[4].metric("Falta de tubo", f"{k['falta_tubo_h']} h")
cols[5].metric("Falta de fio", f"{k['falta_fio_h']} h")
cols[6].metric("Refugo de fio", f"{k['refugo_fio_m']:,.0f} m".replace(",", "."))

aba_tranc, aba_gantt, aba_ped, aba_plano, aba_auto, aba_util = st.tabs(
    ["Trançadeiras (gargalo)", "Gantt", "Pedidos", "Plano das trançadeiras", "Autoclave e refugo", "Utilização"]
)

# ---------------------------------------------------------------- perdas das trançadeiras
with aba_tranc:
    st.markdown("Onde vai o tempo de trabalho de cada trançadeira (dentro dos turnos, do início ao fim do plano).")
    perdas = perdas_trancadeiras(res).assign(estado=lambda df: df["estado"].map(ROTULO_ESTADO))
    fig = px.bar(
        perdas, y="trancadeira", x="pct", color="estado", orientation="h",
        color_discrete_map=CORES_ESTADO, category_orders={"estado": ORDEM_ESTADOS},
        hover_data={"horas": ":.1f", "pct": ":.1f"}, labels={"pct": "% do tempo de trabalho", "trancadeira": ""},
    )
    fig.update_layout(barmode="stack", height=90 + 32 * len(res.trancadeiras), legend_title_text="",
                      legend=dict(orientation="h", y=1.0, yanchor="bottom"), margin=dict(t=50), bargap=0.35)
    fig.update_xaxes(range=[0, 100], ticksuffix="%")
    fig.update_yaxes(categoryorder="category descending")
    st.plotly_chart(fig, width="stretch")
    st.dataframe(perdas.pivot(index="trancadeira", columns="estado", values="horas")[ORDEM_ESTADOS].round(1),
                 width="stretch")
    st.caption("Tabela em horas. 'Falta de tubo' = esperando a extrusora/mandril; 'Falta de fio' = jogo reserva "
               "ainda não bobinado.")

# ---------------------------------------------------------------- Gantt
with aba_gantt:
    ev = res.eventos.assign(estado_rotulo=lambda df: df["estado"].map(ROTULO_ESTADO))
    destacar = st.selectbox("Destacar pedido", ["(todos)"] + list(res.plano.sequencia))
    ordem_maquinas = [m for m in dados.maquinas if m in set(ev["maquina"])]
    fig = px.timeline(
        ev, x_start="inicio", x_end="fim", y="maquina", color="estado_rotulo",
        color_discrete_map=CORES_ESTADO, category_orders={"maquina": ordem_maquinas, "estado_rotulo": ORDEM_ESTADOS},
        hover_data={"pedido": True, "carretel": True, "construcao": True, "detalhe": True, "maquina": False,
                    "estado_rotulo": False},
    )
    if destacar != "(todos)":
        for trace in fig.data:
            pedidos_trace = [cd[0] for cd in trace.customdata]
            trace.marker.opacity = [1.0 if p == destacar else 0.15 for p in pedidos_trace]
    fig.update_yaxes(autorange="reversed", title=None)
    fig.update_layout(height=140 + 26 * len(ordem_maquinas), legend_title_text="", margin=dict(t=50),
                      legend=dict(orientation="h", y=1.0, yanchor="bottom"))
    st.plotly_chart(fig, width="stretch")

# ---------------------------------------------------------------- tabelas
with aba_ped:
    st.dataframe(
        res.pedidos.style.apply(
            lambda linha: ["background-color: #f8d7da" if not linha["no_prazo"] else "" for _ in linha], axis=1),
        hide_index=True, width="stretch",
    )

with aba_plano:
    plano = res.pedidos.assign(passagens=[dados.passagens(c, t) for c, t in zip(res.pedidos["construcao"],
                                                                                 res.pedidos["trancadeira"])])
    tranc = st.selectbox("Trançadeira", res.trancadeiras)
    st.dataframe(
        plano[plano["trancadeira"] == tranc][["posicao_sequencia", "pedido", "produto", "construcao", "passagens",
                                              "carreteis", "data_entrega", "conclusao", "atraso_h"]],
        hide_index=True, width="stretch",
    )

with aba_auto:
    st.markdown("**Lotes da autoclave**")
    st.dataframe(res.lotes, hide_index=True, width="stretch")
    st.markdown("**Refugo de fio** (resto do jogo descartado na troca de tipo de fio)")
    st.dataframe(res.refugo, hide_index=True, width="stretch")

with aba_util:
    st.dataframe(utilizacao(res), hide_index=True, width="stretch")

buffer = io.BytesIO()
with pd.ExcelWriter(buffer, engine="openpyxl") as xls:
    res.pedidos.to_excel(xls, sheet_name="pedidos", index=False)
    res.operacoes.sort_values(["maquina", "inicio"]).to_excel(xls, sheet_name="programacao", index=False)
    res.eventos.sort_values(["maquina", "inicio"]).to_excel(xls, sheet_name="eventos", index=False)
    perdas_trancadeiras(res).to_excel(xls, sheet_name="perdas_trancadeiras", index=False)
    res.lotes.to_excel(xls, sheet_name="autoclave", index=False)
    res.refugo.to_excel(xls, sheet_name="refugo_fio", index=False)
    comparacao.to_excel(xls, sheet_name="cenarios", index=False)
st.download_button("Baixar programação (Excel)", buffer.getvalue(), file_name="programacao.xlsx", type="primary")
