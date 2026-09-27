"""Script de verification temporaire -- affiche le ticker et l'entry_price
de chaque position ouverte, pour voir lesquelles ont un prix d'entree
enregistre (necessaire pour le test de trim)."""

from core.persistence import get_connection

conn = get_connection()
conn.row_factory = None
rows = conn.execute(
    "SELECT ticker, entry_price FROM portfolio_positions WHERE status = 'open'"
).fetchall()
conn.close()

for ticker, entry_price in rows:
    print(f"{ticker:8s} entry_price={entry_price}")