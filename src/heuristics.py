"""Planejamento: escolhe ordem dos pedidos, construção e trançadeira; a simulação dá a nota.

O SimPy não otimiza sozinho. Aqui:
1. regras clássicas geram uma ORDEM de pedidos (planilha, EDD, SPT, menor folga);
2. `montar_plano` percorre essa ordem e coloca cada pedido na combinação construção + trançadeira
   que termina mais cedo (a trançadeira é o gargalo, então é ela que se planeja);
3. `busca_local` parte do melhor plano e testa mudanças (mover pedido na fila, trocar trançadeira ou
   construção), simulando cada tentativa e ficando com o que melhora o objetivo.
"""
from __future__ import annotations

import random
from typing import Callable

import pandas as pd

from .calendario import Calendario
from .data_loader import PROCESSO_TRANCA, DadosFabrica, Pedido
from .metrics import kpis
from .simulation import Plano, ResultadoSimulacao, simular


def calendario_para(dados: DadosFabrica, inicio: pd.Timestamp) -> Calendario:
    if dados.turnos is None:
        return Calendario.sempre_aberto(inicio)
    return Calendario.de_turnos(dados.turnos, inicio)


def opcoes(dados: DadosFabrica, p: Pedido) -> list[tuple[str, str]]:
    """Todas as combinações (construção, trançadeira) possíveis para o pedido."""
    return [(c, t) for c in p.construcoes for t in dados.construcoes[c].trancadeiras]


def horas_trancadeira(dados: DadosFabrica, p: Pedido, construcao: str, tranc: str) -> float:
    """Estimativa de horas que o pedido ocupa a trançadeira (trança + troca de tubo + alimentação de fio)."""
    c = dados.construcoes[construcao]
    passagens = dados.passagens(construcao, tranc)
    metros = p.n_carreteis * p.metros_por_carretel * passagens
    producao = metros * c.etapa(PROCESSO_TRANCA).alternativas[tranc] / 3600
    troca_tubo = p.n_carreteis * passagens * dados.troca_tubo_min / 60
    alimentacao = metros / c.rendimento_m_por_jogo * dados.alimentacao_min_por_deck * dados.decks_em_uso(construcao, tranc) / 60
    return producao + troca_tubo + alimentacao


def montar_plano(dados: DadosFabrica, ordem: list[str]) -> Plano:
    """Lista de prioridade: cada pedido vai para a combinação que termina mais cedo na trançadeira."""
    por_codigo = {p.pedido: p for p in dados.pedidos}
    carga = {t: 0.0 for t in dados.do_tipo("TRANCADEIRA")}
    ultimo_fio: dict[str, str] = {}
    atribuicao = {}
    for codigo in ordem:
        p = por_codigo[codigo]

        def custo(op):
            c, t = op
            fio = dados.construcoes[c].tipo_fio
            troca_fio = ultimo_fio.get(t) not in (None, fio)
            penalidade = dados.alimentacao_min_por_deck * dados.decks_em_uso(c, t) / 60 if troca_fio else 0.0
            return carga[t] + horas_trancadeira(dados, p, c, t) + penalidade

        c, t = min(opcoes(dados, p), key=custo)
        atribuicao[codigo] = (c, t)
        carga[t] = custo((c, t))
        ultimo_fio[t] = dados.construcoes[c].tipo_fio
    return Plano(list(ordem), atribuicao)


# ---------------------------------------------------------------------- regras de ordem
def _prioridade_manual(p: Pedido) -> float:
    return p.prioridade if p.prioridade is not None else float("inf")


def _horas_min(dados: DadosFabrica, p: Pedido) -> float:
    return min(horas_trancadeira(dados, p, c, t) for c, t in opcoes(dados, p))


def ordem_planilha(dados: DadosFabrica, inicio=None) -> list[str]:
    ordem = {p.pedido: i for i, p in enumerate(dados.pedidos)}
    return [p.pedido for p in sorted(dados.pedidos, key=lambda p: (_prioridade_manual(p), ordem[p.pedido]))]


def ordem_edd(dados: DadosFabrica, inicio=None) -> list[str]:
    """Earliest Due Date: entrega mais cedo primeiro."""
    return [p.pedido for p in sorted(dados.pedidos, key=lambda p: (_prioridade_manual(p), p.data_entrega))]


def ordem_spt(dados: DadosFabrica, inicio=None) -> list[str]:
    """Shortest Processing Time: pedidos mais rápidos (na trançadeira) primeiro."""
    return [p.pedido for p in sorted(dados.pedidos, key=lambda p: (_prioridade_manual(p), _horas_min(dados, p)))]


def ordem_menor_folga(dados: DadosFabrica, inicio: pd.Timestamp) -> list[str]:
    """Menor folga: (prazo - início) - horas de trançadeira."""
    def folga(p: Pedido) -> float:
        return (p.data_entrega - inicio).total_seconds() / 3600 - _horas_min(dados, p)
    return [p.pedido for p in sorted(dados.pedidos, key=lambda p: (_prioridade_manual(p), folga(p)))]


REGRAS: dict[str, Callable] = {
    "Ordem da planilha": ordem_planilha,
    "EDD (entrega mais cedo)": ordem_edd,
    "SPT (mais rápido primeiro)": ordem_spt,
    "Menor folga": ordem_menor_folga,
}

OBJETIVOS: dict[str, Callable[[dict], tuple]] = {
    "Menor atraso total": lambda k: (k["atraso_total_h"], k["atrasados"], k["makespan_h"]),
    "Menos pedidos atrasados": lambda k: (k["atrasados"], k["atraso_total_h"], k["makespan_h"]),
    "Terminar tudo antes (makespan)": lambda k: (k["makespan_h"], k["atraso_total_h"]),
    "Menor refugo de fio": lambda k: (k["refugo_fio_m"], k["atraso_total_h"], k["makespan_h"]),
}


# ---------------------------------------------------------------------- busca local
def _vizinho(dados: DadosFabrica, plano: Plano, res: ResultadoSimulacao, rnd: random.Random) -> Plano:
    novo = plano.copiar()
    atrasados = list(res.pedidos.loc[~res.pedidos["no_prazo"], "pedido"])
    codigo = rnd.choice(atrasados) if atrasados and rnd.random() < 0.6 else rnd.choice(novo.sequencia)
    p = next(x for x in dados.pedidos if x.pedido == codigo)
    alternativas = [op for op in opcoes(dados, p) if op != novo.atribuicao[codigo]]

    if alternativas and rnd.random() < 0.4:
        novo.atribuicao[codigo] = rnd.choice(alternativas)  # outra trançadeira e/ou construção
    else:
        i = novo.sequencia.index(codigo)
        novo.sequencia.pop(i)
        # pedidos atrasados tendem a subir na fila
        j = rnd.randint(0, i) if codigo in atrasados else rnd.randint(0, len(novo.sequencia))
        novo.sequencia.insert(j, codigo)
    return novo


def busca_local(
    dados: DadosFabrica,
    inicial: Plano,
    inicio: pd.Timestamp,
    objetivo: str = "Menor atraso total",
    max_simulacoes: int = 200,
    progresso: Callable[[float], None] | None = None,
    calendario: Calendario | None = None,
    semente: int = 0,
) -> tuple[ResultadoSimulacao, int]:
    """Subida de encosta com vizinhanças aleatórias. Retorna o melhor resultado e nº de simulações."""
    cal = calendario or calendario_para(dados, inicio)
    nota = OBJETIVOS[objetivo]
    rnd = random.Random(semente)
    melhor = simular(dados, inicial, inicio, cal)
    melhor_nota = nota(kpis(melhor))
    for n in range(2, max_simulacoes + 1):
        candidato = _vizinho(dados, melhor.plano, melhor, rnd)
        res = simular(dados, candidato, inicio, cal)
        k = nota(kpis(res))
        if k < melhor_nota:
            melhor, melhor_nota = res, k
        if progresso:
            progresso(n / max_simulacoes)
    return melhor, max_simulacoes
