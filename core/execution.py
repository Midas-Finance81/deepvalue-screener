"""
Execution -- calcul des ordres a partir du portefeuille cible
================================================================

Brique 2 de l'architecture a 3 couches (persistance -> execution -> pont IB) :
prend le portefeuille cible stocke dans portfolio.db (positions "open") et
calcule les QUANTITES D'ACTIONS a acheter/vendre/ajuster.

generate_order_sheet() : calcul simple sur un capital donne manuellement,
sans connexion IB -- utile pour une premiere estimation ou en secours.

reconcile_portfolio() : version complete, connecte a TWS (paper trading),
utilise la VRAIE valeur nette liquidative du compte comme capital, compare
au portefeuille cible ET aux positions REELLEMENT detenues -> calcule le
delta exact a executer (achat si on doit augmenter, vente si on doit
reduire ou sortir completement). C'est cette fonction qui sera utilisee
une fois pret a passer de vrais ordres (Read-Only API desactive).

NON TESTE en conditions reelles avec une vraie base portfolio.db remplie
par un run complet -- valide sur ta machine apres un run_screener().
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import yfinance as yf

from core.persistence import get_connection
from core.config import GAIN_THRESHOLD_FOR_TRIM
from core.ib_connector import connect, get_account_snapshot, PAPER_TRADING_PORT, LIVE_TRADING_PORT
from ib_async import Stock, MarketOrder, Trade


@dataclass
class OrderLine:
    ticker: str
    action: str  # "BUY" ou "SELL" (pas d'ordres de vente pour l'instant -- cf. note plus bas)
    target_weight: float
    current_price: float | None
    target_value: float
    target_shares: int | None
    notes: str = ""


@dataclass
class TrimCandidate:
    ticker: str
    entry_price: float
    current_price: float
    gain_pct: float
    target_weight: float


def check_gain_thresholds() -> list[TrimCandidate]:
    """Detecte les positions ouvertes dont le gain depuis le prix d'entree
    depasse GAIN_THRESHOLD_FOR_TRIM (config.py) -- candidates a un trim
    partiel (retour au poids cible normal, PAS une sortie complete, decision
    validee avec Thomas).

    LIMITE ACTUELLE : ne fait que DETECTER et RAPPORTER -- ne calcule pas
    encore la quantite exacte a vendre pour le trim, car ca necessite de
    connaitre la quantite REELLEMENT detenue sur le compte IBKR (pas encore
    disponible, compte en cours de validation). A completer une fois le
    pont IB branche : la quantite a vendre sera (quantite detenue actuelle)
    - (quantite correspondant au poids cible sur la valeur totale reelle
    du portefeuille)."""
    positions = get_open_positions()
    trim_candidates = []

    for pos in positions:
        entry_price = pos["entry_price"]
        if not entry_price:
            continue  # position ouverte avant l'ajout du suivi de prix d'entree, ou prix indisponible a l'epoque

        ticker = pos["ticker"]
        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            continue
        current_price = float(hist["Close"].iloc[-1])
        gain_pct = (current_price - entry_price) / entry_price

        if gain_pct >= GAIN_THRESHOLD_FOR_TRIM:
            trim_candidates.append(TrimCandidate(
                ticker=ticker, entry_price=entry_price, current_price=current_price,
                gain_pct=round(gain_pct, 4), target_weight=pos["entry_weight"] or 0.0,
            ))

    return trim_candidates


def print_trim_report() -> None:
    candidates = check_gain_thresholds()
    print(f"\n=== POSITIONS AU-DELA DU SEUIL DE GAIN ({GAIN_THRESHOLD_FOR_TRIM:.0%}) ===")
    if not candidates:
        print("Aucune position ne depasse le seuil actuellement.")
        return
    for c in candidates:
        print(f"  {c.ticker:6s} : +{c.gain_pct:.1%} (entree {c.entry_price:.2f}$ -> actuel {c.current_price:.2f}$) "
              f"-- a ramener a son poids cible ({c.target_weight:.2%})")
    print("NOTE : detection uniquement -- la quantite exacte a vendre sera calculable "
          "une fois le pont IB branche (necessite la position reelle detenue).")


def get_open_positions() -> list[sqlite3.Row]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM portfolio_positions WHERE status = 'open'").fetchall()
    conn.close()
    return rows


def generate_order_sheet(total_capital: float) -> list[OrderLine]:
    """Calcule, pour chaque position ouverte en base, le nombre d'actions
    a acheter pour respecter son poids cible sur un capital total donne.

    LIMITE IMPORTANTE : ne gere que les ACHATS pour l'instant (positions
    "open" en base = ce qu'on veut detenir). Les positions "closed" (sorties
    du portefeuille cible, cf. rebalancing_log) ne sont pas encore traduites
    en ordres de VENTE ici -- ca necessite de connaitre la quantite deja
    detenue reellement (donnee du compte IB, indisponible tant que le
    compte n'est pas actif). A completer une fois le pont IB branche."""
    positions = get_open_positions()
    order_sheet = []

    for pos in positions:
        ticker = pos["ticker"]
        target_weight = pos["entry_weight"] or 0.0
        target_value = target_weight * total_capital

        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            order_sheet.append(OrderLine(
                ticker=ticker, action="BUY", target_weight=target_weight,
                current_price=None, target_value=target_value, target_shares=None,
                notes="Prix indisponible -- verifier le ticker manuellement.",
            ))
            continue

        current_price = float(hist["Close"].iloc[-1])
        target_shares = int(target_value // current_price)  # arrondi au nombre entier d'actions en dessous

        order_sheet.append(OrderLine(
            ticker=ticker, action="BUY", target_weight=target_weight,
            current_price=round(current_price, 2), target_value=round(target_value, 2),
            target_shares=target_shares,
        ))

    return order_sheet


@dataclass
class ReconciledOrder:
    ticker: str
    action: str  # "BUY", "SELL", ou "HOLD" (deja au bon niveau, rien a faire)
    current_shares: int
    target_shares: int
    delta_shares: int
    current_price: float | None
    estimated_value: float | None
    target_dollar_amount: float | None = None  # utilise pour les ordres fractionnes (cashQty)
    notes: str = ""


def reconcile_portfolio(
    total_capital: float | None = None, port: int = PAPER_TRADING_PORT,
) -> list[ReconciledOrder]:
    """Version complete : connecte a TWS, utilise la VRAIE valeur nette
    liquidative du compte (sauf si total_capital est fourni explicitement),
    compare le portefeuille cible (portfolio.db, positions 'open') aux
    positions REELLEMENT detenues sur le compte -> calcule le delta exact
    (achat/vente/rien a faire) pour chaque ticker concerne, y compris les
    sorties completes (ticker detenu mais plus dans le portefeuille cible)."""
    ib = connect(port=port)
    try:
        snapshot = get_account_snapshot(ib)
    finally:
        ib.disconnect()

    capital = total_capital if total_capital is not None else snapshot.cash_balance
    current_holdings = {p["ticker"]: int(p["quantity"]) for p in snapshot.positions}

    target_positions = get_open_positions()
    target_tickers = {pos["ticker"] for pos in target_positions}

    results = []

    # Positions cibles (achat ou ajustement)
    for pos in target_positions:
        ticker = pos["ticker"]
        target_weight = pos["entry_weight"] or 0.0
        target_value = target_weight * capital

        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            results.append(ReconciledOrder(
                ticker=ticker, action="?", current_shares=current_holdings.get(ticker, 0),
                target_shares=0, delta_shares=0, current_price=None, estimated_value=None,
                notes="Prix indisponible -- verifier manuellement.",
            ))
            continue

        current_price = float(hist["Close"].iloc[-1])
        target_shares = int(target_value // current_price)
        current_shares = current_holdings.get(ticker, 0)
        delta = target_shares - current_shares

        notes = ""
        if target_shares == 0 and current_shares == 0:
            notes = (f"Budget alloue ({target_value:,.0f}$) insuffisant pour 1 action "
                     f"a {current_price:.2f}$ -- position ignoree, pas juste 'a niveau'.")

        action = "BUY" if delta > 0 else ("SELL" if delta < 0 else "HOLD")
        results.append(ReconciledOrder(
            ticker=ticker, action=action, current_shares=current_shares,
            target_shares=target_shares, delta_shares=delta,
            current_price=round(current_price, 2), estimated_value=round(abs(delta) * current_price, 2),
            target_dollar_amount=round(target_value, 2),
            notes=notes,
        ))

    # Positions detenues mais plus dans le portefeuille cible -> sortie complete
    for ticker, quantity in current_holdings.items():
        if ticker not in target_tickers and quantity != 0:
            hist = yf.Ticker(ticker).history(period="1d")
            current_price = float(hist["Close"].iloc[-1]) if not hist.empty else None
            results.append(ReconciledOrder(
                ticker=ticker, action="SELL", current_shares=quantity, target_shares=0,
                delta_shares=-quantity, current_price=current_price,
                estimated_value=round(quantity * current_price, 2) if current_price else None,
                notes="Sortie complete -- plus dans le portefeuille cible.",
            ))

    return results


def print_reconciliation(total_capital: float | None = None, port: int = PAPER_TRADING_PORT) -> None:
    orders = reconcile_portfolio(total_capital=total_capital, port=port)
    print(f"\n=== RECONCILIATION PORTEFEUILLE CIBLE vs COMPTE IB ({'PAPER' if port == PAPER_TRADING_PORT else 'REEL'}) ===")
    if not orders:
        print("Rien a faire -- le compte correspond deja au portefeuille cible (ou aucune position cible).")
        return

    skipped_budget = [o for o in orders if o.action == "HOLD" and "Budget alloue" in o.notes]
    for o in orders:
        if o.action == "HOLD" and o in skipped_budget:
            print(f"  IGNORE  {o.ticker:6s} -- {o.notes}")
        elif o.action == "HOLD":
            print(f"  RIEN A FAIRE {o.ticker:6s} (deja {o.current_shares} actions, cible identique)")
        elif o.action == "?":
            print(f"  ?       {o.ticker:6s} -- {o.notes}")
        else:
            print(f"  {o.action:4s} {o.ticker:6s} {abs(o.delta_shares):4d} actions "
                  f"(detenu {o.current_shares} -> cible {o.target_shares}) "
                  f"@ ~{o.current_price:.2f}$ (~{o.estimated_value:,.0f}$) {o.notes}")

    if skipped_budget:
        print(f"\n{len(skipped_budget)} position(s) ignoree(s) faute de budget suffisant pour 1 action -- "
              f"augmente le capital, ou passe l'ordre fractionne A LA MAIN depuis TWS "
              f"(non envoyable via l'API, cf. execute_reconciliation()).")
    print("\nNOTE : aucun ordre n'a ete envoye -- ceci est un calcul de reconciliation uniquement.")


def print_order_sheet(total_capital: float) -> None:
    orders = generate_order_sheet(total_capital)
    total_deployed = sum(o.target_shares * o.current_price for o in orders if o.target_shares and o.current_price)

    print(f"\n=== FEUILLE D'ORDRES (capital total : {total_capital:,.0f}$) ===")
    for o in orders:
        if o.target_shares is not None:
            print(f"  {o.action} {o.ticker:6s} x{o.target_shares:4d} actions "
                  f"@ ~{o.current_price:.2f}$ (poids cible {o.target_weight:.2%}, "
                  f"valeur ~{o.target_shares * o.current_price:,.0f}$)")
        else:
            print(f"  {o.action} {o.ticker:6s} -- {o.notes}")
    print(f"\nCapital deploye estime : {total_deployed:,.0f}$ / {total_capital:,.0f}$ "
          f"({total_deployed / total_capital:.1%})")
    print("NOTE : ordres d'achat uniquement pour l'instant -- les sorties de portefeuille "
          "(positions fermees) ne sont pas encore traduites en ordres de vente ici.")


def execute_reconciliation(
    total_capital: float | None = None, port: int = PAPER_TRADING_PORT,
    order_type: str = "MKT", dry_run: bool = True,
) -> list[Trade]:
    """ENVOIE DE VRAIS ORDRES (sauf si dry_run=True, defaut). Calcule la
    reconciliation comme reconcile_portfolio(), puis place un ordre marche
    pour chaque BUY/SELL, en nombre d'actions ENTIER.

    LIMITATION CONFIRMEE EN CONDITIONS REELLES: les actions fractionnees / ordres en
    montant (cashQty) NE SONT PAS SUPPORTES par l'API TWS pour les actions
    -- confirme directement par IBKR ("Cash Quantity orders are only
    supported for forex orders via TWS API"), et l'Error 10243 confirme
    aussi que les ordres fractionnes classiques sont rejetes via l'API
    ("Please use desktop version to place this order"). Ce n'est PAS un
    probleme de permission compte ou de code -- une limitation dure de la
    plateforme, pas de contournement programmatique possible. La fonction
    use_fractional a ete retiree suite a ce test reel plutot que laissee
    en place a echouer silencieusement.

    Pour les positions dont le budget est trop petit pour 1 action entiere
    (signalees 'IGNORE' par reconcile_portfolio/print_reconciliation),
    deux options restent : augmenter le capital alloue, ou passer l'ordre
    fractionne A LA MAIN depuis l'interface TWS (possible cote GUI, pas cote API).

    GARDE-FOUS DE SECURITE :
      - dry_run=True par defaut : affiche ce qui SERAIT envoye, n'envoie rien.
        Il faut explicitement passer dry_run=False pour reellement agir.
      - Sur le port REEL (LIVE_TRADING_PORT), une confirmation manuelle
        (taper le mot exact "CONFIRMER") est exigee avant tout envoi, meme
        avec dry_run=False -- aucun bypass programmatique possible.
      - N'envoie AUCUN ordre pour les lignes avec current_price manquant
        (donnee prix indisponible -- on ne devine jamais un prix).

    Necessite Read-Only API DESACTIVE dans TWS (File > Global Configuration
    > API > Settings), sinon les ordres seront rejetes par TWS lui-meme."""
    orders = reconcile_portfolio(total_capital=total_capital, port=port)
    actionable = [o for o in orders if o.action in ("BUY", "SELL") and o.current_price is not None]
    ignored = [o for o in orders if o.action == "HOLD" and o.current_shares == 0 and o.target_shares == 0]

    is_live = port == LIVE_TRADING_PORT
    mode_label = "SIMULATION (dry_run)" if dry_run else "ENVOI D'ORDRES"
    print(f"\n=== {mode_label} -- {'COMPTE REEL' if is_live else 'PAPER TRADING'} ===")
    for o in actionable:
        print(f"  {o.action} {o.ticker:6s} x{abs(o.delta_shares)} actions (~{o.estimated_value:,.0f}$)")
    if ignored:
        print(f"\n{len(ignored)} position(s) toujours ignoree(s) (budget < prix d'1 action) -- "
              f"les actions fractionnees ne sont PAS envoyables via l'API TWS, cf. docstring. "
              f"A passer a la main depuis TWS si tu veux les representer malgre tout : "
              + ", ".join(o.ticker for o in ignored))

    if not actionable:
        print("Rien a executer via l'API.")
        return []

    if dry_run:
        print("\ndry_run=True -- aucun ordre envoye. Relance avec dry_run=False pour executer reellement.")
        return []

    if is_live:
        print("\n!!! COMPTE REEL -- de l'argent reel va etre engage. !!!")
        confirmation = input("Tape exactement CONFIRMER pour continuer, autre chose pour annuler : ")
        if confirmation != "CONFIRMER":
            print("Annule -- aucun ordre envoye.")
            return []

    ib = connect(port=port)
    trades = []
    try:
        for o in actionable:
            contract = Stock(o.ticker, "SMART", "USD")
            ib.qualifyContracts(contract)
            order = MarketOrder(o.action, abs(o.delta_shares))
            order.tif = "DAY"  # bug connu ib_async/TWS (Error 10349) : TIF vide = annulation a tort
            if order_type != "MKT":
                print(f"  [ATTENTION] order_type='{order_type}' non gere pour l'instant, ordre marche (MKT) utilise pour {o.ticker}.")
            trade = ib.placeOrder(contract, order)
            trades.append(trade)
            print(f"  -> Ordre envoye : {o.action} {abs(o.delta_shares)} {o.ticker} (id {trade.order.orderId})")
        ib.sleep(2)  # laisse le temps a TWS de renvoyer les premiers statuts

        # Enregistre le prix d'entree en base pour les ACHATS reussis --
        # lacune corrigee suite au test de Thomas (les positions ouvertes via
        # execute_reconciliation() n'avaient jamais leur entry_price rempli,
        # seul un run_screener() complet le faisait via record_run()).
        _record_entry_prices(trades, actionable)
    finally:
        ib.disconnect()

    return trades


def _record_entry_prices(trades: list[Trade], orders: list[ReconciledOrder]) -> None:
    """Pour chaque BUY reussi (au moins partiellement rempli), enregistre le
    prix de remplissage reel dans portfolio_positions.entry_price. Si pas
    encore rempli au moment de l'appel (ordre reste PendingSubmit/Submitted),
    utilise le prix releve au moment du calcul de l'ordre en repli -- mieux
    qu'un entry_price manquant, mais a corriger manuellement plus tard si
    le remplissage reel s'ecarte significativement."""
    orders_by_ticker = {o.ticker: o for o in orders}
    conn = get_connection()
    for trade in trades:
        if trade.order.action != "BUY":
            continue
        ticker = trade.contract.symbol
        fills = trade.fills
        if fills:
            avg_fill_price = sum(f.execution.price * f.execution.shares for f in fills) / sum(f.execution.shares for f in fills)
        else:
            fallback = orders_by_ticker.get(ticker)
            avg_fill_price = fallback.current_price if fallback else None
        if avg_fill_price is None:
            continue
        conn.execute(
            "UPDATE portfolio_positions SET entry_price = ? WHERE ticker = ? AND status = 'open'",
            (avg_fill_price, ticker),
        )
    conn.commit()
    conn.close()


@dataclass
class TrimOrder:
    ticker: str
    current_shares: int
    target_shares: int
    shares_to_sell: int
    current_price: float
    gain_pct: float
    estimated_value: float


def compute_trim_orders(total_capital: float | None = None, port: int = PAPER_TRADING_PORT) -> list[TrimOrder]:
    """Complete check_gain_thresholds() : pour chaque candidat au trim,
    se connecte a IB pour connaitre la quantite REELLEMENT detenue, calcule
    la quantite cible sur le poids normal (pas le poids gonfle par la
    hausse du prix), et deduit la quantite a VENDRE pour revenir au poids
    cible (trim partiel, pas une sortie complete -- decision validee avec
    Thomas). Ne vend rien si la position n'a en fait pas plus d'actions que
    sa cible (ex. le prix a fluctue depuis la detection)."""
    candidates = check_gain_thresholds()
    if not candidates:
        return []

    ib = connect(port=port)
    try:
        snapshot = get_account_snapshot(ib)
    finally:
        ib.disconnect()

    capital = total_capital if total_capital is not None else snapshot.cash_balance
    current_holdings = {p["ticker"]: int(p["quantity"]) for p in snapshot.positions}

    trim_orders = []
    for c in candidates:
        current_shares = current_holdings.get(c.ticker, 0)
        target_value = c.target_weight * capital
        target_shares = int(target_value // c.current_price)
        shares_to_sell = current_shares - target_shares

        if shares_to_sell > 0:
            trim_orders.append(TrimOrder(
                ticker=c.ticker, current_shares=current_shares, target_shares=target_shares,
                shares_to_sell=shares_to_sell, current_price=c.current_price, gain_pct=c.gain_pct,
                estimated_value=round(shares_to_sell * c.current_price, 2),
            ))

    return trim_orders


def execute_trim(
    total_capital: float | None = None, port: int = PAPER_TRADING_PORT, dry_run: bool = True,
) -> list[Trade]:
    """ENVOIE DE VRAIS ORDRES DE VENTE PARTIELLE (sauf si dry_run=True,
    defaut) pour les positions au-dela du seuil de gain (config.py). Trim
    partiel vers le poids cible normal, PAS une sortie complete

    GARDE-FOUS DE SECURITE (identiques a execute_reconciliation()) :
      - dry_run=True par defaut : affiche ce qui SERAIT vendu, n'envoie rien.
      - Sur le port REEL, confirmation manuelle ("CONFIRMER") exigee.
      - order.tif="DAY" fixe explicitement (Error 10349, cf. execute_reconciliation()).

    Necessite Read-Only API DESACTIVE dans TWS."""
    trims = compute_trim_orders(total_capital=total_capital, port=port)

    is_live = port == LIVE_TRADING_PORT
    mode_label = "SIMULATION (dry_run)" if dry_run else "ENVOI D'ORDRES DE TRIM"
    print(f"\n=== {mode_label} -- {'COMPTE REEL' if is_live else 'PAPER TRADING'} ===")
    if not trims:
        print("Aucune position a trimmer actuellement (soit aucun seuil de gain atteint, "
              "soit deja au poids cible ou en dessous).")
        return []

    for t in trims:
        print(f"  SELL {t.ticker:6s} x{t.shares_to_sell} actions (gain +{t.gain_pct:.1%}, "
              f"detenu {t.current_shares} -> cible {t.target_shares}, ~{t.estimated_value:,.0f}$)")

    if dry_run:
        print("\ndry_run=True -- aucun ordre envoye. Relance avec dry_run=False pour executer reellement.")
        return []

    if is_live:
        print("\n!!! COMPTE REEL -- de l'argent reel va etre engage. !!!")
        confirmation = input("Tape exactement CONFIRMER pour continuer, autre chose pour annuler : ")
        if confirmation != "CONFIRMER":
            print("Annule -- aucun ordre envoye.")
            return []

    ib = connect(port=port)
    trades = []
    try:
        for t in trims:
            contract = Stock(t.ticker, "SMART", "USD")
            ib.qualifyContracts(contract)
            order = MarketOrder("SELL", t.shares_to_sell)
            order.tif = "DAY"
            trade = ib.placeOrder(contract, order)
            trades.append(trade)
            print(f"  -> Ordre de trim envoye : SELL {t.shares_to_sell} {t.ticker} (id {trade.order.orderId})")
        ib.sleep(2)
    finally:
        ib.disconnect()

    return trades


if __name__ == "__main__":
    # Exemple : capital de 10 000$ -- ajuste selon ton capital reel
    print_order_sheet(total_capital=10_000)