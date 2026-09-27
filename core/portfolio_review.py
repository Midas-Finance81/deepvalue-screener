"""
Analyse du portefeuille actuel -- pas un nouveau screening
================================================================

Contrairement a run_screener() (qui part du Pilier 1 pour trouver de
NOUVEAUX candidats), ce module reprend les Piliers 2 et 3 sur les positions
DEJA DETENUES (portfolio_positions, status='open') pour voir si leur these
tient toujours -- qualite de la dette, performance operationnelle, et gain
depuis l'entree par rapport au seuil de trim (config.GAIN_THRESHOLD_FOR_TRIM).

Le Pilier 1 (achats d'inities) n'est PAS recalcule ici : le signal d'achat
initial est un evenement passe, il ne "s'actualise" pas -- ce qui compte
maintenant, c'est si les fondamentaux (Piliers 2/3) restent solides.

COUT : ce module appelle l'API Claude (Pilier 3 qualitatif) pour CHAQUE
position -- 19 positions = 19 appels API. Mets include_qualitative=False
pour l'eviter si tu veux juste un coup d'oeil rapide et gratuit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yfinance as yf

from core.persistence import get_connection
from core.bond_quality import score_bond_quality
from core.operational_profile import score_operational_profile
from core.quality_profile import score_qualitative_profile
from core.composite_scorer import _score_pillar3_quant
from core.config import GAIN_THRESHOLD_FOR_TRIM


@dataclass
class PositionReview:
    ticker: str
    entry_price: float | None
    current_price: float | None
    gain_pct: float | None
    entry_score: float | None
    pillar2_score: float | None
    pillar3_quant_score: float | None
    pillar3_qual_score: float | None
    updated_score: float | None
    over_trim_threshold: bool
    notes: list[str] = field(default_factory=list)


def _get_open_tickers() -> list[dict]:
    conn = get_connection()
    conn.row_factory = None
    rows = conn.execute(
        "SELECT ticker, entry_price, entry_weight, entry_score FROM portfolio_positions WHERE status = 'open'"
    ).fetchall()
    conn.close()
    return [{"ticker": r[0], "entry_price": r[1], "entry_weight": r[2], "entry_score": r[3]} for r in rows]


def review_position(ticker: str, entry_price: float | None, entry_score: float | None, include_qualitative: bool = True) -> PositionReview:
    notes = []

    hist = yf.Ticker(ticker).history(period="1d")
    current_price = float(hist["Close"].iloc[-1]) if not hist.empty else None
    gain_pct = None
    if entry_price and current_price:
        gain_pct = (current_price - entry_price) / entry_price
    elif not entry_price:
        notes.append("Prix d'entree inconnu -- gain non calculable.")

    bond = score_bond_quality(ticker)
    pillar2 = bond.quality_score if bond else None
    if bond is None:
        notes.append("Pilier 2 indisponible.")

    ops = score_operational_profile(ticker)
    pillar3_quant = _score_pillar3_quant(ops) if ops else None
    if ops is None:
        notes.append("Pilier 3 quantitatif indisponible.")

    pillar3_qual = None
    if include_qualitative:
        qual = score_qualitative_profile(ticker)
        pillar3_qual = qual.qualitative_composite if qual else None
        if qual is None:
            notes.append("Pilier 3 qualitatif indisponible.")

    scores = [s for s in (pillar2, pillar3_quant, pillar3_qual) if s is not None]
    updated_score = round(sum(scores) / len(scores), 1) if scores else None

    over_threshold = gain_pct is not None and gain_pct >= GAIN_THRESHOLD_FOR_TRIM

    return PositionReview(
        ticker=ticker, entry_price=entry_price, current_price=current_price, gain_pct=gain_pct,
        entry_score=entry_score, pillar2_score=pillar2, pillar3_quant_score=pillar3_quant,
        pillar3_qual_score=pillar3_qual, updated_score=updated_score,
        over_trim_threshold=over_threshold, notes=notes,
    )


def review_current_portfolio(include_qualitative: bool = True) -> list[PositionReview]:
    positions = _get_open_tickers()
    reviews = []
    for pos in positions:
        print(f"Analyse {pos['ticker']}...")
        review = review_position(pos["ticker"], pos["entry_price"], pos["entry_score"], include_qualitative)
        reviews.append(review)
    return reviews


def print_portfolio_review(include_qualitative: bool = True) -> None:
    reviews = review_current_portfolio(include_qualitative)
    print(f"\n=== ANALYSE DU PORTEFEUILLE ACTUEL ({len(reviews)} positions) ===")
    for r in reviews:
        gain_str = f"{r.gain_pct:+.1%}" if r.gain_pct is not None else "?"
        score_str = f"{r.updated_score}" if r.updated_score is not None else "?"
        entry_str = f"{r.entry_score}" if r.entry_score is not None else "?"
        flag = "  >>> SEUIL DE TRIM ATTEINT" if r.over_trim_threshold else ""
        print(f"  {r.ticker:6s} gain={gain_str:>8s}  score entree={entry_str:>5s} -> actuel={score_str:>5s}{flag}")
        for n in r.notes:
            print(f"           {n}")

    trims = [r for r in reviews if r.over_trim_threshold]
    if trims:
        print(f"\n{len(trims)} position(s) au-dela du seuil de trim ({GAIN_THRESHOLD_FOR_TRIM:.0%}) : "
              + ", ".join(r.ticker for r in trims))


if __name__ == "__main__":
    print_portfolio_review()