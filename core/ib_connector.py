"""
Pont Interactive Brokers -- connexion et lecture de compte
================================================================

Premiere brique du pont IB : connexion a TWS (paper trading) et lecture
SEULEMENT (positions, solde de cash) -- rien qui place un ordre. Objectif :
valider que la connexion fonctionne avant de toucher a l'execution reelle.

Prerequis (cf. guide de configuration TWS) :
  - TWS ouvert, connecte en paper trading
  - API activee (Enable ActiveX and Socket Clients), port note (7497 par defaut en paper)
  - pip install ib_async

NON TESTE dans ce sandbox (TWS est une appli desktop, ne peut pas tourner
dans ce container) : premier test forcement sur ta machine.
"""

from __future__ import annotations

from dataclasses import dataclass

from ib_async import IB, util

# Ports par defaut IBKR -- ATTENTION a ne pas confondre paper/reel.
PAPER_TRADING_PORT = 7497
LIVE_TRADING_PORT = 7496  # a utiliser SEULEMENT une fois pret, avec confirmation explicite

DEFAULT_HOST = "127.0.0.1"
DEFAULT_CLIENT_ID = 1  # identifiant arbitraire de connexion, change si tu connectes plusieurs clients a la fois


@dataclass
class AccountSnapshot:
    account_id: str
    net_liquidation: float
    cash_balance: float
    positions: list[dict]  # [{"ticker": ..., "quantity": ..., "avg_cost": ..., "market_value": ...}, ...]


def connect(port: int = PAPER_TRADING_PORT, host: str = DEFAULT_HOST, client_id: int = DEFAULT_CLIENT_ID) -> IB:
    """Connexion a TWS. Leve une exception explicite si la connexion echoue
    (TWS pas ouvert, API pas activee, mauvais port...) plutot que de planter
    avec une erreur socket cryptique."""
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
    except Exception as e:
        raise ConnectionError(
            f"Impossible de se connecter a TWS sur {host}:{port}. "
            f"Verifie que TWS est ouvert, connecte, et que l'API est activee "
            f"(File > Global Configuration > API > Settings > Enable ActiveX and Socket Clients). "
            f"Erreur d'origine : {e}"
        ) from e
    return ib


def get_account_snapshot(ib: IB) -> AccountSnapshot:
    """Recupere le solde de cash et les positions actuelles du compte
    connecte. Lecture seule."""
    account_values = ib.accountValues()
    account_id = account_values[0].account if account_values else "?"

    net_liq = next((float(v.value) for v in account_values if v.tag == "NetLiquidation"), 0.0)
    cash = next((float(v.value) for v in account_values if v.tag == "TotalCashValue"), 0.0)

    positions = []
    for pos in ib.positions():
        positions.append({
            "ticker": pos.contract.symbol,
            "quantity": pos.position,
            "avg_cost": pos.avgCost,
            "market_value": pos.position * pos.avgCost,  # approximation -- le vrai market value viendrait du prix actuel, pas du cout moyen
        })

    return AccountSnapshot(
        account_id=account_id, net_liquidation=net_liq, cash_balance=cash, positions=positions,
    )


def print_account_snapshot(port: int = PAPER_TRADING_PORT) -> None:
    """Point d'entree de test : connecte, affiche le compte, deconnecte proprement."""
    print(f"Connexion a TWS sur le port {port}"
          f"{' (PAPER TRADING)' if port == PAPER_TRADING_PORT else ' (COMPTE REEL -- attention)'}...")
    ib = connect(port=port)
    try:
        snapshot = get_account_snapshot(ib)
        print(f"\nCompte : {snapshot.account_id}")
        print(f"Valeur nette liquidative : {snapshot.net_liquidation:,.2f}$")
        print(f"Solde de cash : {snapshot.cash_balance:,.2f}$")
        print(f"\nPositions actuelles ({len(snapshot.positions)}) :")
        if not snapshot.positions:
            print("  (aucune position -- normal sur un compte paper tout juste cree)")
        for p in snapshot.positions:
            print(f"  {p['ticker']:6s} x{p['quantity']:.0f} @ cout moyen {p['avg_cost']:.2f}$")
    finally:
        ib.disconnect()
        print("\nDeconnecte de TWS proprement.")


if __name__ == "__main__":
    print_account_snapshot()