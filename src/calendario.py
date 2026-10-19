"""Calendário de trabalho a partir da aba `turnos`.

O tempo da simulação é em minutos desde `inicio`. O calendário guarda os intervalos em que a fábrica
trabalha e converte "N minutos de trabalho a partir de t" no instante real de término (pulando
domingos, folgas etc.).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import time

import pandas as pd

DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6}


def interpretar_dias(texto: str) -> set[int]:
    """'seg-sab' -> {0..5}; 'seg,qua,sex' -> {0,2,4}; vazio -> todos."""
    texto = str(texto or "").strip().lower()
    if not texto or texto == "nan":
        return set(range(7))
    dias: set[int] = set()
    for parte in texto.replace(";", ",").split(","):
        parte = parte.strip()
        if "-" in parte:
            a, b = (DIAS[p.strip()[:3]] for p in parte.split("-"))
            dias.update(range(a, b + 1) if a <= b else list(range(a, 7)) + list(range(0, b + 1)))
        elif parte:
            dias.add(DIAS[parte[:3]])
    return dias


def _minutos(t) -> int:
    if isinstance(t, str):
        t = time.fromisoformat(t)
    return t.hour * 60 + t.minute


@dataclass
class Calendario:
    inicio: pd.Timestamp
    intervalos: list[tuple[float, float]]  # [ini, fim) em minutos desde `inicio`, ordenados e sem sobreposição

    def __post_init__(self):
        self._inis = [a for a, _ in self.intervalos]

    @classmethod
    def sempre_aberto(cls, inicio: pd.Timestamp) -> "Calendario":
        return cls(pd.Timestamp(inicio), [(0.0, float("inf"))])

    @classmethod
    def de_turnos(cls, turnos: pd.DataFrame, inicio: pd.Timestamp, horizonte_dias: int = 120) -> "Calendario":
        inicio = pd.Timestamp(inicio)
        dia0 = inicio.normalize()
        brutos: list[tuple[float, float]] = []
        for d in range(-1, horizonte_dias):
            data = dia0 + pd.Timedelta(days=d)
            for _, t in turnos.iterrows():
                if data.weekday() not in interpretar_dias(t["dias"]):
                    continue
                ini, fim = _minutos(t["inicio"]), _minutos(t["fim"])
                if fim <= ini:  # turno que vira a meia-noite
                    fim += 24 * 60
                base = (data - inicio).total_seconds() / 60
                brutos.append((base + ini, base + fim))
        brutos.sort()
        unidos: list[list[float]] = []
        for a, b in brutos:
            if unidos and a <= unidos[-1][1]:
                unidos[-1][1] = max(unidos[-1][1], b)
            else:
                unidos.append([a, b])
        intervalos = [(max(a, 0.0), b) for a, b in unidos if b > 0]
        return cls(inicio, intervalos)

    def _indice(self, t: float) -> int:
        """Índice do primeiro intervalo que termina depois de t."""
        i = bisect.bisect_right(self._inis, t) - 1
        if i >= 0 and self.intervalos[i][1] > t:
            return i
        return i + 1

    def proximo_util(self, t: float) -> float:
        i = self._indice(t)
        if i >= len(self.intervalos):
            raise RuntimeError("Simulação passou do horizonte do calendário; aumente horizonte_dias.")
        return max(t, self.intervalos[i][0])

    def segmentos(self, t: float, duracao: float) -> list[tuple[float, float]]:
        """Trechos reais [ini, fim) que somam `duracao` minutos de trabalho começando em t."""
        partes = []
        i = self._indice(t)
        while duracao > 1e-9:
            if i >= len(self.intervalos):
                raise RuntimeError("Simulação passou do horizonte do calendário; aumente horizonte_dias.")
            a, b = self.intervalos[i]
            ini = max(t, a)
            fim = min(b, ini + duracao)
            partes.append((ini, fim))
            duracao -= fim - ini
            t = fim
            i += 1
        return partes

    def fim_trabalho(self, t: float, duracao: float) -> float:
        if duracao <= 0:
            return t
        return self.segmentos(t, duracao)[-1][1]

    def recortar(self, ini: float, fim: float) -> list[tuple[float, float]]:
        """Parte de [ini, fim) que cai dentro do horário de trabalho."""
        partes = []
        i = self._indice(ini)
        while i < len(self.intervalos) and self.intervalos[i][0] < fim:
            a, b = self.intervalos[i]
            if min(b, fim) > max(a, ini):
                partes.append((max(a, ini), min(b, fim)))
            i += 1
        return partes

    def minutos_uteis(self, ini: float, fim: float) -> float:
        return sum(b - a for a, b in self.recortar(ini, fim))
