"""
Pilier 2 — Qualite et endettement (Rating synthetique)
========================================================

Le rating Moody's officiel et les donnees TRACE de la FINRA ne sont pas en
acces libre (accord d'utilisateur requis, cf. note plus bas). On utilise donc
un RATING SYNTHETIQUE, calcule a partir des etats financiers publics
(SEC EDGAR), en suivant la methode d'Aswath Damodaran (NYU Stern) : le ratio
de couverture d'interets (EBIT / charges d'interet) est mappe sur une table
de correspondance construite a partir de l'historique des vraies notations
Moody's/S&P des entreprises US. C'est la meme logique que le tableau que tu
as partage, juste derivee des fondamentaux plutot que d'un abonnement payant.

Sources :
  - Mapping ticker -> CIK : https://www.sec.gov/files/company_tickers.json
  - Etats financiers (EBIT, charges d'interet) : SEC EDGAR XBRL "company facts"
    https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json
  - Table de correspondance : Damodaran, NYU Stern, donnees janvier 2026
    https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.html
    (table "large non-financial service firms" -- a ne PAS utiliser telle
    quelle pour les banques/assureurs, qui suivent une table differente)

Relation prix action / rendement obligataire :
  Le rendement obligataire = taux sans risque + spread de defaut. Quand la
  qualite percue du credit se degrade (spread qui monte), le rendement de
  l'obligation monte -- et empiriquement, ca coincide souvent avec une baisse
  du prix de l'action (meme risque de solvabilite anticipe par les deux
  marches). Ce module ne calcule pas cette relation (pas de donnees de
  rendement reel sans FINRA), il sert a filtrer la QUALITE du credit en amont.

PATCH (institutions financieres + filiales de financement captif) :
  Le ratio de couverture d'interets n'a pas de sens pour les banques/assureurs
  (leur activite EST de preter/emprunter -- ce n'est pas un signe de risque)
  ni pour les industriels avec grosse filiale de financement (ex. Ford/Ford
  Credit). Plutot que d'exclure ces entreprises
  (choix de O'Shaughnessy), on bascule sur un ratio de LEVIER (Fonds propres
  / Total actifs), methode standard pour juger la solidite d'un bilan
  bancaire. RECALIBRE (juillet 2026) sur les seuils reglementaires OFFICIELS
  de la FDIC (cadre Prompt Corrective Action, verifies via plusieurs 10-K
  reels) -- bien mieux ancre que l'echelle initiale inventee, meme si ce
  n'est toujours pas une equivalence exacte au ratio Tier 1 reglementaire
  strict (cf. note detaillee dans LEVERAGE_QUALITY_BANDS plus bas). Le
  probleme initial (JPM scorait moins bien que FLG/ex-NYCB) est corrige :
  les deux climent desormais correctement la barre "bien capitalise".

NON TESTE dans ce sandbox (pas d'acces a sec.gov ici) : a valider sur ta
machine sur 2-3 tickers connus, en comparant le rating obtenu a un rating
Moody's/S&P reel trouve en ligne (ex. via un communique de presse de
l'entreprise, souvent public meme sans abonnement).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import requests

SEC_HEADERS = {
    "User-Agent": "Thomas <snoopy.offroad@gmail.com> deep-value-screener/0.1",
}

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"

# Codes SIC 6000-6799 = finance/assurance/immobilier (secteur "financier"
# au sens large -- banques, assureurs, courtiers, holdings financieres).
FINANCIAL_SIC_PREFIX = "6"

STALE_DATA_THRESHOLD_DAYS = 550  # ~18 mois : au-dela, un tag XBRL est considere obsolete pour le TTM

EQUITY_TAGS = ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
TOTAL_ASSETS_TAGS = ["Assets"]

# Echelle de levier (Fonds propres GAAP / Total actifs) -- RECALIBREE sur
# les seuils reglementaires OFFICIELS de la FDIC (cadre "Prompt Corrective
# Action" post-Basel III, verifies via plusieurs 10-K reels en juillet 2026) :
#   - Bien capitalise (well capitalized)              : levier >= 5.0%
#   - Adequatement capitalise (adequately capitalized) : levier >= 4.0%
#   - Sous-capitalise (undercapitalized)                : levier < 4.0%
#   - Significativement sous-capitalise                 : levier < 3.0%
#   - Sous-capitalise de maniere critique (critical)     : levier <= 2.0%
# NUANCE : ces seuils officiels portent sur le ratio de levier Tier 1
# REGLEMENTAIRE (Tier 1 capital / actifs moyens), qui exclut certains
# elements (goodwill, une partie de l'AOCI) presents dans notre proxy
# (Fonds propres GAAP / Total actifs). C'est une approximation PROCHE mais
# pas une equivalence exacte -- toujours pas une table sourcee comme
# Damodaran au sens strict, mais bien mieux ancree que l'echelle inventee
# precedente (qui placait a tort le seuil "tres solide" a 12%, alors que la
# vraie barre reglementaire "bien capitalise" n'est que 5%).
# (borne_inf_incluse, borne_sup_incluse, label, score 0-100)
LEVERAGE_QUALITY_BANDS = [
    (-1e9, 0.02, "Sous-capitalise de maniere critique", 5),
    (0.02, 0.03, "Significativement sous-capitalise", 15),
    (0.03, 0.04, "Sous-capitalise", 30),
    (0.04, 0.05, "Adequatement capitalise", 50),
    (0.05, 0.08, "Bien capitalise", 70),
    (0.08, 1e9, "Tres bien capitalise (marge confortable)", 90),
]

# Tags XBRL candidats, par ordre de preference (les entreprises ne balisent
# pas toutes leurs comptes avec le meme tag ; on essaie dans l'ordre).
EBIT_TAGS = ["OperatingIncomeLoss"]
INTEREST_EXPENSE_TAGS = [
    "InterestExpense",
    "InterestExpenseDebt",
    "InterestAndDebtExpense",
    "InterestExpenseDebtExcludingAmortization",
]

# Table Damodaran (NYU Stern, donnees janvier 2026) -- grandes entreprises
# non financieres. (borne_inf_incluse, borne_sup_incluse, rating, spread_defaut)
SYNTHETIC_RATING_TABLE = [
    (-1e9, 0.199999, "D2/D", 0.19),
    (0.2, 0.649999, "C2/C", 0.16),
    (0.65, 0.799999, "Ca2/CC", 0.1261),
    (0.8, 1.249999, "Caa/CCC", 0.0885),
    (1.25, 1.499999, "B3/B-", 0.0509),
    (1.5, 1.749999, "B2/B", 0.0321),
    (1.75, 1.999999, "B1/B+", 0.0275),
    (2.0, 2.249999, "Ba2/BB", 0.0184),
    (2.25, 2.49999, "Ba1/BB+", 0.0138),
    (2.5, 2.999999, "Baa2/BBB", 0.0111),
    (3.0, 4.249999, "A3/A-", 0.0089),
    (4.25, 5.499999, "A2/A", 0.0078),
    (5.5, 6.499999, "A1/A+", 0.0070),
    (6.5, 8.499999, "Aa2/AA", 0.0055),
    (8.5, 1e9, "Aaa/AAA", 0.0040),
]

# Score numerique 0 (pire, D2/D) -> 100 (meilleur, Aaa/AAA), pour alimenter
# le scorer composite plus tard -- meme logique que le decile ranking du
# quant-screener existant.
RATING_ORDER = [r[2] for r in SYNTHETIC_RATING_TABLE]  # du pire au meilleur


@dataclass
class BondQualityResult:
    ticker: str
    cik: str
    method_used: str  # "interest_coverage" ou "leverage_ratio"
    quality_score: float  # 0-100, dans tous les cas
    rating_label: str  # rating synthetique (methode 1) ou label qualitatif (methode 2)
    ebit: float | None = None
    interest_expense: float | None = None
    interest_coverage: float | None = None
    default_spread: float | None = None
    equity: float | None = None
    total_assets: float | None = None
    leverage_ratio: float | None = None
    note: str | None = None


_ticker_to_cik_cache: dict[str, str] | None = None
_cik_to_ticker_cache: dict[str, str] | None = None


def _load_ticker_map() -> dict[str, str]:
    global _ticker_to_cik_cache, _cik_to_ticker_cache
    if _ticker_to_cik_cache is not None:
        return _ticker_to_cik_cache
    resp = requests.get(TICKER_MAP_URL, headers=SEC_HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    _ticker_to_cik_cache = {
        row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in data.values()
    }
    # cache inverse (premier ticker rencontre pour un CIK donne en cas de doublon rare)
    _cik_to_ticker_cache = {}
    for ticker, cik10 in _ticker_to_cik_cache.items():
        _cik_to_ticker_cache.setdefault(cik10, ticker)
    return _ticker_to_cik_cache


def get_cik_for_ticker(ticker: str) -> str | None:
    """Retourne le CIK a 10 chiffres pour un ticker, ou None si introuvable."""
    return _load_ticker_map().get(ticker.upper())


def get_ticker_for_cik(cik10: str) -> str | None:
    """Retourne le ticker pour un CIK a 10 chiffres, ou None si introuvable."""
    _load_ticker_map()
    return _cik_to_ticker_cache.get(cik10)


def _points_for_tag(facts_json: dict, tag: str, unit: str = "USD", namespace: str = "us-gaap") -> list[dict]:
    """Points pour UN SEUL tag XBRL (liste vide si absent). unit et
    namespace generalises pour couvrir les actions en circulation (unite
    "shares", parfois taguees dans le namespace "dei" plutot que "us-gaap")."""
    ns_facts = facts_json.get("facts", {}).get(namespace, {})
    tag_data = ns_facts.get(tag)
    if not tag_data:
        return []
    return tag_data.get("units", {}).get(unit, [])


SHARES_OUTSTANDING_TAGS = [
    ("CommonStockSharesOutstanding", "us-gaap"),
    ("EntityCommonStockSharesOutstanding", "dei"),
    ("CommonStockSharesIssued", "us-gaap"),
]


def get_shares_outstanding(facts_json: dict) -> float | None:
    """Nombre d'actions en circulation le plus recent (10-K ou 10-Q),
    essaie plusieurs tags/namespaces candidats. Utilise pour le critere
    d'ownership (% detenu par les inities)."""
    all_points = []
    for tag, namespace in SHARES_OUTSTANDING_TAGS:
        all_points.extend(_points_for_tag(facts_json, tag, unit="shares", namespace=namespace))
    if not all_points:
        return None
    latest = max(all_points, key=lambda p: p["end"])
    return float(latest["val"])


def _latest_instant_value(facts_json: dict, tags: list[str]) -> float | None:
    """Pour les mesures de BILAN (cash, equity, actifs -- des photos a un
    instant T, pas des flux) : prend la valeur la plus recente disponible,
    10-K OU 10-Q indifferemment, en fusionnant TOUS les tags candidats
    (pas juste le premier qui a des donnees) -- une entreprise peut avoir
    bascule d'un tag a l'autre au fil du temps, la donnee la plus fraiche
    peut se trouver sous n'importe lequel."""
    all_points = []
    for tag in tags:
        all_points.extend(_points_for_tag(facts_json, tag))
    if not all_points:
        return None
    latest = max(all_points, key=lambda p: p["end"])
    return float(latest["val"])


def _ttm_for_single_tag(points: list[dict]) -> float | None:
    """Calcule le TTM pour UN SEUL tag deja resolu (liste de points USD).
    Retourne None si la donnee la plus recente (10-K ou 10-Q) depasse le
    seuil de fraicheur -- le tag est alors considere obsolete pour ce tag
    precis (l'appelant essaiera le tag candidat suivant)."""
    duration_points = [p for p in points if "start" in p and "end" in p]
    annual_points = [p for p in duration_points if p.get("form") == "10-K" and p.get("fp") == "FY"]
    if not annual_points:
        return None
    latest_fy = max(annual_points, key=lambda p: p["end"])
    fy_val = float(latest_fy["val"])
    fy_end = date.fromisoformat(latest_fy["end"])

    # Cumul le plus recent qui depasse la fin du dernier exercice annuel
    # (donc un cumul "depuis le debut de l'exercice en cours"). En cas
    # d'egalite de date de fin (frequent : un 10-Q publie a la fois le
    # trimestre seul ET le cumul depuis le debut d'exercice), on prefere
    # la duree la PLUS LONGUE -- plus probablement le vrai cumul YTD qu'un
    # trimestre isole pris par erreur.
    post_fy_points = [p for p in duration_points if date.fromisoformat(p["end"]) > fy_end]

    # Garde-fou de fraicheur : verifie la donnee la PLUS RECENTE disponible
    # POUR CE TAG (10-K ou 10-Q, peu importe). Si NI le 10-K NI aucun 10-Q
    # ne sont recents, ce tag precis est structurellement obsolete (ex.
    # BYRN qui a arrete de baliser InterestExpense depuis 2021) -- on
    # retourne None pour que l'appelant essaie le tag candidat suivant
    # plutot que de basculer directement sur le repli levier.
    most_recent_end = max([fy_end] + [date.fromisoformat(p["end"]) for p in post_fy_points])
    if (date.today() - most_recent_end).days > STALE_DATA_THRESHOLD_DAYS:
        return None

    if not post_fy_points:
        return fy_val  # pas encore de trimestre publie depuis le dernier 10-K

    current_ytd = max(
        post_fy_points,
        key=lambda p: (p["end"], (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days),
    )
    current_ytd_val = float(current_ytd["val"])
    current_end = date.fromisoformat(current_ytd["end"])
    current_start = date.fromisoformat(current_ytd["start"])
    current_duration_days = (current_end - current_start).days

    # Cherche le cumul comparatif de la meme duree, ~1 an plus tot.
    target_prior_end = current_end - timedelta(days=365)
    prior_candidates = [
        p for p in duration_points
        if abs((date.fromisoformat(p["end"]) - target_prior_end).days) <= 15
        and abs((date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days - current_duration_days) <= 15
    ]
    if not prior_candidates:
        return fy_val  # comparatif introuvable, on ne fait pas de TTM approximatif au hasard

    prior_ytd = min(
        prior_candidates,
        key=lambda p: abs((date.fromisoformat(p["end"]) - target_prior_end).days),
    )
    prior_ytd_val = float(prior_ytd["val"])

    return fy_val + current_ytd_val - prior_ytd_val


def _ttm_duration_value(facts_json: dict, tags: list[str]) -> float | None:
    """Pour les mesures de FLUX (EBIT, charges d'interet, capex, D&A, OCF) :
    calcule un Trailing Twelve Months (12 mois glissants) plutot que de se
    limiter au dernier exercice annuel complet (qui peut avoir jusqu'a
    12-15 mois de retard). Formule standard :

        TTM = dernier exercice annuel complet (10-K)
            + cumul depuis le debut de l'exercice en cours (dernier 10-Q)
            - cumul de la meme periode l'annee precedente (10-Q comparatif)

    Essaie CHAQUE tag candidat dans l'ordre -- si un tag a des donnees mais
    qu'elles sont trop anciennes (obsoletes), passe au tag suivant plutot
    que d'abandonner immediatement. Bug trouve en conditions reelles : le
    premier tag qui avait ne serait-ce qu'UNE donnee (meme vieille de
    plusieurs annees) faisait abandonner la recherche sans jamais essayer
    les tags de repli (ex. AAPL/F basculaient a tort vers le ratio de
    levier alors qu'un tag alternatif avait des donnees recentes)."""
    for tag in tags:
        points = _points_for_tag(facts_json, tag)
        if not points:
            continue
        result = _ttm_for_single_tag(points)
        if result is not None:
            return result
    return None


def get_sic_code(cik: str) -> str | None:
    """Code SIC de l'entreprise (ex. '6022' = banque commerciale d'Etat).
    Utilise pour detecter les institutions financieres."""
    resp = requests.get(SUBMISSIONS_URL.format(cik10=cik), headers=SEC_HEADERS, timeout=30)
    if resp.status_code != 200:
        return None
    return resp.json().get("sic")


def is_financial_institution(cik: str) -> bool:
    sic = get_sic_code(cik)
    return sic is not None and sic.startswith(FINANCIAL_SIC_PREFIX)


def rating_for_coverage(coverage: float) -> tuple[str, float]:
    """Mappe un ratio de couverture d'interets sur (rating, spread de defaut)."""
    for low, high, rating, spread in SYNTHETIC_RATING_TABLE:
        if low <= coverage <= high:
            return rating, spread
    # Couverture negative extreme, hors table -> pire notation
    return SYNTHETIC_RATING_TABLE[0][2], SYNTHETIC_RATING_TABLE[0][3]


def _quality_score(rating: str) -> float:
    """Convertit le rating en score 0-100 (position dans l'echelle triee)."""
    idx = RATING_ORDER.index(rating)
    return round(100 * idx / (len(RATING_ORDER) - 1), 1)


def label_for_leverage(leverage_ratio: float) -> tuple[str, float]:
    """Mappe un ratio de levier (equity/assets) sur (label, score 0-100).
    Echelle approximative -- cf. avertissement en tete de fichier."""
    for low, high, label, score in LEVERAGE_QUALITY_BANDS:
        if low <= leverage_ratio <= high:
            return label, float(score)
    return LEVERAGE_QUALITY_BANDS[0][2], float(LEVERAGE_QUALITY_BANDS[0][3])


def _score_via_leverage(ticker: str, cik: str, facts: dict, note: str) -> BondQualityResult | None:
    equity = _latest_instant_value(facts, EQUITY_TAGS)
    assets = _latest_instant_value(facts, TOTAL_ASSETS_TAGS)
    if equity is None or assets is None or assets == 0:
        print(f"[{ticker}] Fonds propres ou total actifs introuvables -- aucune methode de scoring applicable.")
        return None

    leverage_ratio = equity / assets
    label, score = label_for_leverage(leverage_ratio)

    return BondQualityResult(
        ticker=ticker.upper(),
        cik=cik,
        method_used="leverage_ratio",
        quality_score=score,
        rating_label=label,
        equity=equity,
        total_assets=assets,
        leverage_ratio=round(leverage_ratio, 4),
        note=note,
    )


def score_bond_quality(ticker: str) -> BondQualityResult | None:
    """Pipeline complet : ticker -> CIK -> methode adaptee au secteur.

    - Institution financiere (SIC 6xxx) -> ratio de levier directement.
    - Sinon -> ratio de couverture d'interets (Damodaran). Si le resultat
      tombe dans les 2 pires tranches (D2/D, C2/C) ALORS QUE l'entreprise
      degage un EBIT positif -- signal typique d'une distorsion par filiale
      de financement captive, cf. cas Ford -- on retente avec le ratio de
      levier comme estimation de secours plutot que de retourner un rating
      de defaut trompeur.
    Retourne None seulement si AUCUNE des deux methodes n'a de donnees
    exploitables (objectif : ne jamais exclure une opportunite faute de
    methode adaptee)."""
    cik = get_cik_for_ticker(ticker)
    if cik is None:
        print(f"[{ticker}] CIK introuvable.")
        return None

    resp = requests.get(COMPANY_FACTS_URL.format(cik10=cik), headers=SEC_HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"[{ticker}] company facts indisponibles (HTTP {resp.status_code}).")
        return None
    facts = resp.json()

    if is_financial_institution(cik):
        return _score_via_leverage(
            ticker, cik, facts,
            note="Institution financiere (SIC 6xxx) -- methode couverture d'interets non pertinente, ratio de levier utilise directement."
        )

    ebit = _ttm_duration_value(facts, EBIT_TAGS)
    interest_expense = _ttm_duration_value(facts, INTEREST_EXPENSE_TAGS)

    if ebit is not None and interest_expense not in (None, 0):
        coverage = ebit / interest_expense
        rating, spread = rating_for_coverage(coverage)
        score = _quality_score(rating)

        # Detection heuristique de distorsion type "filiale de financement
        # captive" (cf. Ford) : rating catastrophique malgre un EBIT positif.
        if rating in (SYNTHETIC_RATING_TABLE[0][2], SYNTHETIC_RATING_TABLE[1][2]) and ebit > 0:
            fallback = _score_via_leverage(
                ticker, cik, facts,
                note=f"Rating par couverture d'interets ({rating}) juge peu fiable malgre un EBIT positif "
                     f"-- possible distorsion par filiale de financement captive (cf. cas Ford). "
                     f"Ratio de levier utilise en repli."
            )
            if fallback is not None:
                print(f"[{ticker}] Bascule vers le ratio de levier (distorsion suspectee, cf. note du resultat).")
                return fallback
            # si le repli echoue aussi, on garde quand meme le resultat couverture ci-dessous

        return BondQualityResult(
            ticker=ticker.upper(),
            cik=cik,
            method_used="interest_coverage",
            quality_score=score,
            rating_label=rating,
            ebit=ebit,
            interest_expense=interest_expense,
            interest_coverage=round(coverage, 2),
            default_spread=spread,
        )

    # Methode couverture indisponible (tags manquants) -> repli levier
    print(f"[{ticker}] EBIT ou charges d'interet introuvables -- tentative via ratio de levier.")
    return _score_via_leverage(
        ticker, cik, facts,
        note="Donnees EBIT/charges d'interet indisponibles -- ratio de levier utilise en repli."
    )


if __name__ == "__main__":
    # Test rapide sur quelques tickers connus -- a lancer en local.
    # Compare le rating obtenu a un rating reel trouve en ligne pour valider.
    for tk in ["AAPL", "F", "BYRN"]:
        result = score_bond_quality(tk)
        print(result)
