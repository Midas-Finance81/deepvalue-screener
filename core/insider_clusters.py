"""
Pilier 1 — Achats groupés d'initiés (Insider Cluster Buys)
============================================================

Source de donnees : SEC EDGAR (gratuit, officiel), pas openinsider.com.
Deux endpoints utilises :
  1. Index quotidien EDGAR (stable depuis 20+ ans) :
     https://www.sec.gov/Archives/edgar/daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx
     -> liste de tous les depots par type de formulaire pour une date donnee.
  2. Le fichier de soumission complet du Form 4 (texte brut contenant le XML
     du document de "ownership") pointe par la colonne "File Name" de l'index.

Regle de detection d'un cluster (definie par Thomas, affinee suite a la
methode de reference partagee) :
  - AU MOINS 3 initiés distincts, PAS de plafond (un cluster de 10+ est un
    signal fort a ne pas exclure, pas un cas limite)
  - Achats en marche ouvert (transaction code == "P")
  - Valeur cumulee >= 200 000 (seuil en USD ici, cf. note de conversion)
  - Sur une fenetre glissante (par defaut 60 jours calendaires)
  - Detection bonus si un dirigeant cle (CEO/CFO/President/Chairman) fait
    partie des acheteurs -- signal juge plus fort que des achats d'initiés
    generiques

IMPORTANT — a valider sur ta machine avant de passer au Pilier 2 :
  Le sandbox de developpement n'a pas acces a sec.gov (domaine non
  autorise sur le reseau de ce container). Ce module n'a donc PAS ete
  teste contre de vraies donnees SEC ici. Avant d'enchainer, lance-le
  sur 2-3 dates recentes et compare 2-3 clusters detectes avec
  openinsider.com/latest-cluster-buys pour t'assurer que le parsing
  XML colle bien au schema reel (le schema Section 16 XML a des
  variantes mineures selon les annees de depot).

CACHE + PARALLELISATION (ajoute pour supporter des runs sur 3 mois sans
attendre des heures) :
  - Cache local SQLite (sec_cache.db, cree a la racine du projet au premier
    lancement) : chaque depot Form 4 deja parse est stocke par numero
    d'accession, et chaque JOURNEE deja entierement traitee est marquee
    comme telle. Un index quotidien SEC pour une date passee ne change
    plus une fois publie -- donc relancer sur une fenetre qui chevauche
    un run precedent saute directement les jours deja en cache, sans
    aucune requete reseau pour eux.
  - Parallelisation : les depots d'une meme journee sont recuperes en
    parallele (ThreadPoolExecutor, 6 workers par defaut) avec un limiteur
    de debit partage entre threads pour rester sous la limite SEC de
    10 requetes/seconde (on vise ~8/s par securite).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import requests
import pandas as pd

SEC_HEADERS = {
    # La SEC exige un User-Agent identifiable (nom + email) sous peine de 403.
    "User-Agent": "Thomas <ton_email@example.com> deep-value-screener/0.1",
}

DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{yyyymmdd}.idx"
FILING_BASE_URL = "https://www.sec.gov/Archives/{file_name}"

# Codes de transaction Form 4 pertinents (Annexe A du formulaire SEC)
CODE_OPEN_MARKET_PURCHASE = "P"

CACHE_DB_PATH = Path("data") / "sec_cache.db"  # cree dans data/ a la racine du projet (cwd)
MAX_REQUESTS_PER_SECOND = 8  # marge de securite sous la limite SEC de 10/s
DEFAULT_WORKERS = 6
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2  # double a chaque tentative (2s, 4s, 8s)


def _get_with_retry(url: str, timeout: int = 30) -> requests.Response | None:
    """requests.get avec retry + backoff exponentiel sur timeout/erreur
    reseau. Sur un run de plusieurs milliers de requetes (fenetre de 3
    mois), un timeout transitoire de la SEC arrive forcement de temps en
    temps -- ca ne doit pas faire planter tout le run. Retourne None si
    toutes les tentatives echouent (l'appelant doit gerer ce cas comme un
    depot manquant, pas une erreur fatale)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return requests.get(url, headers=SEC_HEADERS, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES:
                print(f"  [ERREUR] Echec definitif apres {MAX_RETRIES} tentatives sur {url} : {e}")
                return None
            wait = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            time.sleep(wait)
    return None


@dataclass
class InsiderTransaction:
    issuer_cik: str
    issuer_name: str
    owner_name: str
    is_officer: bool
    is_director: bool
    is_ten_pct_owner: bool
    officer_title: str | None
    transaction_code: str
    transaction_date: date
    shares: float
    price_per_share: float
    shares_owned_following: float | None

    @property
    def value(self) -> float:
        return self.shares * self.price_per_share


def _quarter_for(d: date) -> int:
    return (d.month - 1) // 3 + 1


# --- Cache SQLite -----------------------------------------------------------

def _get_cache_connection() -> sqlite3.Connection:
    """Connexion SQLite (cree le fichier et les tables au premier appel).
    check_same_thread=False : la connexion est partagee entre threads, mais
    chaque acces passe par _cache_lock pour eviter les ecritures concurrentes
    (SQLite ne gere pas bien les ecritures paralleles sans ca)."""
    CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS filing_transactions ("
        "  accession TEXT PRIMARY KEY,"
        "  day TEXT NOT NULL,"
        "  transactions_json TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_days (day TEXT PRIMARY KEY)"
    )
    conn.commit()
    return conn


_cache_lock = threading.Lock()


def _is_day_cached(conn: sqlite3.Connection, day: date) -> bool:
    with _cache_lock:
        row = conn.execute("SELECT 1 FROM processed_days WHERE day = ?", (day.isoformat(),)).fetchone()
    return row is not None


def _load_cached_day(conn: sqlite3.Connection, day: date) -> list[InsiderTransaction]:
    with _cache_lock:
        rows = conn.execute(
            "SELECT transactions_json FROM filing_transactions WHERE day = ?", (day.isoformat(),)
        ).fetchall()
    transactions = []
    for (tx_json,) in rows:
        for tx_dict in json.loads(tx_json):
            tx_dict["transaction_date"] = date.fromisoformat(tx_dict["transaction_date"])
            transactions.append(InsiderTransaction(**tx_dict))
    return transactions


def _save_filing_to_cache(conn: sqlite3.Connection, accession: str, day: date, transactions: list[InsiderTransaction]) -> None:
    payload = json.dumps([
        {**tx.__dict__, "transaction_date": tx.transaction_date.isoformat()}
        for tx in transactions
    ])
    with _cache_lock:
        conn.execute(
            "INSERT OR REPLACE INTO filing_transactions (accession, day, transactions_json) VALUES (?, ?, ?)",
            (accession, day.isoformat(), payload),
        )
        conn.commit()


def _mark_day_cached(conn: sqlite3.Connection, day: date) -> None:
    with _cache_lock:
        conn.execute("INSERT OR REPLACE INTO processed_days (day) VALUES (?)", (day.isoformat(),))
        conn.commit()


# --- Limiteur de debit partage entre threads --------------------------------

class _RateLimiter:
    """Limite le nombre de requetes/seconde a travers TOUS les threads --
    necessaire car chaque worker du ThreadPoolExecutor ferait sinon ses
    propres requetes sans se soucier des autres, depassant vite la limite
    SEC de 10 req/s en parallele."""

    def __init__(self, max_per_second: float):
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.monotonic()


_rate_limiter = _RateLimiter(MAX_REQUESTS_PER_SECOND)


def fetch_daily_form4_filings(day: date) -> list[str]:
    """Retourne la liste des 'File Name' (chemins EDGAR) des depots Form 4
    pour une date donnee. Les week-ends / jours feries renvoient une liste vide."""
    url = DAILY_INDEX_URL.format(
        year=day.year, quarter=_quarter_for(day), yyyymmdd=day.strftime("%Y%m%d")
    )
    _rate_limiter.wait()
    resp = _get_with_retry(url)
    if resp is None or resp.status_code != 200:
        return []

    file_names = []
    started = False
    for line in resp.text.splitlines():
        if line.startswith("Form Type"):
            started = True
            continue
        if not started or not line.strip():
            continue
        if line.startswith("-----"):
            continue
        # Colonnes a largeur fixe : Form Type | Company Name | CIK | Date Filed | File Name
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form_type = parts[0]
        file_name = parts[-1]
        if form_type == "4":
            file_names.append(file_name)
    # L'index quotidien liste une ligne par CIK associe au depot (emetteur +
    # initie), donc un meme Form 4 apparait generalement 2 fois -- mais avec
    # un chemin different (edgar/data/{CIK}/{accession}.txt ou {CIK} varie
    # selon la partie). On deduplique donc sur le numero d'accession (partie
    # finale du chemin), pas sur le chemin complet.
    seen_accessions = set()
    deduped = []
    for fn in file_names:
        accession = fn.rsplit("/", 1)[-1]  # ex: 0001354866-26-000045.txt
        if accession not in seen_accessions:
            seen_accessions.add(accession)
            deduped.append(fn)
    return deduped


def parse_form4_submission(file_name: str) -> list[InsiderTransaction]:
    """Telecharge un depot Form 4 complet et en extrait les transactions
    d'achat en marche ouvert (code 'P')."""
    url = FILING_BASE_URL.format(file_name=file_name)
    _rate_limiter.wait()
    resp = _get_with_retry(url)
    if resp is None or resp.status_code != 200:
        return []
    raw = resp.text

    xml_match = re.search(r"<XML>(.*?)</XML>", raw, re.DOTALL)
    if not xml_match:
        return []
    xml_body = xml_match.group(1)

    def _tag(pattern: str, text: str) -> str | None:
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else None

    issuer_cik = _tag(r"<issuerCik>(.*?)</issuerCik>", xml_body) or ""
    issuer_name = _tag(r"<issuerName>(.*?)</issuerName>", xml_body) or ""
    owner_name = _tag(r"<rptOwnerName>(.*?)</rptOwnerName>", xml_body) or ""
    is_officer = (_tag(r"<isOfficer>(.*?)</isOfficer>", xml_body) or "0") in ("1", "true")
    is_director = (_tag(r"<isDirector>(.*?)</isDirector>", xml_body) or "0") in ("1", "true")
    is_ten_pct = (_tag(r"<isTenPercentOwner>(.*?)</isTenPercentOwner>", xml_body) or "0") in ("1", "true")
    officer_title = _tag(r"<officerTitle>(.*?)</officerTitle>", xml_body)

    transactions = []
    for tx_block in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml_body, re.DOTALL):
        code = _tag(r"<transactionCode>(.*?)</transactionCode>", tx_block)
        if code != CODE_OPEN_MARKET_PURCHASE:
            continue
        tx_date_str = _tag(r"<transactionDate>\s*<value>(.*?)</value>", tx_block)
        shares_str = _tag(r"<transactionShares>\s*<value>(.*?)</value>", tx_block)
        price_str = _tag(r"<transactionPricePerShare>\s*<value>(.*?)</value>", tx_block)
        following_str = _tag(r"<sharesOwnedFollowingTransaction>\s*<value>(.*?)</value>", tx_block)
        if not (tx_date_str and shares_str and price_str):
            continue
        date_match = re.match(r"\d{4}-\d{2}-\d{2}", tx_date_str)
        if not date_match:
            continue  # date manifestement corrompue, on ignore cette transaction plutot que de planter
        transactions.append(
            InsiderTransaction(
                issuer_cik=issuer_cik,
                issuer_name=issuer_name,
                owner_name=owner_name,
                is_officer=is_officer,
                is_director=is_director,
                is_ten_pct_owner=is_ten_pct,
                officer_title=officer_title,
                transaction_code=code,
                transaction_date=date.fromisoformat(date_match.group()),
                shares=float(shares_str),
                price_per_share=float(price_str),
                shares_owned_following=float(following_str) if following_str else None,
            )
        )
    return transactions


def _process_one_filing(file_name: str, day: date, conn: sqlite3.Connection) -> tuple[str, list[InsiderTransaction]]:
    """Traite UN depot (avec cache) -- fonction executee par les workers
    du ThreadPoolExecutor. Retourne (accession, transactions)."""
    accession = file_name.rsplit("/", 1)[-1]
    with _cache_lock:
        cached = conn.execute(
            "SELECT transactions_json FROM filing_transactions WHERE accession = ?", (accession,)
        ).fetchone()
    if cached is not None:
        transactions = []
        for tx_dict in json.loads(cached[0]):
            tx_dict["transaction_date"] = date.fromisoformat(tx_dict["transaction_date"])
            transactions.append(InsiderTransaction(**tx_dict))
        return accession, transactions

    try:
        transactions = parse_form4_submission(file_name)
    except Exception as e:
        # Isole l'erreur a CE depot -- une exception imprevue sur un seul
        # fichier ne doit pas faire planter tout le run via future.result().
        print(f"  [ERREUR] Depot {accession} ignore (exception : {e})")
        return accession, []
    _save_filing_to_cache(conn, accession, day, transactions)
    return accession, transactions


def collect_transactions(
    start: date, end: date, pause_seconds: float = 0.15, workers: int = DEFAULT_WORKERS,
) -> pd.DataFrame:
    """Parcourt les index quotidiens entre start et end (inclus) et retourne
    toutes les transactions d'achat en marche ouvert sous forme de DataFrame.

    CACHE : une journee deja entierement traitee lors d'un run precedent
    (marquee dans processed_days) est chargee directement depuis sec_cache.db,
    sans aucune requete reseau. Un index quotidien SEC pour une date passee
    ne change plus une fois publie, donc c'est sans risque.

    PARALLELISATION : les depots d'une meme journee (non deja en cache
    individuellement) sont recuperes en parallele via ThreadPoolExecutor,
    avec un limiteur de debit partage entre threads (~8 req/s, sous la
    limite SEC de 10/s). pause_seconds n'est plus utilise en mode parallele
    (garde pour compatibilite de signature) -- le rythme est regule par
    _rate_limiter.

    Utilise sur une fenetre de 3 mois (~90 jours), le premier run reste
    long (des milliers de depots a recuperer), mais tout run ulterieur qui
    chevauche cette fenetre saute instantanement les jours deja en cache."""
    conn = _get_cache_connection()
    rows = []
    seen_accessions: set[str] = set()  # filet de securite anti-doublon global
    day = start
    while day <= end:
        if day.weekday() < 5:  # lundi=0 ... vendredi=4
            if _is_day_cached(conn, day):
                cached_transactions = _load_cached_day(conn, day)
                print(f"[{day}] (cache) {len(cached_transactions)} transactions chargees sans requete reseau.")
                for tx in cached_transactions:
                    accession_key = f"{tx.issuer_cik}-{tx.owner_name}-{tx.transaction_date}-{tx.shares}"
                    if accession_key in seen_accessions:
                        continue
                    seen_accessions.add(accession_key)
                    rows.append(tx.__dict__ | {"value": tx.value})
            else:
                filings = fetch_daily_form4_filings(day)
                print(f"[{day}] {len(filings)} depots Form 4 a traiter ({workers} workers en parallele)...")
                completed = 0
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(_process_one_filing, file_name, day, conn): file_name
                        for file_name in filings
                    }
                    for future in as_completed(futures):
                        accession, transactions = future.result()
                        if accession not in seen_accessions:
                            seen_accessions.add(accession)
                            for tx in transactions:
                                rows.append(tx.__dict__ | {"value": tx.value})
                        completed += 1
                        if completed % 50 == 0:
                            print(f"  ... {completed}/{len(filings)} depots traites, {len(rows)} transactions trouvees")
                _mark_day_cached(conn, day)
        day += timedelta(days=1)
    conn.close()
    return pd.DataFrame(rows)


def _format_usd(value: float) -> str:
    """Formate un montant en style compact (200K$, 1.3M$)."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M$"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K$"
    return f"{value:.0f}$"


PROMINENT_OFFICER_KEYWORDS = [
    "chief executive", "ceo",
    "chief financial", "cfo",
    "chairman", "president",
]


def _has_prominent_officer(titles: pd.Series) -> bool:
    """True si au moins un des achats du cluster vient d'un dirigeant cle
    (CEO/CFO/President/Chairman) -- signal juge particulierement fort
    (methode de la video partagee par Thomas)."""
    for title in titles.dropna():
        title_lower = str(title).lower()
        if any(keyword in title_lower for keyword in PROMINENT_OFFICER_KEYWORDS):
            return True
    return False


def detect_clusters(
    transactions: pd.DataFrame,
    window_days: int = 60,
    min_insiders: int = 3,
    max_insiders: int | None = None,
    min_total_value: float = 200_000,
) -> pd.DataFrame:
    """Applique la regle de cluster de Thomas sur un DataFrame de transactions
    (colonnes attendues : issuer_cik, issuer_name, owner_name, transaction_date,
    value, officer_title). Fenetre glissante = window_days, evaluee a la date
    de la transaction la plus recente par issuer. total_value = somme des
    transactions de TOUS les insiders du cluster (pas la valeur d'un insider
    individuel).

    max_insiders=None (par defaut) : PAS de plafond -- un cluster de 10+
    initiés est un signal fort selon la methode de reference (video
    partagee par Thomas), pas un cas a exclure. Un plafond a 5 excluait a
    tort ces clusters exceptionnels."""
    if transactions.empty:
        return pd.DataFrame(
            columns=["issuer_cik", "issuer_name", "n_insiders", "total_value", "total_value_fmt",
                     "has_prominent_officer", "window_end"]
        )

    df = transactions.copy()
    df["transaction_date"] = pd.to_datetime(df["transaction_date"])
    results = []

    for cik, group in df.groupby("issuer_cik"):
        group = group.sort_values("transaction_date")
        latest_date = group["transaction_date"].max()
        window_start = latest_date - pd.Timedelta(days=window_days)
        windowed = group[group["transaction_date"] >= window_start]

        n_insiders = windowed["owner_name"].nunique()
        total_value = windowed["value"].sum()
        total_shares = windowed["shares"].sum()
        avg_purchase_price = total_value / total_shares if total_shares else None

        # Actions detenues par le cluster APRES leurs achats : pour chaque
        # initie, on prend sa derniere transaction dans la fenetre (colonne
        # "shares_owned_following", deja cumulative -- PAS une somme de
        # toutes ses transactions, qui compterait le meme stock plusieurs
        # fois). LIMITE connue : ne capture que les inities ayant transige
        # dans la fenetre, pas l'ensemble des inities de l'entreprise -- une
        # sous-estimation de l'ownership total, mais la meilleure approximation
        # disponible sans parser les proxy statements (DEF 14A).
        latest_per_owner = windowed.sort_values("transaction_date").groupby("owner_name").tail(1)
        cluster_shares_held = latest_per_owner["shares_owned_following"].sum(skipna=True)

        meets_max = max_insiders is None or n_insiders <= max_insiders
        if min_insiders <= n_insiders and meets_max and total_value >= min_total_value:
            results.append(
                {
                    "issuer_cik": cik,
                    "issuer_name": windowed["issuer_name"].iloc[0],
                    "n_insiders": n_insiders,
                    "total_value": total_value,
                    "total_value_fmt": _format_usd(total_value),
                    "avg_purchase_price": round(avg_purchase_price, 2) if avg_purchase_price else None,
                    "has_prominent_officer": _has_prominent_officer(windowed.get("officer_title", pd.Series(dtype=object))),
                    "cluster_shares_held": cluster_shares_held if cluster_shares_held else None,
                    "window_end": latest_date.date(),
                }
            )

    return pd.DataFrame(results).sort_values("total_value", ascending=False).reset_index(drop=True)


def check_recent_insider_activity(ciks: set[str], lookback_days: int = 60) -> dict[str, bool]:
    """Pour un ensemble de CIK donne (typiquement les positions en baisse
    du portefeuille), verifie si un cluster d'achats d'inities (memes
    regles que detect_clusters) a ete detecte dans les `lookback_days`
    derniers jours -- reutilise integralement collect_transactions() (cache,
    parallelisation, retry deja eprouves) plutot que d'interroger un point
    d'acces par emetteur non verifie. Retourne {cik: True/False}.

    Utilise par le cycle de planification mensuelle (etape 3, verification
    croisee Pilier 1 + Piliers 2/3) pour distinguer une these cassee d'une
    opportunite de renforcement sur une position en baisse."""
    end = date.today()
    start = end - timedelta(days=lookback_days)
    txs = collect_transactions(start, end)

    if txs.empty:
        return {cik: False for cik in ciks}

    filtered = txs[txs["issuer_cik"].isin(ciks)]
    clusters = detect_clusters(filtered, window_days=lookback_days) if not filtered.empty else pd.DataFrame()

    ciks_with_activity = set(clusters["issuer_cik"]) if not clusters.empty else set()
    return {cik: cik in ciks_with_activity for cik in ciks}


if __name__ == "__main__":
    # Premier test : 3 jours seulement, pour valider vite le parsing avant
    # de lancer un scan complet de 60 jours (qui peut prendre plusieurs
    # minutes). Une fois valide, remonte start a 60 jours.
    end = date.today()
    start = end - timedelta(days=3)
    txs = collect_transactions(start, end)
    print(f"\n{len(txs)} transactions d'achat en marche ouvert collectees.\n")
    clusters = detect_clusters(txs)
    print(clusters)