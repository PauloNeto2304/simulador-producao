"""Modelo de eventos discretos (SimPy) da linha de mangueiras, organizado em estações.

Unidade de fluxo: o carretel. Cada pedido vira ceil(metros / metros_por_carretel) carretéis.
Tempo da simulação em minutos desde `inicio`; o calendário de turnos define quando se trabalha.

As estações não chamam umas às outras — conversam por estoques (simpy.Store / Container):

    mandris[dash] ──► EXT_TUBO ──► pulmão de tubos ──► TRANÇADEIRA ──► jusante (cobertura, bandagem,
         ▲                         (limitado: corda)        ▲  │         autoclave, desbandagem, teste)
         │                                                  │  ▼                              │
         └──────── solda/inspeção ◄──── mandril volta ──────┼──┼──────────────────────────────┘
                                                     jogos  │  │ jogos
                                                     cheios │  │ vazios
                                                         BOBINAGEM

A trançadeira é o tambor (gargalo): o plano diz qual carretel vai em qual trançadeira e em que ordem,
e a extrusão do tubo e a bobinagem trabalham puxadas por essa fila.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
import simpy

from .calendario import Calendario
from .data_loader import PROCESSO_LOTE, PROCESSO_TRANCA, Construcao, DadosFabrica, Etapa, Pedido

# estados registrados no log de eventos
PRODUZINDO, SETUP = "PRODUZINDO", "SETUP"
TROCA_TUBO, ALIMENTACAO = "TROCA_TUBO", "ALIMENTACAO_FIO"
FALTA_FIO, FALTA_TUBO = "FALTA_FIO", "FALTA_TUBO"
BOBINANDO, INSPECAO, VULCANIZANDO = "BOBINANDO", "INSPECAO_SOLDA", "VULCANIZANDO"

PRIORIDADE_TUBO, PRIORIDADE_JUSANTE = 0, 1  # tubo (alimenta o gargalo) passa na frente da cobertura


@dataclass
class Plano:
    """Decisões do planejador: ordem global dos pedidos e, para cada pedido, (construção, trançadeira)."""
    sequencia: list[str]
    atribuicao: dict[str, tuple[str, str]]

    def copiar(self) -> "Plano":
        return Plano(list(self.sequencia), dict(self.atribuicao))


@dataclass
class Tarefa:
    pedido: Pedido
    carretel: int
    construcao: Construcao
    trancadeira: str
    posicao: int  # posição na fila da trançadeira
    rank: int  # posição do pedido na sequência global
    passagens: int
    decks_em_uso: int

    @property
    def metros(self) -> float:
        return self.pedido.metros_por_carretel

    @property
    def seg_m_tranca(self) -> float:
        return self.construcao.etapa(PROCESSO_TRANCA).alternativas[self.trancadeira]


@dataclass
class Jogo:
    nome: str
    fio: str | None = None
    restante: float = 0.0  # metros de mangueira (por passagem) que ainda rende


@dataclass
class ResultadoSimulacao:
    eventos: pd.DataFrame  # log de estados por máquina
    operacoes: pd.DataFrame  # uma linha por (carretel, etapa)
    lotes: pd.DataFrame  # ciclos de autoclave
    refugo: pd.DataFrame  # descartes de fio
    pedidos: pd.DataFrame  # conclusão e atraso por pedido
    inicio: pd.Timestamp
    calendario: Calendario
    plano: Plano
    trancadeiras: list[str] = field(default_factory=list)


# ====================================================================== estações
class Trancadeira:
    def __init__(self, fab: "Fabrica", nome: str, tarefas: list[Tarefa]):
        self.fab, self.nome, self.tarefas = fab, nome, tarefas
        env, d = fab.env, fab.dados
        self.tubos = simpy.FilterStore(env)
        self.cartoes = simpy.Container(env, init=d.pulmao_tubos, capacity=d.pulmao_tubos)
        self.prontos = simpy.FilterStore(env)  # jogos cheios esperando
        self.reservas_ativas = 0  # jogos cheios ou sendo bobinados para esta máquina
        self.ociosos: list[Jogo] = []

        # fila de consumo de fio: uma entrada por passagem
        self.consumos = [(t.construcao, t.metros) for t in tarefas for _ in range(t.passagens)]
        self.ponteiro, self.consumido_no_atual = 0, 0.0

        jogos = [Jogo(f"{nome}-J{i + 1}") for i in range(d.maquinas[nome].jogos)]
        self.montado = jogos[0]
        if tarefas:  # início: carrinhos prontos com o fio certo
            primeiro = self._blocos_futuros()[0]
            self.montado.fio, self.montado.restante = primeiro.tipo_fio, primeiro.rendimento_m_por_jogo
            blocos = self._blocos_futuros()
            for jogo, c in zip(jogos[1:], blocos):
                jogo.fio, jogo.restante = c.tipo_fio, c.rendimento_m_por_jogo
                self.reservas_ativas += 1
                self.prontos.put(jogo)
            self.ociosos.extend(jogos[1 + len(blocos):])
        env.process(self._rodar())

    def _blocos_futuros(self) -> list[Construcao]:
        """Construção (fio/rendimento) de cada próximo jogo que vai ser necessário, em ordem."""
        blocos: list[Construcao] = []
        fio, cap = self.montado.fio, self.montado.restante
        for i in range(self.ponteiro, len(self.consumos)):
            c, m = self.consumos[i]
            if i == self.ponteiro:
                m -= self.consumido_no_atual
            if c.tipo_fio != fio:
                blocos.append(c)
                fio, cap = c.tipo_fio, c.rendimento_m_por_jogo
            while m > cap + 1e-9:
                m -= cap
                blocos.append(c)
                cap = c.rendimento_m_por_jogo
            cap -= m
        return blocos

    def _reabastecer(self, jogo: Jogo):
        """Manda o jogo vazio para a bobinagem com o fio do próximo bloco ainda não coberto."""
        blocos = self._blocos_futuros()
        if self.reservas_ativas < len(blocos):
            self.reservas_ativas += 1
            self.fab.env.process(self.fab.bobinagem.bobinar(self, jogo, blocos[self.reservas_ativas - 1]))
        else:
            self.ociosos.append(jogo)

    def _trocar_jogo(self, t: Tarefa):
        fab, antigo = self.fab, self.montado
        if antigo.restante > 1e-6:
            fab.refugo.append(dict(trancadeira=self.nome, jogo=antigo.nome, fio=antigo.fio,
                                   metros_descartados=round(antigo.restante, 1), instante=fab.env.now,
                                   motivo=f"troca para {t.construcao.tipo_fio}"))
        t0 = fab.env.now
        novo = yield self.prontos.get(lambda j: j.fio == t.construcao.tipo_fio)
        self.reservas_ativas -= 1
        fab.esperar(self.nome, FALTA_FIO, t0, t)
        yield from fab.trabalhar(self.nome, ALIMENTACAO, fab.dados.alimentacao_min_por_deck * t.decks_em_uso, t,
                                 detalhe=f"{t.decks_em_uso} deck(s) · {novo.fio}")
        self.montado = novo
        antigo.fio, antigo.restante = None, 0.0
        self._reabastecer(antigo)

    def _rodar(self):
        fab, d = self.fab, self.fab.dados
        for t in self.tarefas:
            t0 = fab.env.now
            yield self.tubos.get(lambda x, t=t: x is t)
            fab.esperar(self.nome, FALTA_TUBO, t0, t)
            yield self.cartoes.put(1)  # abriu vaga no pulmão: a extrusora pode fazer o próximo tubo

            inicio = fab.env.now
            setup = fab.setup(self.nome, t)
            if setup:
                yield from fab.trabalhar(self.nome, SETUP, setup, t)
            for p in range(t.passagens):
                yield from fab.trabalhar(self.nome, TROCA_TUBO, d.troca_tubo_min, t)
                falta = t.metros
                while falta > 1e-9:
                    if self.montado.fio != t.construcao.tipo_fio or self.montado.restante <= 1e-9:
                        yield from self._trocar_jogo(t)
                    m = min(falta, self.montado.restante)
                    yield from fab.trabalhar(self.nome, PRODUZINDO, m * t.seg_m_tranca / 60, t,
                                             detalhe=f"passagem {p + 1}/{t.passagens}")
                    self.montado.restante -= m
                    falta -= m
                    self.consumido_no_atual += m
                self.ponteiro += 1
                self.consumido_no_atual = 0.0
            fab.ultimo_na_maquina[self.nome] = (t.pedido.produto, t.pedido.dash)
            fab.registrar_operacao(t, PROCESSO_TRANCA, self.nome, inicio)
            fab.env.process(fab.jusante(t))


class Bobinagem:
    """Pool de bobinadeiras por tipo; cada jogo é bobinado inteiro por uma única máquina."""

    def __init__(self, fab: "Fabrica"):
        self.fab = fab
        self.livres: dict[str, simpy.Store] = {}
        for m in fab.dados.maquinas.values():
            if m.tipo.startswith("BOBINADEIRA"):
                self.livres.setdefault(m.tipo, simpy.Store(fab.env)).put(m.nome)

    def bobinar(self, tranc: Trancadeira, jogo: Jogo, c: Construcao):
        fab = self.fab
        pool = self.livres[c.tipo_bobinadeira]
        maquina = yield pool.get()
        yield from fab.trabalhar(maquina, BOBINANDO, c.tempo_bobinar_jogo_min, None,
                                 detalhe=f"{jogo.nome} · {c.tipo_fio}")
        yield pool.put(maquina)
        jogo.fio, jogo.restante = c.tipo_fio, c.rendimento_m_por_jogo
        yield tranc.prontos.put(jogo)


@dataclass
class _ItemLote:
    tarefa: Tarefa
    grupo: str
    ciclo: float
    chegada: float
    pronto: simpy.Event


# ====================================================================== fábrica
class Fabrica:
    def __init__(self, dados: DadosFabrica, plano: Plano, inicio: pd.Timestamp, calendario: Calendario | None = None):
        self.dados = dados
        self.inicio = pd.Timestamp(inicio)
        self.cal = calendario or (
            Calendario.de_turnos(dados.turnos, self.inicio) if dados.turnos is not None
            else Calendario.sempre_aberto(self.inicio)
        )
        self.plano = plano
        self.env = simpy.Environment()
        self.eventos: list[dict] = []
        self.operacoes: list[dict] = []
        self.lotes: list[dict] = []
        self.refugo: list[dict] = []
        self.ultimo_na_maquina: dict[str, tuple[str, int]] = {}

        tarefas = self._montar_tarefas()
        self.recursos = {
            m: simpy.PriorityResource(self.env, capacity=1)
            for c in dados.construcoes.values() for e in c.etapas
            if e.processo not in (PROCESSO_TRANCA, PROCESSO_LOTE) for m in e.alternativas
        }
        self.mandris = {
            dash: simpy.Container(self.env, init=q, capacity=q) for dash, q in dados.mandris.items()
        }
        self.soldas = simpy.Store(self.env)
        for m in dados.do_tipo("SOLDA_MANDRIL"):
            self.soldas.put(m)
        self.bobinagem = Bobinagem(self)

        # autoclave
        self.fila_lote: list[_ItemLote] = []
        self._chegou_no_lote = self.env.event()
        self.pendentes_por_grupo: dict[str, int] = {}
        autoclaves = sorted({m for c in dados.construcoes.values() for e in c.etapas if e.processo == PROCESSO_LOTE
                             for m in e.alternativas})
        for t in (t for fila in tarefas.values() for t in fila):
            if t.construcao.etapa(PROCESSO_LOTE):
                g = dados.autoclave_grupo[t.pedido.produto]
                self.pendentes_por_grupo[g] = self.pendentes_por_grupo.get(g, 0) + 1
        for nome in autoclaves:
            self.env.process(self._autoclave(nome))

        self.trancadeiras = {nome: Trancadeira(self, nome, fila) for nome, fila in tarefas.items()}
        for fila in tarefas.values():
            for t in fila:
                self.env.process(self._tubo(t))

    def _montar_tarefas(self) -> dict[str, list[Tarefa]]:
        d, p = self.dados, self.plano
        por_codigo = {x.pedido: x for x in d.pedidos}
        filas: dict[str, list[Tarefa]] = {m: [] for m in d.do_tipo("TRANCADEIRA")}
        for rank, codigo in enumerate(p.sequencia):
            ped = por_codigo[codigo]
            nome_c, tranc = p.atribuicao[codigo]
            c = d.construcoes[nome_c]
            if c.produto != ped.produto or tranc not in c.trancadeiras:
                raise ValueError(f"Plano inválido para {codigo}: {nome_c} em {tranc}.")
            for n in range(1, ped.n_carreteis + 1):
                filas[tranc].append(Tarefa(ped, n, c, tranc, len(filas[tranc]), rank,
                                           d.passagens(nome_c, tranc), d.decks_em_uso(nome_c, tranc)))
        return filas

    # ------------------------------------------------------------------ utilitários de tempo e log
    def trabalhar(self, maquina: str, estado: str, duracao_min: float, t: Tarefa | None, detalhe: str = ""):
        """Consome `duracao_min` de tempo de trabalho (respeitando turnos) e registra no log."""
        if duracao_min <= 0:
            return
        segmentos = self.cal.segmentos(self.env.now, duracao_min)
        yield self.env.timeout(segmentos[-1][1] - self.env.now)
        for a, b in segmentos:
            self._log(maquina, estado, a, b, t, detalhe)

    def esperar(self, maquina: str, estado: str, desde: float, t: Tarefa | None):
        """Registra uma espera já ocorrida (só a parte dentro do horário de trabalho)."""
        for a, b in self.cal.recortar(desde, self.env.now):
            self._log(maquina, estado, a, b, t, "")

    def _log(self, maquina, estado, a, b, t, detalhe):
        self.eventos.append(dict(
            maquina=maquina, estado=estado, inicio=a, fim=b, detalhe=detalhe,
            pedido=t.pedido.pedido if t else None, carretel=t.carretel if t else None,
            construcao=t.construcao.nome if t else None,
        ))

    def registrar_operacao(self, t: Tarefa, processo: str, maquina: str, inicio: float, lote=None):
        self.operacoes.append(dict(
            pedido=t.pedido.pedido, carretel=t.carretel, produto=t.pedido.produto, construcao=t.construcao.nome,
            processo=processo, maquina=maquina, inicio=inicio, fim=self.env.now, lote=lote,
        ))

    def setup(self, maquina: str, t: Tarefa) -> float:
        anterior = self.ultimo_na_maquina.get(maquina)
        if anterior is None:
            return 0.0
        troca_produto, troca_dash = self.dados.setups.get(maquina, (0.0, 0.0))
        produto_ant, dash_ant = anterior
        if dash_ant != t.pedido.dash:
            return troca_dash
        return troca_produto if produto_ant != t.pedido.produto else 0.0

    # ------------------------------------------------------------------ fluxo do carretel
    def _tubo(self, t: Tarefa):
        """Antes da trança: pega vaga no pulmão (corda), pega mandril e extruda o tubo."""
        tranc = self.trancadeiras[t.trancadeira]
        yield tranc.cartoes.get(1)
        mandris = self.mandris.get(t.pedido.dash)
        if mandris is not None:
            yield mandris.get(1)
        ordem_tranca = t.construcao.etapa(PROCESSO_TRANCA).ordem
        for etapa in t.construcao.etapas:
            if etapa.ordem < ordem_tranca:
                yield from self._operar(t, etapa, (PRIORIDADE_TUBO, t.posicao))
        yield tranc.tubos.put(t)

    def jusante(self, t: Tarefa):
        ordem_tranca = t.construcao.etapa(PROCESSO_TRANCA).ordem
        for etapa in t.construcao.etapas:
            if etapa.ordem <= ordem_tranca:
                continue
            if etapa.processo == PROCESSO_LOTE:
                yield from self._entrar_no_lote(t)
            else:
                yield from self._operar(t, etapa, (PRIORIDADE_JUSANTE, t.rank))
        if t.pedido.dash in self.mandris:
            self.env.process(self._retornar_mandril(t))

    def _operar(self, t: Tarefa, etapa: Etapa, prioridade):
        pedidos_req = {m: self.recursos[m].request(priority=prioridade) for m in etapa.alternativas}
        yield self.env.any_of(list(pedidos_req.values()))
        liberadas = [m for m, r in pedidos_req.items() if r.triggered]
        maquina = min(liberadas, key=lambda m: etapa.alternativas[m])
        for m, r in pedidos_req.items():
            if m != maquina:
                if r.triggered:
                    self.recursos[m].release(r)
                else:
                    r.cancel()
        inicio = self.env.now
        setup = self.setup(maquina, t)
        if setup:
            yield from self.trabalhar(maquina, SETUP, setup, t)
        yield from self.trabalhar(maquina, PRODUZINDO, t.metros * etapa.alternativas[maquina] / 60, t,
                                  detalhe=etapa.processo)
        self.recursos[maquina].release(pedidos_req[maquina])
        self.ultimo_na_maquina[maquina] = (t.pedido.produto, t.pedido.dash)
        self.registrar_operacao(t, etapa.processo, maquina, inicio)

    def _retornar_mandril(self, t: Tarefa):
        if self.dados.inspecao_mandril_min > 0 and self.dados.do_tipo("SOLDA_MANDRIL"):
            maquina = yield self.soldas.get()
            yield from self.trabalhar(maquina, INSPECAO, self.dados.inspecao_mandril_min, t)
            yield self.soldas.put(maquina)
        yield self.mandris[t.pedido.dash].put(1)

    # ------------------------------------------------------------------ autoclave (lote)
    def _entrar_no_lote(self, t: Tarefa):
        d = self.dados
        item = _ItemLote(t, d.autoclave_grupo[t.pedido.produto], d.autoclave_ciclo[t.pedido.produto],
                         self.env.now, self.env.event())
        self.fila_lote.append(item)
        self.pendentes_por_grupo[item.grupo] -= 1
        self._chegou_no_lote.succeed()
        self._chegou_no_lote = self.env.event()
        yield item.pronto

    def _autoclave(self, nome: str):
        d = self.dados
        while True:
            if not self.fila_lote:
                yield self._chegou_no_lote
                continue
            lider = min(self.fila_lote, key=lambda x: (x.tarefa.rank, x.chegada))
            candidatos = sorted((x for x in self.fila_lote if x.grupo == lider.grupo),
                                key=lambda x: (x.tarefa.rank, x.chegada))
            if len(candidatos) < d.autoclave_capacidade and self.pendentes_por_grupo[lider.grupo] > 0:
                limite = min(x.chegada for x in candidatos) + d.autoclave_espera_max_min
                if limite > self.env.now:
                    yield self.env.any_of([self._chegou_no_lote, self.env.timeout(limite - self.env.now)])
                    continue

            lote = candidatos[: d.autoclave_capacidade]
            for x in lote:
                self.fila_lote.remove(x)
            # vulcanização não pode pausar: só começa em horário de trabalho e roda direto
            inicio = self.cal.proximo_util(self.env.now)
            fim = inicio + d.autoclave_carga_min + max(x.ciclo for x in lote)
            yield self.env.timeout(fim - self.env.now)

            id_lote = len(self.lotes) + 1
            pedidos = ", ".join(dict.fromkeys(x.tarefa.pedido.pedido for x in lote))
            self.lotes.append(dict(lote=id_lote, maquina=nome, grupo=lider.grupo, carreteis=len(lote),
                                   pedidos=pedidos, inicio=inicio, fim=fim))
            self._log(nome, VULCANIZANDO, inicio, fim, None, f"Lote {id_lote} · {len(lote)} carretéis · {pedidos}")
            for x in lote:
                self.registrar_operacao(x.tarefa, PROCESSO_LOTE, nome, inicio, lote=id_lote)
                x.pronto.succeed()

    # ------------------------------------------------------------------ execução
    def rodar(self) -> ResultadoSimulacao:
        self.env.run()
        esperados = sum(len(tr.tarefas) for tr in self.trancadeiras.values())
        concluidos = sum(1 for o in self.operacoes if o["processo"] == self.dados.construcoes[o["construcao"]].etapas[-1].processo)
        if concluidos < esperados:
            raise RuntimeError(f"Simulação travou: {concluidos} de {esperados} carretéis concluídos.")

        para_ts = lambda df, cols: df.assign(**{c: self.inicio + pd.to_timedelta(df[c], unit="min") for c in cols}) if not df.empty else df
        eventos = para_ts(pd.DataFrame(self.eventos), ["inicio", "fim"])
        ops = para_ts(pd.DataFrame(self.operacoes), ["inicio", "fim"])
        lotes = para_ts(pd.DataFrame(self.lotes), ["inicio", "fim"])
        refugo = para_ts(pd.DataFrame(self.refugo, columns=["trancadeira", "jogo", "fio", "metros_descartados",
                                                            "instante", "motivo"]), ["instante"])
        return ResultadoSimulacao(eventos, ops, lotes, refugo, self._resumo_pedidos(ops), self.inicio, self.cal,
                                  self.plano, sorted(self.trancadeiras))

    def _resumo_pedidos(self, ops: pd.DataFrame) -> pd.DataFrame:
        conclusao = ops.groupby("pedido")["fim"].max()
        rank = {p: i for i, p in enumerate(self.plano.sequencia)}
        linhas = []
        for p in self.dados.pedidos:
            prazo = p.data_entrega.normalize() + pd.Timedelta(days=1)  # entrega até o fim do dia
            fim = conclusao[p.pedido]
            atraso_h = max(0.0, (fim - prazo).total_seconds() / 3600)
            construcao, tranc = self.plano.atribuicao[p.pedido]
            linhas.append(dict(
                pedido=p.pedido, produto=p.produto, construcao=construcao, trancadeira=tranc, metros=p.metros,
                carreteis=p.n_carreteis, metros_produzidos=p.n_carreteis * p.metros_por_carretel,
                data_entrega=p.data_entrega.date(), posicao_sequencia=rank[p.pedido] + 1, conclusao=fim,
                atraso_h=round(atraso_h, 1), no_prazo=atraso_h == 0,
            ))
        return pd.DataFrame(linhas).sort_values("posicao_sequencia").reset_index(drop=True)


def simular(dados: DadosFabrica, plano: Plano, inicio: pd.Timestamp, calendario: Calendario | None = None) -> ResultadoSimulacao:
    return Fabrica(dados, plano, inicio, calendario).rodar()
