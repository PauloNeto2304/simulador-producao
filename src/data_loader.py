"""Leitura e validação da planilha de entrada (formato de data/template_fabrica.xlsx)."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pandas as pd

ABAS_OBRIGATORIAS = {
    "maquinas": ["maquina", "tipo", "ativa"],
    "dash": ["dash", "metros_por_carretel"],
    "construcoes": ["construcao", "produto", "camadas", "tipo_fio", "tipo_bobinadeira", "rendimento_m_por_jogo",
                    "tempo_bobinar_jogo_min"],
    "rotas": ["construcao", "ordem", "processo", "maquina", "seg_por_metro"],
    "autoclave": ["parametro", "valor"],
    "pedidos": ["pedido", "produto", "metros", "data_entrega"],
}
PROCESSO_LOTE = "VULCANIZACAO"
PROCESSO_TRANCA = "TRANCA"
PROCESSO_TUBO = "EXT_TUBO"
GRUPO_PADRAO = "GERAL"


class ErroPlanilha(Exception):
    """Planilha inválida; `erros` lista todos os problemas encontrados."""

    def __init__(self, erros: list[str]):
        super().__init__("\n".join(erros))
        self.erros = erros


@dataclass
class Maquina:
    nome: str
    tipo: str
    decks: int = 0
    jogos: int = 0


@dataclass
class Etapa:
    ordem: int
    processo: str
    alternativas: dict[str, float]  # maquina -> seg/metro (por passagem, na trança; vazio na vulcanização)


@dataclass
class Construcao:
    nome: str
    produto: str
    camadas: int
    tipo_fio: str
    tipo_bobinadeira: str
    rendimento_m_por_jogo: float
    tempo_bobinar_jogo_min: float
    etapas: list[Etapa]

    def etapa(self, processo: str) -> Etapa | None:
        return next((e for e in self.etapas if e.processo == processo), None)

    @property
    def trancadeiras(self) -> list[str]:
        e = self.etapa(PROCESSO_TRANCA)
        return list(e.alternativas) if e else []


@dataclass
class Pedido:
    pedido: str
    produto: str
    dash: int
    metros: float
    data_entrega: pd.Timestamp
    prioridade: float | None
    metros_por_carretel: float
    construcoes: list[str]  # construções possíveis (o planejador escolhe)

    @property
    def n_carreteis(self) -> int:
        return math.ceil(self.metros / self.metros_por_carretel)


@dataclass
class DadosFabrica:
    maquinas: dict[str, Maquina]
    construcoes: dict[str, Construcao]
    pedidos: list[Pedido]
    setups: dict[str, tuple[float, float]]  # maquina -> (troca_produto_min, troca_dash_min)
    alimentacao_min_por_deck: float
    troca_tubo_min: float
    pulmao_tubos: int
    inspecao_mandril_min: float
    mandris: dict[int, int]  # dash -> quantidade
    autoclave_capacidade: int
    autoclave_carga_min: float
    autoclave_espera_max_min: float
    autoclave_ciclo: dict[str, float]  # produto -> ciclo (min)
    autoclave_grupo: dict[str, str]  # produto -> grupo de mistura
    turnos: pd.DataFrame | None
    avisos: list[str] = field(default_factory=list)

    def do_tipo(self, tipo: str) -> list[str]:
        return [m.nome for m in self.maquinas.values() if m.tipo == tipo]

    def passagens(self, construcao: str, trancadeira: str) -> int:
        """2 camadas em máquina de 1 deck = 2 passagens."""
        return math.ceil(self.construcoes[construcao].camadas / self.maquinas[trancadeira].decks)

    def decks_em_uso(self, construcao: str, trancadeira: str) -> int:
        return min(self.construcoes[construcao].camadas, self.maquinas[trancadeira].decks)


def extrair_dash(produto: str) -> int | None:
    """GH781-12 -> 12."""
    m = re.search(r"-(\d+)\s*$", str(produto))
    return int(m.group(1)) if m else None


def _limpar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.dropna(how="all")
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: v.strip() if isinstance(v, str) else v)
    return df


def _numero(valor, padrao=0.0) -> float:
    return float(valor) if pd.notna(valor) and valor != "" else padrao


def _parametros(df: pd.DataFrame | None) -> dict:
    if df is None:
        return {}
    return {str(p): v for p, v in zip(df["parametro"], df["valor"])}


def carregar(arquivo) -> DadosFabrica:
    """Lê o Excel (caminho ou arquivo enviado) e devolve os dados validados."""
    abas = {nome.strip().lower(): _limpar(df) for nome, df in pd.read_excel(arquivo, sheet_name=None).items()}
    erros: list[str] = []
    avisos: list[str] = []

    for aba, colunas in ABAS_OBRIGATORIAS.items():
        if aba not in abas:
            erros.append(f"Aba '{aba}' não encontrada.")
            continue
        faltando = [c for c in colunas if c not in abas[aba].columns]
        if faltando:
            erros.append(f"Aba '{aba}': colunas ausentes {faltando}.")
    if erros:
        raise ErroPlanilha(erros)

    # Máquinas
    maquinas: dict[str, Maquina] = {}
    for _, r in abas["maquinas"].iterrows():
        if str(r["ativa"]).upper() == "N":
            continue
        nome, tipo = str(r["maquina"]), str(r["tipo"]).upper()
        m = Maquina(nome, tipo, int(_numero(r.get("decks"))), int(_numero(r.get("jogos_bobina"))))
        if tipo == "TRANCADEIRA" and (m.decks < 1 or m.jogos < 1):
            erros.append(f"Máquinas: trançadeira {nome} precisa de decks e jogos_bobina >= 1.")
        maquinas[nome] = m

    # Dash
    metros_dash = {int(d): float(m) for d, m in zip(abas["dash"]["dash"], abas["dash"]["metros_por_carretel"])}

    # Rotas por construção
    etapas_por_construcao: dict[str, list[Etapa]] = {}
    for construcao, grupo in abas["rotas"].groupby("construcao"):
        etapas = []
        for (ordem, processo), linhas in grupo.groupby(["ordem", "processo"], sort=True):
            alternativas = {}
            for _, r in linhas.iterrows():
                m = str(r["maquina"])
                if m not in maquinas:
                    erros.append(f"Rotas: {construcao} etapa {processo} usa a máquina '{m}', que não existe/está inativa.")
                    continue
                if processo == PROCESSO_LOTE:
                    alternativas[m] = 0.0
                    continue
                if processo == PROCESSO_TRANCA and maquinas[m].tipo != "TRANCADEIRA":
                    erros.append(f"Rotas: {construcao} etapa TRANCA usa '{m}', que não é do tipo TRANCADEIRA.")
                    continue
                if pd.isna(r["seg_por_metro"]) or float(r["seg_por_metro"]) <= 0:
                    erros.append(f"Rotas: {construcao} etapa {processo} máquina {m} sem seg_por_metro válido.")
                    continue
                alternativas[m] = float(r["seg_por_metro"])
            if alternativas:
                etapas.append(Etapa(int(ordem), str(processo), alternativas))
        etapas_por_construcao[str(construcao)] = etapas

    # Construções
    tipos_existentes = {m.tipo for m in maquinas.values()}
    construcoes: dict[str, Construcao] = {}
    for _, r in abas["construcoes"].iterrows():
        nome = str(r["construcao"])
        etapas = etapas_por_construcao.get(nome, [])
        if not etapas:
            erros.append(f"Construções: '{nome}' não tem rota na aba 'rotas'.")
            continue
        tipo_bob = str(r["tipo_bobinadeira"]).upper()
        if tipo_bob not in tipos_existentes:
            erros.append(f"Construções: '{nome}' usa tipo_bobinadeira '{tipo_bob}', sem nenhuma máquina ativa desse tipo.")
        if _numero(r["rendimento_m_por_jogo"]) <= 0 or _numero(r["camadas"]) < 1:
            erros.append(f"Construções: '{nome}' precisa de camadas >= 1 e rendimento_m_por_jogo > 0.")
        c = Construcao(
            nome=nome, produto=str(r["produto"]), camadas=int(_numero(r["camadas"], 1)), tipo_fio=str(r["tipo_fio"]),
            tipo_bobinadeira=tipo_bob, rendimento_m_por_jogo=_numero(r["rendimento_m_por_jogo"]),
            tempo_bobinar_jogo_min=_numero(r["tempo_bobinar_jogo_min"]), etapas=etapas,
        )
        if not c.trancadeiras:
            erros.append(f"Construções: '{nome}' não tem nenhuma trançadeira na etapa TRANCA.")
        construcoes[nome] = c
    sem_construcao = set(etapas_por_construcao) - set(construcoes)
    for nome in sorted(sem_construcao):
        avisos.append(f"Rotas: '{nome}' não está na aba 'construcoes' e foi ignorada.")

    # Setups (opcional)
    setups: dict[str, tuple[float, float]] = {}
    if "setups" in abas:
        for _, r in abas["setups"].iterrows():
            setups[str(r["maquina"])] = (_numero(r.get("setup_troca_produto_min")), _numero(r.get("setup_troca_dash_min")))

    # Parâmetros e mandris
    p = _parametros(abas.get("parametros"))
    mandris: dict[int, int] = {}
    if "mandris" in abas:
        mandris = {int(d): int(q) for d, q in zip(abas["mandris"]["dash"], abas["mandris"]["quantidade"])}

    # Autoclave
    params = _parametros(abas["autoclave"])
    ciclo_padrao = _numero(params.get("ciclo_min"), 120)
    ciclo: dict[str, float] = {}
    grupo_mistura: dict[str, str] = {}
    if "autoclave_produtos" in abas:
        for _, r in abas["autoclave_produtos"].iterrows():
            prod = str(r["produto"])
            ciclo[prod] = _numero(r.get("ciclo_min"), ciclo_padrao)
            g = r.get("grupo_mistura")
            grupo_mistura[prod] = str(g) if pd.notna(g) and str(g) else GRUPO_PADRAO

    # Pedidos
    construcoes_por_produto: dict[str, list[str]] = {}
    for c in construcoes.values():
        construcoes_por_produto.setdefault(c.produto, []).append(c.nome)

    pedidos: list[Pedido] = []
    for i, r in abas["pedidos"].iterrows():
        linha = i + 2
        produto = str(r["produto"])
        dash = extrair_dash(produto)
        if dash is None:
            erros.append(f"Pedidos linha {linha}: não consegui ler o dash de '{produto}' (esperado ...-8, ...-12).")
            continue
        if dash not in metros_dash:
            erros.append(f"Pedidos linha {linha}: dash {dash} não cadastrado na aba 'dash'.")
            continue
        if produto not in construcoes_por_produto:
            erros.append(f"Pedidos linha {linha}: produto '{produto}' sem construção na aba 'construcoes'.")
            continue
        entrega = pd.to_datetime(r["data_entrega"], dayfirst=True, errors="coerce")
        if pd.isna(entrega):
            erros.append(f"Pedidos linha {linha}: data_entrega inválida ({r['data_entrega']}).")
            continue
        if pd.isna(r["metros"]) or float(r["metros"]) <= 0:
            erros.append(f"Pedidos linha {linha}: metros inválido.")
            continue
        if produto not in ciclo:
            avisos.append(f"{produto}: sem linha em 'autoclave_produtos'; usando ciclo padrão e grupo {GRUPO_PADRAO}.")
            ciclo[produto] = ciclo_padrao
            grupo_mistura[produto] = GRUPO_PADRAO
        if mandris and dash not in mandris:
            avisos.append(f"Dash {dash}: sem quantidade na aba 'mandris'; considerando mandris ilimitados.")
        prioridade = r.get("prioridade")
        pedidos.append(
            Pedido(
                pedido=str(r["pedido"]), produto=produto, dash=dash, metros=float(r["metros"]),
                data_entrega=entrega,
                prioridade=float(prioridade) if pd.notna(prioridade) and prioridade != "" else None,
                metros_por_carretel=metros_dash[dash], construcoes=construcoes_por_produto[produto],
            )
        )

    if erros:
        raise ErroPlanilha(erros)

    turnos = abas.get("turnos")
    if turnos is not None and (turnos.empty or not {"inicio", "fim", "dias"} <= set(turnos.columns)):
        turnos = None

    return DadosFabrica(
        maquinas=maquinas,
        construcoes=construcoes,
        pedidos=pedidos,
        setups=setups,
        alimentacao_min_por_deck=_numero(p.get("alimentacao_fio_min_por_deck"), 47),
        troca_tubo_min=_numero(p.get("troca_tubo_min"), 15),
        pulmao_tubos=int(_numero(p.get("pulmao_tubos_por_trancadeira"), 2)),
        inspecao_mandril_min=_numero(p.get("inspecao_solda_mandril_min"), 0),
        mandris=mandris,
        autoclave_capacidade=int(_numero(params.get("capacidade_carreteis"), 5)),
        autoclave_carga_min=_numero(params.get("carga_descarga_min")),
        autoclave_espera_max_min=_numero(params.get("espera_max_lote_min")),
        autoclave_ciclo=ciclo,
        autoclave_grupo=grupo_mistura,
        turnos=turnos,
        avisos=sorted(set(avisos)),
    )
