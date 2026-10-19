"""Indicadores calculados a partir de um resultado de simulação."""
from __future__ import annotations

import pandas as pd

ESTADOS_ESPERA = {"FALTA_FIO", "FALTA_TUBO"}
ORDEM_ESTADOS_TRANCADEIRA = ["PRODUZINDO", "TROCA_TUBO", "ALIMENTACAO_FIO", "SETUP", "FALTA_FIO", "FALTA_TUBO", "OCIOSA"]


def _horas(df: pd.DataFrame) -> pd.Series:
    return (df["fim"] - df["inicio"]).dt.total_seconds() / 3600


def _horizonte_min(res) -> float:
    return (res.operacoes["fim"].max() - res.inicio).total_seconds() / 60


def horas_uteis(res) -> float:
    """Horas de trabalho (turnos) entre o início e o fim da simulação."""
    return res.calendario.minutos_uteis(0, _horizonte_min(res)) / 60


def perdas_trancadeiras(res) -> pd.DataFrame:
    """Horas por estado de cada trançadeira dentro do horário de trabalho (formato longo)."""
    disponivel = horas_uteis(res)
    ev = res.eventos[res.eventos["maquina"].isin(res.trancadeiras)]
    horas = _horas(ev).groupby([ev["maquina"], ev["estado"]]).sum()
    linhas = []
    for t in res.trancadeiras:
        usadas = 0.0
        for estado in ORDEM_ESTADOS_TRANCADEIRA[:-1]:
            h = float(horas.get((t, estado), 0.0))
            usadas += h
            linhas.append(dict(trancadeira=t, estado=estado, horas=h))
        linhas.append(dict(trancadeira=t, estado="OCIOSA", horas=max(0.0, disponivel - usadas)))
    df = pd.DataFrame(linhas)
    df["pct"] = (df["horas"] / disponivel * 100).round(1) if disponivel else 0.0
    df["horas"] = df["horas"].round(1)
    return df


def kpis(res) -> dict:
    ped, ev = res.pedidos, res.eventos
    tranc = ev[ev["maquina"].isin(res.trancadeiras)]
    h_tranc = _horas(tranc).groupby(tranc["estado"]).sum()
    disponivel_tranc = horas_uteis(res) * len(res.trancadeiras)
    return {
        "pedidos": len(ped),
        "atrasados": int((~ped["no_prazo"]).sum()),
        "atraso_total_h": round(float(ped["atraso_h"].sum()), 1),
        "atraso_max_h": round(float(ped["atraso_h"].max()), 1),
        "makespan_h": round(_horizonte_min(res) / 60, 1),
        "setup_total_h": round(float(_horas(ev[ev["estado"] == "SETUP"]).sum()), 1),
        "refugo_fio_m": round(float(res.refugo["metros_descartados"].sum()), 0) if not res.refugo.empty else 0.0,
        "falta_fio_h": round(float(h_tranc.get("FALTA_FIO", 0.0)), 1),
        "falta_tubo_h": round(float(h_tranc.get("FALTA_TUBO", 0.0)), 1),
        "trancadeiras_produzindo_pct": round(float(h_tranc.get("PRODUZINDO", 0.0)) / disponivel_tranc * 100, 1)
        if disponivel_tranc else 0.0,
        "lotes_autoclave": len(res.lotes),
        "ocupacao_media_autoclave": round(float(res.lotes["carreteis"].mean()), 2) if not res.lotes.empty else 0.0,
    }


def utilizacao(res) -> pd.DataFrame:
    """% do horário de trabalho em que cada máquina ficou ocupada (esperas não contam)."""
    disponivel = horas_uteis(res)
    ev = res.eventos[~res.eventos["estado"].isin(ESTADOS_ESPERA)]
    ocupado = _horas(ev).groupby(ev["maquina"]).sum()
    df = (ocupado / disponivel * 100).round(1).rename("utilizacao_%").reset_index()
    return df.sort_values("utilizacao_%", ascending=False).reset_index(drop=True)
