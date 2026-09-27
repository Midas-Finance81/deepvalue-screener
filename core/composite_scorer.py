"""
Scorer composite -- assemble les 3 piliers en un portefeuille pondere
========================================================================

Pipeline complet :
  1. Pilier 1 (insider_clusters) genere l'UNIVERS de candidats -- seuls les
     emetteurs avec un cluster d'achats d'inities (3-5 inities, >=200k$)
     entrent dans le reste du pipeline. La TAILLE du cluster contribue aussi
     au score final (pas seulement un filtre d'entree, confirme par Thomas).
  2. Pour chaque candidat : Pilier 2 (bond_quality) + Pilier 3 quantitatif
     (operational_profile) + Pilier 3 qualitatif (qualitative_profile) sont
     calcules et combines avec Pilier 1 en un score composite 0-100
     (ponderation 1/3 - 1/3 - 1/3, Pilier3 = moyenne quanti/quali).
  3. Momentum (garde-fou a 5 regimes, PAS un filtre binaire, PAS un facteur
     pondere dans le composite) : la combinaison tendance 6 mois / direction
     1 mois donne un ajustement de -2 a +2 points, ajoute directement au
     score composite pour obtenir le score final.
  4. Portefeuille : exactement 4 quartiles de 5 lignes (20 positions),
     ponderation par tranche (40% / 30% / 20% / 10% du capital), repartie
     egalement entre les positions de chaque quartile -- meilleures notes
     (quartile 1) = position la plus lourde.

FILTRES ET BONUS AJOUTES (demande explicite de Thomas) :
  - Prix plancher 5$/action : applique EN PREMIER (avant tout appel API
    Claude payant) pour ne pas gaspiller de budget sur des titres exclus.
  - Bonus "achat en creux" : si le prix moyen d'achat du cluster (Pilier 1)
    se situe dans le bas de la fourchette 52 semaines du titre, bonus de
    points ajoute au score Pilier 1 -- signal de conviction plus fort
    (les inities achetent malgre/a cause d'une baisse, pas en suivant une
    hausse). Bonus maximal si achat au plus bas, nul au-dela de 35% de la
    fourchette (seuil DIP_BUY_POSITION_THRESHOLD, a valider avec Thomas).

HYPOTHESES RESTANT A VALIDER (les seuils exacts, poses faute de
specification plus fine) :
  - Bornes de normalisation du score Pilier 1 (200k$ = 0 pt, 2M$ = 100 pts)
  - Seuil "plat" du momentum (+/-5% sur 6 mois)
  - Poids exacts des quartiles (40/30/20/10)
  - Seuil et magnitude du bonus achat en creux (35% de la fourchette, max 15 pts)

NON TESTE dans ce sandbox (pas d'acces a sec.gov, yfinance, ni l'API
Anthropic depuis ce reseau restreint) : a valider en local, brique par
brique comme d'habitude -- ce module fait beaucoup d'appels reseau et
d'appels API Claude payants, donc teste d'abord sur 2-3 candidats avant
de lancer sur l'univers complet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import yfinance as yf
import requests

from core.insider_clusters import collect_transactions, detect_clusters
from core.bond_quality import score_bond_quality, get_ticker_for_cik, get_shares_outstanding, COMPANY_FACTS_URL, SEC_HEADERS
from core.operational_profile import score_operational_profile
from core.quality_profile import score_qualitative_profile, is_us_domestic_filer
from core.persistence import record_run, print_rebalancing_summary

# --- Hypotheses de ponderation, a valider (voir docstring) -----------------
PILLAR_WEIGHTS = {"pillar1": 1 / 3, "pillar2": 1 / 3, "pillar3": 1 / 3}

# Momentum : garde-fou a 5 regimes (pas un filtre binaire), combinant la
# tendance moyen-terme (6 mois) et la direction recente (1 mois). Points
# ajoutes directement au score composite (0-100) -- effet volontairement
# modeste (+/-2 max), pour nuancer sans dominer le classement.
MOMENTUM_FLAT_THRESHOLD = 0.05  # +/-5% sur 6 mois = considere "plat"
MOMENTUM_POINTS = {
    "haussier": 2,             # tendance 6m haussiere + 1m positif
    "retournement_haussier": 1,  # tendance 6m baissiere + 1m positif
    "stagnation": 0,            # tendance 6m plate
    "retournement_baissier": -1,  # tendance 6m haussiere + 1m negatif
    "baissier": -2,             # tendance 6m baissiere + 1m negatif
}

TRANCHE_WEIGHTS = [0.40, 0.30, 0.20, 0.10]  # 4 quartiles, somme = 100%
POSITIONS_PER_TRANCHE = 5  # 4 x 5 = 20 lignes au total

# Bornes de normalisation du score Pilier 1 (taille du cluster -> 0-100)
PILLAR1_SCORE_FLOOR = 200_000    # seuil d'entree = 0 point
PILLAR1_SCORE_CEILING = 2_000_000  # 2M$ et plus = 100 points

# Bonus si un dirigeant cle (CEO/CFO/President/Chairman) fait partie des
# acheteurs du cluster -- signal juge plus fort qu'un achat d'initie
# generique (methode de reference partagee par Thomas).
PROMINENT_OFFICER_BONUS = 10

# Bonus si les inities du cluster detiennent collectivement une part
# significative du capital -- signal statique complementaire au signal
# d'achat (flux). Methode de reference : >=20% = bon signal, mais pas une
# regle stricte (peut aussi refleter une structure familiale/entrenchee,
# cf. video). LIMITE connue : notre calcul ne capture que les inities ayant
# transige recemment (donnee disponible via Form 4), pas l'ensemble des
# inities de l'entreprise -- sous-estimation probable de l'ownership reel.
OWNERSHIP_BONUS_THRESHOLD = 0.20
OWNERSHIP_BONUS_POINTS = 10

MIN_SHARE_PRICE = 3.0  # filtre plancher pour exclure les penny stocks les plus extremes

# Bonus "achat en creux" : si le prix moyen d'achat du cluster se situe
# dans le bas de la fourchette 52 semaines, c'est un signal de conviction
# plus fort (les inities achetent malgre/a cause de la baisse, pas en
# suivant une hausse). Position 0 = prix le plus bas de l'annee, 1 = le
# plus haut. Bonus max si achat au plus bas, nul au-dela du seuil.
DIP_BUY_POSITION_THRESHOLD = 0.35  # au-dela de 35% de la fourchette, pas de bonus
DIP_BUY_MAX_BONUS = 15  # points ajoutes au score Pilier 1 (avant plafonnement a 100)


@dataclass
class ScoredCandidate:
    ticker: str
    issuer_name: str
    pillar1_score: float
    pillar2_score: float | None
    pillar3_quant_score: float | None
    pillar3_qual_score: float | None
    pillar3_score: float | None
    composite_score: float | None  # avant ajustement momentum
    momentum_6m_return: float | None
    momentum_1m_return: float | None
    momentum_regime: str | None
    momentum_points: int
    final_score: float | None  # composite_score + momentum_points
    current_price: float | None = None
    dip_buy_position: float | None = None  # 0 = achat au plus bas 52 sem., 1 = au plus haut
    dip_buy_bonus: float = 0.0
    ownership_pct: float | None = None  # approximation, cf. limite documentee
    excluded_reason: str | None = None  # ex. "prix < 5$"
    weight: float = 0.0
    notes: list[str] = field(default_factory=list)


def _score_pillar1(total_value: float) -> float:
    span = PILLAR1_SCORE_CEILING - PILLAR1_SCORE_FLOOR
    score = (total_value - PILLAR1_SCORE_FLOOR) / span * 100
    return round(min(100.0, max(0.0, score)), 1)


def score_ownership_bonus(cik: str, cluster_shares_held: float | None) -> tuple[float | None, float]:
    """% du capital detenu par les inities du cluster (approximation, cf.
    limite documentee plus haut). Retourne (pourcentage, bonus de points).
    (None, 0.0) si les donnees sont indisponibles."""
    if not cluster_shares_held:
        return None, 0.0
    resp = requests.get(COMPANY_FACTS_URL.format(cik10=cik), headers=SEC_HEADERS, timeout=30)
    if resp.status_code != 200:
        return None, 0.0
    facts = resp.json()
    shares_outstanding = get_shares_outstanding(facts)
    if not shares_outstanding:
        return None, 0.0
    ownership_pct = cluster_shares_held / shares_outstanding
    bonus = OWNERSHIP_BONUS_POINTS if ownership_pct >= OWNERSHIP_BONUS_THRESHOLD else 0.0
    return round(ownership_pct, 4), bonus


def _score_pillar3_quant(profile) -> float:
    ratio = profile.reinvestment_ratio
    reinvestment_score = 50.0 if ratio is None else min(100.0, max(0.0, (ratio / 2.0) * 100))

    trend = profile.fcf_trend
    trend_score = {"hausse": 100.0, "stable": 50.0, "baisse": 0.0}.get(trend, 50.0)

    score = (reinvestment_score + trend_score) / 2
    if profile.is_selling_productive_assets:
        score = max(0.0, score - 20)
    return round(score, 1)


def get_current_price_and_52w_range(ticker: str) -> tuple[float | None, float | None, float | None]:
    """Retourne (prix actuel, plus bas 52 semaines, plus haut 52 semaines).
    Utilise pour le filtre prix plancher ET le bonus achat en creux --
    un seul appel reseau pour les deux, pas la peine de dupliquer."""
    hist = yf.Ticker(ticker).history(period="1y")
    if hist.empty:
        return None, None, None
    current_price = float(hist["Close"].iloc[-1])
    low_52w = float(hist["Low"].min())
    high_52w = float(hist["High"].max())
    return current_price, low_52w, high_52w


def score_dip_buy_bonus(avg_purchase_price: float | None, low_52w: float | None, high_52w: float | None) -> tuple[float | None, float]:
    """Position du prix moyen d'achat du cluster dans la fourchette 52
    semaines (0 = au plus bas, 1 = au plus haut), et bonus de points associe
    (max DIP_BUY_MAX_BONUS si achat au plus bas, 0 au-dela du seuil).
    Retourne (position, bonus) -- (None, 0.0) si donnees insuffisantes."""
    if avg_purchase_price is None or low_52w is None or high_52w is None or high_52w == low_52w:
        return None, 0.0
    position = (avg_purchase_price - low_52w) / (high_52w - low_52w)
    position = min(1.0, max(0.0, position))
    if position >= DIP_BUY_POSITION_THRESHOLD:
        return round(position, 3), 0.0
    bonus = DIP_BUY_MAX_BONUS * (1 - position / DIP_BUY_POSITION_THRESHOLD)
    return round(position, 3), round(bonus, 1)


def _price_return(ticker: str, days_back: int) -> float | None:
    """Rendement du prix sur les `days_back` derniers jours calendaires.
    Retourne None si les donnees prix sont indisponibles."""
    end = date.today()
    start = end - timedelta(days=days_back)
    hist = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat())
    if hist.empty or len(hist) < 2:
        return None
    first_close = hist["Close"].iloc[0]
    last_close = hist["Close"].iloc[-1]
    if first_close == 0:
        return None
    return round((last_close - first_close) / first_close, 4)


def classify_momentum(ticker: str) -> tuple[float | None, float | None, str | None, int]:
    """Classe le momentum en 5 regimes (tendance 6 mois x direction 1 mois)
    et retourne (rendement_6m, rendement_1m, regime, points). Si les
    donnees sont indisponibles, retourne (None, None, None, 0) -- points
    neutres, ne penalise ni n'avantage le candidat."""
    six_m = _price_return(ticker, 190)
    one_m = _price_return(ticker, 30)

    if six_m is None or one_m is None:
        return six_m, one_m, None, 0

    if abs(six_m) <= MOMENTUM_FLAT_THRESHOLD:
        regime = "stagnation"
    elif six_m > MOMENTUM_FLAT_THRESHOLD:
        regime = "haussier" if one_m >= 0 else "retournement_baissier"
    else:  # six_m < -MOMENTUM_FLAT_THRESHOLD
        regime = "retournement_haussier" if one_m > 0 else "baissier"

    return six_m, one_m, regime, MOMENTUM_POINTS[regime]


def score_candidate(
    cik: str, issuer_name: str, cluster_total_value: float,
    avg_purchase_price: float | None = None, has_prominent_officer: bool = False,
    cluster_shares_held: float | None = None,
) -> ScoredCandidate:
    ticker = get_ticker_for_cik(cik)
    if ticker is None:
        return ScoredCandidate(
            ticker="?", issuer_name=issuer_name,
            pillar1_score=_score_pillar1(cluster_total_value),
            pillar2_score=None, pillar3_quant_score=None, pillar3_qual_score=None,
            pillar3_score=None, composite_score=None,
            momentum_6m_return=None, momentum_1m_return=None,
            momentum_regime=None, momentum_points=0, final_score=None,
            notes=["ticker introuvable pour ce CIK -- exclu"],
        )

    # Filtre prix plancher EN PREMIER, avant tout appel API Claude payant --
    # inutile de depenser des appels sur un titre qu'on va exclure de toute facon.
    current_price, low_52w, high_52w = get_current_price_and_52w_range(ticker)
    if current_price is not None and current_price < MIN_SHARE_PRICE:
        return ScoredCandidate(
            ticker=ticker, issuer_name=issuer_name,
            pillar1_score=_score_pillar1(cluster_total_value),
            pillar2_score=None, pillar3_quant_score=None, pillar3_qual_score=None,
            pillar3_score=None, composite_score=None,
            momentum_6m_return=None, momentum_1m_return=None,
            momentum_regime=None, momentum_points=0, final_score=None,
            current_price=current_price,
            excluded_reason=f"prix ({current_price:.2f}$) < plancher ({MIN_SHARE_PRICE:.0f}$)",
            notes=[f"Exclu : prix {current_price:.2f}$ sous le plancher de {MIN_SHARE_PRICE:.0f}$"],
        )

    # Filtre univers "USA" : exclut les emetteurs prives etrangers (ex. TSM),
    # qui deposent un 20-F plutot qu'un 10-K et cassent les Piliers 2/3
    # (bases sur les tags US-GAAP et le MD&A des 10-K). Applique avant tout
    # appel API Claude payant, comme le filtre prix.
    if not is_us_domestic_filer(cik):
        return ScoredCandidate(
            ticker=ticker, issuer_name=issuer_name,
            pillar1_score=_score_pillar1(cluster_total_value),
            pillar2_score=None, pillar3_quant_score=None, pillar3_qual_score=None,
            pillar3_score=None, composite_score=None,
            momentum_6m_return=None, momentum_1m_return=None,
            momentum_regime=None, momentum_points=0, final_score=None,
            excluded_reason="emetteur etranger (ne depose pas de 10-K, hors univers USA)",
            notes=["Exclu : emetteur prive etranger (20-F, pas de 10-K) -- hors univers USA"],
        )

    notes = []
    dip_position, dip_bonus = score_dip_buy_bonus(avg_purchase_price, low_52w, high_52w)
    if dip_position is None:
        notes.append("Bonus achat en creux non calcule (prix moyen du cluster ou fourchette 52 sem. indisponible)")
    elif dip_bonus > 0:
        notes.append(f"Bonus achat en creux : +{dip_bonus} pts (achat a {dip_position:.0%} de la fourchette 52 sem.)")

    officer_bonus = PROMINENT_OFFICER_BONUS if has_prominent_officer else 0
    if officer_bonus > 0:
        notes.append(f"Bonus dirigeant cle : +{officer_bonus} pts (CEO/CFO/President/Chairman parmi les acheteurs)")

    ownership_pct, ownership_bonus = score_ownership_bonus(cik, cluster_shares_held)
    if ownership_pct is None:
        notes.append("Bonus ownership non calcule (donnees actions detenues/en circulation indisponibles)")
    elif ownership_bonus > 0:
        notes.append(f"Bonus ownership : +{ownership_bonus} pts (inities du cluster detiennent ~{ownership_pct:.1%} du capital, approximation)")

    pillar1_score = round(min(100.0, _score_pillar1(cluster_total_value) + dip_bonus + officer_bonus + ownership_bonus), 1)

    bond = score_bond_quality(ticker)
    pillar2_score = bond.quality_score if bond else None
    if bond is None:
        notes.append("Pilier 2 indisponible (donnees EBIT/interet manquantes)")

    ops = score_operational_profile(ticker)
    pillar3_quant = _score_pillar3_quant(ops) if ops else None
    if ops is None:
        notes.append("Pilier 3 quantitatif indisponible")

    qual = score_qualitative_profile(ticker)
    pillar3_qual = qual.qualitative_composite if qual else None
    if qual is None:
        notes.append("Pilier 3 qualitatif indisponible (extraction MD&A ou API Claude en echec)")

    pillar3_parts = [s for s in (pillar3_quant, pillar3_qual) if s is not None]
    pillar3_score = round(sum(pillar3_parts) / len(pillar3_parts), 1) if pillar3_parts else None

    composite = None
    weighted_parts = []
    total_weight_used = 0.0
    for score, w in [(pillar1_score, PILLAR_WEIGHTS["pillar1"]),
                      (pillar2_score, PILLAR_WEIGHTS["pillar2"]),
                      (pillar3_score, PILLAR_WEIGHTS["pillar3"])]:
        if score is not None:
            weighted_parts.append(score * w)
            total_weight_used += w
    if total_weight_used > 0:
        composite = round(sum(weighted_parts) / total_weight_used, 1)

    six_m, one_m, regime, points = classify_momentum(ticker)
    if regime is None:
        notes.append("Momentum indisponible (donnees prix introuvables) -- 0 point neutre applique")

    final_score = round(min(100.0, max(0.0, composite + points)), 1) if composite is not None else None

    return ScoredCandidate(
        ticker=ticker, issuer_name=issuer_name,
        pillar1_score=pillar1_score, pillar2_score=pillar2_score,
        pillar3_quant_score=pillar3_quant, pillar3_qual_score=pillar3_qual,
        pillar3_score=pillar3_score, composite_score=composite,
        momentum_6m_return=six_m, momentum_1m_return=one_m,
        momentum_regime=regime, momentum_points=points, final_score=final_score,
        current_price=current_price, dip_buy_position=dip_position, dip_buy_bonus=dip_bonus,
        ownership_pct=ownership_pct,
        notes=notes,
    )


def build_portfolio(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    """Classe par final_score (composite + ajustement momentum) decroissant,
    garde exactement 4 x POSITIONS_PER_TRANCHE lignes (20 par defaut), et
    assigne les poids par tranche (quartiles)."""
    target_size = len(TRANCHE_WEIGHTS) * POSITIONS_PER_TRANCHE
    eligible = [c for c in candidates if c.final_score is not None]
    eligible.sort(key=lambda c: c.final_score, reverse=True)
    portfolio = eligible[:target_size]

    if len(portfolio) < target_size:
        print(f"ATTENTION : seulement {len(portfolio)} candidats eligibles, "
              f"en dessous de la cible de {target_size} lignes (4 x {POSITIONS_PER_TRANCHE}).")

    # Poids "cible" fixe par position (quartile / POSITIONS_PER_TRANCHE),
    # INDEPENDANT du nombre de candidats reellement presents dans ce
    # quartile -- sinon un candidat isole dans un quartile recupere tout
    # le poids du quartile a lui seul (bug corrige suite au run de Thomas :
    # GRML, le pire score du lot, se retrouvait avec 30% du portefeuille).
    # On renormalise ensuite a 100% si le portefeuille est incomplet, en
    # gardant les proportions relatives entre quartiles intactes.
    base_weights = []
    for i, candidate in enumerate(portfolio):
        tranche_idx = min(i // POSITIONS_PER_TRANCHE, len(TRANCHE_WEIGHTS) - 1)
        base_weights.append(TRANCHE_WEIGHTS[tranche_idx] / POSITIONS_PER_TRANCHE)

    total_base_weight = sum(base_weights)
    for candidate, base_weight in zip(portfolio, base_weights):
        candidate.weight = round(base_weight / total_base_weight, 4) if total_base_weight > 0 else 0.0

    return portfolio


def run_screener(collection_days: int = 90) -> list[ScoredCandidate]:
    """Pipeline complet : Pilier 1 sur les derniers `collection_days` jours,
    puis scoring des candidats, puis construction du portefeuille."""
    end = date.today()
    start = end - timedelta(days=collection_days)
    print(f"Collecte des transactions d'inities ({start} -> {end})...")
    txs = collect_transactions(start, end)
    clusters = detect_clusters(txs)
    print(f"{len(clusters)} clusters detectes. Scoring des candidats...\n")

    scored = []
    for _, row in clusters.iterrows():
        print(f"Scoring {row['issuer_name']}...")
        candidate = score_candidate(
            row["issuer_cik"], row["issuer_name"], row["total_value"],
            row.get("avg_purchase_price"), row.get("has_prominent_officer", False),
            row.get("cluster_shares_held"),
        )
        scored.append(candidate)
        if candidate.excluded_reason:
            print(f"  -> EXCLU : {candidate.excluded_reason}")
        else:
            print(f"  -> composite: {candidate.composite_score}, momentum: {candidate.momentum_regime} "
                  f"({candidate.momentum_points:+d} pts), final: {candidate.final_score}")

    portfolio = build_portfolio(scored)

    run_id = record_run(scored, portfolio, collection_days)
    print(f"\nRun #{run_id} enregistre dans portfolio.db.")

    return portfolio


FINRA_BOND_SEARCH_URL = "https://www.finra.org/finra-data/fixed-income/bond"


def print_finra_checklist(portfolio: list[ScoredCandidate]) -> None:
    """Affiche une checklist pour la verification MANUELLE de negociabilite
    de la dette (decision prise avec Thomas : pas d'automatisation, l'API
    TRACE complete necessite un accord d'utilisateur FINRA). La recherche
    de base sur finra.org/finra-data/fixed-income/bond est accessible sans
    compte -- copier le nom de l'emetteur dans la recherche pour chaque
    ligne du portefeuille.

    On n'a pas le CUSIP de chaque titre dans nos donnees (ni Form 4, ni
    XBRL ne le fournissent), donc pas de lien direct fiable vers une fiche
    obligataire precise -- juste le nom pret a copier-coller pour la
    recherche manuelle."""
    print(f"\n=== CHECKLIST NEGOCIABILITE DE LA DETTE (verification manuelle) ===")
    print(f"Recherche sur : {FINRA_BOND_SEARCH_URL}\n")
    for c in portfolio:
        print(f"[ ] {c.ticker:6s} -- rechercher : \"{c.issuer_name}\"")


if __name__ == "__main__":
    result = run_screener()
    print("\n=== PORTEFEUILLE FINAL ===")
    for c in result:
        print(f"{c.ticker:6s} | final: {c.final_score:5.1f} (composite {c.composite_score:5.1f}, "
              f"momentum {c.momentum_regime}) | poids: {c.weight:.2%} | {c.issuer_name}")
    print_finra_checklist(result)
    print_rebalancing_summary()