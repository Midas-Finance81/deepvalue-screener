"""
Sante des positions en baisse -- etape 3 du cycle de planification mensuelle
================================================================================

Pour chaque position ouverte dont le gain est negatif, croise DEUX signaux
independants avant toute decision -- jamais un stop-loss aveugle sur le
seul prix (incompatible avec la logique deep value : une these peut 
legitimement traverser une baisse avant que le catalyseur ne joue) :

  1. Pilier 1 reactualise : y a-t-il eu un NOUVEAU cluster d'achats
     d'inities sur ce titre depuis peu (INSIDER_ACTIVITY_LOOKBACK_DAYS) ?
     Si oui, c'est un signal de renforcement de conviction -- les inities
     eux-memes ne voient pas leur these remise en cause.
  2. Piliers 2/3 reactualises : le score fondamental (bond_quality +
     operational_profile + qualitative_profile, via portfolio_review.py)
     s'est-il degrade de plus de FUNDAMENTALS_DEGRADATION_THRESHOLD points
     par rapport au score d'entree ?

DECISION:
  - Les DEUX signaux negatifs (pas de nouvel achat d'initie ET fondamentaux
    degrades) -> CLOTURE AUTOMATIQUE. Techniquement : on marque la position
    'closed' en base -- le prochain execute_reconciliation() generera
    naturellement le bon ordre de vente complete (meme mecanique deja
    testee et validee avec GLOO plus tot dans le projet), pas besoin de
    dupliquer la logique de vente ici.
  - Sinon (un seul signal negatif, ou aucun) -> ALERTE SEULE, la position
    reste ouverte, decision manuelle.

COUT : reutilise portfolio_review.py (1 appel API Claude par position en
baisse pour le Pilier 3 qualitatif) et insider_clusters.py (scan Form 4 sur
INSIDER_ACTIVITY_LOOKBACK_DAYS jours, mais beneficie du cache si cette
periode a deja ete scannee par un run_screener() recent).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.persistence import get_connection
from core.bond_quality import get_cik_for_ticker
from core.insider_clusters import check_recent_insider_activity
from core.portfolio_review import review_position, PositionReview
from core.config import FUNDAMENTALS_DEGRADATION_THRESHOLD, INSIDER_ACTIVITY_LOOKBACK_DAYS
from core.composite_scorer import classify_momentum


@dataclass
class PositionHealthCheck:
    ticker: str
    review: PositionReview
    has_recent_insider_buying: bool | None  # None si CIK introuvable, verification non faite
    fundamentals_degraded: bool
    decision: str  # "CLOTURE_AUTO", "ALERTE", ou "OK" (n'aurait pas du etre appelee si gain >= 0)
    momentum_regime: str | None = None  # PUREMENT INFORMATIF -- n'influence jamais la decision
                                          # (decision volontairement basee sur Pilier 1 + Piliers 2/3
                                          # uniquement, pas sur le prix -- cf. rejet du stop-loss pur)
    notes: list[str] = field(default_factory=list)


def _get_declining_positions() -> list[dict]:
    conn = get_connection()
    conn.row_factory = None
    rows = conn.execute(
        "SELECT ticker, entry_price, entry_weight, entry_score FROM portfolio_positions WHERE status = 'open'"
    ).fetchall()
    conn.close()
    return [{"ticker": r[0], "entry_price": r[1], "entry_weight": r[2], "entry_score": r[3]} for r in rows]


def check_declining_positions(include_qualitative: bool = True, lookback_days: int = INSIDER_ACTIVITY_LOOKBACK_DAYS) -> list[PositionHealthCheck]:
    """Pipeline complet de l'etape 3 : identifie les positions en baisse,
    croise Pilier 1 + Piliers 2/3, decide. NE FERME AUCUNE POSITION ICI --
    retourne juste les checks. Utilise apply_health_decisions() pour
    appliquer reellement les clotures automatiques en base."""
    positions = _get_declining_positions()
    checks = []

    # D'abord, calcule le score actualise (Piliers 2/3) pour TOUTES les
    # positions -- necessaire pour savoir lesquelles sont vraiment en baisse
    # (gain_pct depend du prix actuel, calcule dans review_position).
    reviews = []
    for pos in positions:
        print(f"Analyse fondamentaux {pos['ticker']}...")
        review = review_position(pos["ticker"], pos["entry_price"], pos["entry_score"], include_qualitative)
        reviews.append(review)

    declining = [r for r in reviews if r.gain_pct is not None and r.gain_pct < 0]
    if not declining:
        return []

    # Verification groupee du Pilier 1 (un seul scan Form 4 pour tous les
    # tickers en baisse, plutot qu'un par un -- plus efficace).
    cik_by_ticker = {r.ticker: get_cik_for_ticker(r.ticker) for r in declining}
    valid_ciks = {cik for cik in cik_by_ticker.values() if cik is not None}
    print(f"\nVerification Pilier 1 reactualise sur {len(valid_ciks)} titre(s) en baisse "
          f"(fenetre {lookback_days} jours)...")
    insider_activity = check_recent_insider_activity(valid_ciks, lookback_days) if valid_ciks else {}

    for r in declining:
        notes = []
        cik = cik_by_ticker.get(r.ticker)
        has_insider_buying = insider_activity.get(cik) if cik else None
        if cik is None:
            notes.append("CIK introuvable -- Pilier 1 non verifie.")

        degraded = False
        if r.entry_score is not None and r.updated_score is not None:
            score_change = r.updated_score - r.entry_score
            degraded = score_change <= FUNDAMENTALS_DEGRADATION_THRESHOLD
        else:
            notes.append("Score d'entree ou actualise indisponible -- degradation non evaluable.")

        if has_insider_buying is False and degraded:
            decision = "CLOTURE_AUTO"
        elif has_insider_buying or degraded:
            decision = "ALERTE"
        else:
            decision = "ALERTE"  # signaux insuffisants pour trancher -> prudence, alerte

        # Momentum : purement informatif, n'influence jamais "decision" ci-dessus.
        _, _, momentum_regime, _ = classify_momentum(r.ticker)

        checks.append(PositionHealthCheck(
            ticker=r.ticker, review=r, has_recent_insider_buying=has_insider_buying,
            fundamentals_degraded=degraded, decision=decision, momentum_regime=momentum_regime, notes=notes,
        ))

    return checks


def apply_health_decisions(checks: list[PositionHealthCheck]) -> list[str]:
    """Applique les clotures automatiques (status='closed' en base) pour
    les checks marques CLOTURE_AUTO. Le prochain execute_reconciliation()
    generera le bon ordre de vente complete -- meme mecanique deja testee
    avec GLOO. Retourne la liste des tickers clotures."""
    to_close = [c.ticker for c in checks if c.decision == "CLOTURE_AUTO"]
    if not to_close:
        return []

    conn = get_connection()
    for ticker in to_close:
        conn.execute(
            "UPDATE portfolio_positions SET status = 'closed' WHERE ticker = ? AND status = 'open'",
            (ticker,),
        )
    conn.commit()
    conn.close()
    return to_close


def print_health_report(include_qualitative: bool = True, apply_decisions: bool = False, lookback_days: int = INSIDER_ACTIVITY_LOOKBACK_DAYS) -> None:
    checks = check_declining_positions(include_qualitative, lookback_days)
    print(f"\n=== SANTE DES POSITIONS EN BAISSE ({len(checks)} position(s)) ===")
    if not checks:
        print("Aucune position en baisse actuellement.")
        return

    for c in checks:
        insider_str = "?" if c.has_recent_insider_buying is None else ("OUI" if c.has_recent_insider_buying else "NON")
        degrade_str = "OUI" if c.fundamentals_degraded else "NON"
        momentum_str = c.momentum_regime or "?"
        print(f"  {c.ticker:6s} gain={c.review.gain_pct:+.1%}  achats initie recents={insider_str:3s}  "
              f"fondamentaux degrades={degrade_str:3s}  -> {c.decision}  "
              f"(momentum, informatif : {momentum_str})")
        for n in c.notes:
            print(f"           {n}")

    auto_closures = [c.ticker for c in checks if c.decision == "CLOTURE_AUTO"]
    alerts = [c.ticker for c in checks if c.decision == "ALERTE"]
    if auto_closures:
        print(f"\n{len(auto_closures)} position(s) marquee(s) pour cloture automatique : {', '.join(auto_closures)}")
    if alerts:
        print(f"{len(alerts)} position(s) en alerte (decision manuelle) : {', '.join(alerts)}")

    if apply_decisions and auto_closures:
        closed = apply_health_decisions(checks)
        print(f"\nClotures appliquees en base : {', '.join(closed)}. "
              f"Lance execute_reconciliation() pour generer les ordres de vente correspondants.")
    elif auto_closures:
        print("\napply_decisions=False -- rien applique en base. "
              "Relance avec apply_decisions=True pour cloturer reellement ces positions.")


if __name__ == "__main__":
    print_health_report(apply_decisions=False)
