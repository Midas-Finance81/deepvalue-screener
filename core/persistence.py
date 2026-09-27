"""
Persistance -- historique des runs et suivi des positions
============================================================

Base separee du cache SEC (sec_cache.db, qui ne stocke que les depots bruts).
Celle-ci (portfolio.db) garde trace de :
  1. runs           -- un run du scorer composite = une ligne
  2. scored_candidates -- le score de chaque candidat a chaque run (historique complet)
  3. portfolio_positions -- les positions "ouvertes"/"fermees" au fil du temps
  4. rebalancing_log -- ce qui a change entre deux runs (achat/vente/ajustement)

Objectif : preparer les deux directions futures du programme (execution.py
pour le pont Interactive Brokers, et une eventuelle interface/dashboard) --
aucune des deux ne peut fonctionner sans un historique qui survit a la fin
du script.

NON TESTE dans ce sandbox (pas de dependance externe requise ici en
revanche -- sqlite3 est standard, donc ce module DEVRAIT fonctionner direct,
mais valide quand meme le premier run sur ta machine).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from core.config import REBUY_COOLDOWN_DAYS

if TYPE_CHECKING:
    from core.composite_scorer import ScoredCandidate

PORTFOLIO_DB_PATH = Path("data") / "portfolio.db"  # cree dans data/ a la racine du projet (cwd)


def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    """Ajoute une colonne a une table existante si elle n'y est pas deja --
    idempotent, ne plante pas si la colonne existe deja. Necessaire pour
    les bases portfolio.db creees avant l'ajout de entry_price (ex. celle
    de Thomas, deja peuplee avec des positions sans cette colonne)."""
    existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing_columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()


def get_connection() -> sqlite3.Connection:
    """Connexion SQLite, cree les tables au premier appel si besoin."""
    PORTFOLIO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(PORTFOLIO_DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runs ("
        "  run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  run_date TEXT NOT NULL,"
        "  collection_days INTEGER"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scored_candidates ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  run_id INTEGER NOT NULL REFERENCES runs(run_id),"
        "  ticker TEXT NOT NULL,"
        "  issuer_name TEXT,"
        "  pillar1_score REAL,"
        "  pillar2_score REAL,"
        "  pillar3_score REAL,"
        "  composite_score REAL,"
        "  momentum_regime TEXT,"
        "  momentum_points INTEGER,"
        "  final_score REAL,"
        "  weight REAL,"
        "  in_portfolio INTEGER NOT NULL DEFAULT 0,"
        "  excluded_reason TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS portfolio_positions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ticker TEXT NOT NULL,"
        "  entry_date TEXT NOT NULL,"
        "  entry_weight REAL,"
        "  entry_score REAL,"
        "  entry_price REAL,"
        "  exit_date TEXT,"
        "  status TEXT NOT NULL DEFAULT 'open'"
        ")"
    )
    _migrate_add_column(conn, "portfolio_positions", "entry_price", "REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rebalancing_log ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  event_date TEXT NOT NULL,"
        "  ticker TEXT NOT NULL,"
        "  action TEXT NOT NULL,"  # 'buy', 'sell', 'increase', 'decrease'
        "  old_weight REAL,"
        "  new_weight REAL,"
        "  reason TEXT"
        ")"
    )
    conn.commit()
    return conn


def _save_run(conn: sqlite3.Connection, run_date: date, collection_days: int) -> int:
    cur = conn.execute(
        "INSERT INTO runs (run_date, collection_days) VALUES (?, ?)",
        (run_date.isoformat(), collection_days),
    )
    conn.commit()
    return cur.lastrowid


def _save_scored_candidates(
    conn: sqlite3.Connection, run_id: int,
    all_candidates: list["ScoredCandidate"], portfolio_tickers: set[str],
) -> None:
    rows = [
        (
            run_id, c.ticker, c.issuer_name, c.pillar1_score, c.pillar2_score,
            c.pillar3_score, c.composite_score, c.momentum_regime, c.momentum_points,
            c.final_score, c.weight, 1 if c.ticker in portfolio_tickers else 0,
            c.excluded_reason,
        )
        for c in all_candidates
    ]
    conn.executemany(
        "INSERT INTO scored_candidates "
        "(run_id, ticker, issuer_name, pillar1_score, pillar2_score, pillar3_score, "
        " composite_score, momentum_regime, momentum_points, final_score, weight, "
        " in_portfolio, excluded_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def get_current_positions(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Positions actuellement 'open', indexees par ticker."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM portfolio_positions WHERE status = 'open'").fetchall()
    return {row["ticker"]: row for row in rows}


def sync_portfolio_positions(
    conn: sqlite3.Connection, portfolio: list["ScoredCandidate"], run_date: date,
) -> None:
    """Compare le portefeuille cible de ce run aux positions actuellement
    ouvertes en base, et met a jour : ouvre les nouvelles positions, ferme
    celles qui sortent du portefeuille, journalise les changements de poids
    pour celles qui restent. Ne place AUCUN ordre reel -- c'est juste le
    suivi en base, la brique d'execution (pont Interactive Brokers) viendra
    lire cet etat plus tard.

    COOLDOWN : un ticker cloture il y a moins de REBUY_COOLDOWN_DAYS
    (config.py) n'est PAS rachete, meme s'il ressort dans le portefeuille
    cible -- evite un aller-retour vente/rachat inutile au sein d'un meme
    cycle (cas reel observe : SMMT cloturee par position_health.py, puis
    rachetee au meme cycle car le screening complet utilise une fenetre
    de detection differente)."""
    current = get_current_positions(conn)
    target_tickers = {c.ticker: c for c in portfolio}

    # Nouvelles positions et ajustements de poids
    for ticker, candidate in target_tickers.items():
        if ticker not in current:
            recent_closure = conn.execute(
                "SELECT exit_date FROM portfolio_positions WHERE ticker = ? AND status = 'closed' "
                "ORDER BY exit_date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if recent_closure and recent_closure[0]:
                days_since_closure = (run_date - date.fromisoformat(recent_closure[0])).days
                if days_since_closure < REBUY_COOLDOWN_DAYS:
                    print(f"  [COOLDOWN] {ticker} ignore -- cloture il y a {days_since_closure}j "
                          f"(< {REBUY_COOLDOWN_DAYS}j), pas de rachat immediat.")
                    continue

            conn.execute(
                "INSERT INTO portfolio_positions (ticker, entry_date, entry_weight, entry_score, entry_price, status) "
                "VALUES (?, ?, ?, ?, ?, 'open')",
                (ticker, run_date.isoformat(), candidate.weight, candidate.final_score,
                 getattr(candidate, "current_price", None)),
            )
            conn.execute(
                "INSERT INTO rebalancing_log (event_date, ticker, action, old_weight, new_weight, reason) "
                "VALUES (?, ?, 'buy', NULL, ?, ?)",
                (run_date.isoformat(), ticker, candidate.weight, "Nouvelle entree dans le portefeuille cible"),
            )
        else:
            old_weight = current[ticker]["entry_weight"]
            if old_weight is None or abs(old_weight - candidate.weight) > 1e-6:
                action = "increase" if (old_weight or 0) < candidate.weight else "decrease"
                conn.execute(
                    "UPDATE portfolio_positions SET entry_weight = ? WHERE ticker = ? AND status = 'open'",
                    (candidate.weight, ticker),
                )
                conn.execute(
                    "INSERT INTO rebalancing_log (event_date, ticker, action, old_weight, new_weight, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (run_date.isoformat(), ticker, action, old_weight, candidate.weight, "Rebalancement de poids"),
                )

    # Positions sorties du portefeuille cible -> fermees
    for ticker, row in current.items():
        if ticker not in target_tickers:
            conn.execute(
                "UPDATE portfolio_positions SET status = 'closed', exit_date = ? WHERE ticker = ? AND status = 'open'",
                (run_date.isoformat(), ticker),
            )
            conn.execute(
                "INSERT INTO rebalancing_log (event_date, ticker, action, old_weight, new_weight, reason) "
                "VALUES (?, ?, 'sell', ?, NULL, ?)",
                (run_date.isoformat(), ticker, row["entry_weight"], "Sortie du portefeuille cible"),
            )

    conn.commit()


def record_run(
    all_candidates: list["ScoredCandidate"], portfolio: list["ScoredCandidate"],
    collection_days: int, run_date: date | None = None,
) -> int:
    """Point d'entree principal : appele depuis composite_scorer.py apres
    build_portfolio(). Enregistre le run, tous les scores, et synchronise
    les positions. Retourne le run_id."""
    run_date = run_date or date.today()
    conn = get_connection()
    run_id = _save_run(conn, run_date, collection_days)
    _save_scored_candidates(conn, run_id, all_candidates, {c.ticker for c in portfolio})
    sync_portfolio_positions(conn, portfolio, run_date)
    conn.close()
    return run_id


def print_rebalancing_summary(run_date: date | None = None) -> None:
    """Affiche les changements de portefeuille du dernier run (ou d'une
    date donnee) -- pratique pour voir d'un coup d'oeil quoi acheter/vendre."""
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    if run_date is None:
        row = conn.execute("SELECT MAX(event_date) AS d FROM rebalancing_log").fetchone()
        run_date = date.fromisoformat(row["d"]) if row and row["d"] else None
    if run_date is None:
        print("Aucun historique de rebalancement disponible.")
        conn.close()
        return

    rows = conn.execute(
        "SELECT * FROM rebalancing_log WHERE event_date = ? ORDER BY action, ticker",
        (run_date.isoformat(),),
    ).fetchall()
    conn.close()

    print(f"\n=== REBALANCEMENT DU {run_date} ===")
    if not rows:
        print("Aucun changement.")
        return
    for r in rows:
        if r["action"] == "buy":
            print(f"  ACHAT   {r['ticker']:6s} -> poids cible {r['new_weight']:.2%}")
        elif r["action"] == "sell":
            print(f"  VENTE   {r['ticker']:6s} (ancien poids {r['old_weight']:.2%}) -> sortie complete")
        else:
            print(f"  AJUSTE  {r['ticker']:6s} {r['old_weight']:.2%} -> {r['new_weight']:.2%} ({r['action']})")