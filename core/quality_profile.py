"""
Pilier 3 — Performance operationnelle (brique qualitative)
==============================================================

Complete operational_profile.py (brique quantitative) avec les questions
que les chiffres seuls ne peuvent pas trancher :
  - Cyclicite du secteur, contexte macro passe/present
  - Comportement et credibilite du management (coherence du discours)

Methode : extraction de la section Item 7 (MD&A) du dernier 10-K via
SEC EDGAR, puis analyse par l'API Claude (meme logique que le facteur
AI-NLP du quant-screener existant).

Prerequis : variable d'environnement ANTHROPIC_API_KEY definie, et le
package `anthropic` installe (pip install anthropic).

NON TESTE dans ce sandbox (pas d'acces a sec.gov ni a l'API Anthropic
depuis ce reseau restreint) : a valider en local.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass

import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

from core.bond_quality import SEC_HEADERS, get_cik_for_ticker

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
FILING_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/{primary_doc}"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2  # double a chaque tentative (2s, 4s, 8s)


def _get_with_retry(url: str, timeout: int = 30) -> requests.Response | None:
    """requests.get avec retry + backoff exponentiel. Meme pattern que
    insider_clusters._get_with_retry, duplique ici (pas importe) car ce
    module a son propre domaine (documents SEC, pas les depots Form 4).
    Attrape RequestException au sens large -- pas seulement Timeout/
    ConnectionError -- suite a un ChunkedEncodingError ("Response ended
    prematurely") rencontre en conditions reelles en pleine recuperation
    d'un gros 10-K (WILSON BANK), qui a fait planter tout run_screener()
    sans ce filet."""
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


CLAUDE_MODEL = "claude-sonnet-5"

ANALYSIS_PROMPT = """Tu analyses la section MD&A (Item 7) du dernier 10-K de {ticker} \
pour un screener actions "deep value". Reponds UNIQUEMENT en JSON valide, sans texte autour, \
avec ce schema exact. IMPORTANT : chaque *_rationale doit tenir en UNE SEULE phrase courte \
(20 mots maximum) -- sois concis, le JSON doit rester compact :

{{
  "cyclicity_score": <0-100, 0=tres cyclique/imprevisible, 100=tres stable/previsible>,
  "cyclicity_rationale": "<1 phrase courte, max 20 mots, en francais>",
  "management_credibility_score": <0-100, 0=discours evasif/incoherent, 100=direct et coherent>,
  "management_credibility_rationale": "<1 phrase courte, max 20 mots, en francais>",
  "reinvestment_narrative_score": <0-100, 0=discours de desinvestissement/detresse, 100=discours de reinvestissement clair et argumente>,
  "reinvestment_narrative_rationale": "<1 phrase courte, max 20 mots, en francais>",
  "why_discounted_summary": "<1-2 phrases courtes, max 40 mots, en francais>"
}}

Texte du MD&A :
---
{mdna_text}
---
"""


@dataclass
class QualitativeProfile:
    ticker: str
    cyclicity_score: float
    cyclicity_rationale: str
    management_credibility_score: float
    management_credibility_rationale: str
    reinvestment_narrative_score: float
    reinvestment_narrative_rationale: str
    why_discounted_summary: str

    @property
    def qualitative_composite(self) -> float:
        """Moyenne simple des 3 scores numeriques -- a ponderer plus finement
        une fois integre au scorer global si besoin."""
        return round(
            (self.cyclicity_score + self.management_credibility_score + self.reinvestment_narrative_score) / 3,
            1,
        )


def is_us_domestic_filer(cik: str) -> bool:
    """True si l'entreprise a deja depose au moins un 10-K (indique un
    emetteur domestique US). Les emetteurs prives etrangers (ex. TSM,
    cotes sur le NYSE via ADR mais domicilies a l'etranger) deposent un
    20-F a la place -- a exclure de l'univers 'USA' du programme."""
    resp = _get_with_retry(SUBMISSIONS_URL.format(cik10=cik))
    if resp is None or resp.status_code != 200:
        return False
    forms = resp.json().get("filings", {}).get("recent", {}).get("form", [])
    return "10-K" in forms


def _get_latest_10k_doc_url(cik: str) -> str | None:
    """Trouve l'URL du document principal du dernier 10-K depose."""
    resp = _get_with_retry(SUBMISSIONS_URL.format(cik10=cik))
    if resp is None or resp.status_code != 200:
        return None
    data = resp.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    for form, accession, primary_doc in zip(forms, accessions, primary_docs):
        if form == "10-K":
            accession_nodash = accession.replace("-", "")
            cik_int = int(cik)
            return FILING_DOC_URL.format(cik_int=cik_int, accession_nodash=accession_nodash, primary_doc=primary_doc)
    return None


def _extract_mdna_section(raw_html: str, max_chars: int = 40_000, min_section_len: int = 3_000) -> str | None:
    """Extrait le texte entre 'Item 7' et 'Item 7A'/'Item 8'. Le HTML des
    10-K varie enormement d'une entreprise a l'autre (tables, styles inline)
    -- cette extraction est volontairement simple (regex sur texte brut apres
    suppression des balises) et A VALIDER manuellement sur quelques exemples.

    Piege frequent : la plupart des 10-K ont une table des matieres en debut
    de document qui liste "Item 7. Management's Discussion..." -- si on
    prend juste la PREMIERE occurrence, on capture cette ligne de sommaire
    (quelques mots) au lieu de la vraie section, souvent des dizaines de
    pages plus loin. On cherche donc TOUTES les occurrences de depart et on
    garde celle qui est suivie du plus grand volume de texte avant la
    prochaine occurrence de fin -- la vraie section est toujours beaucoup
    plus longue qu'une ligne de sommaire."""
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw_html))
    text = re.sub(r"\s+", " ", text)

    # Plusieurs formats de titre observes selon les entreprises : separateur
    # entre "Item 7" et "Management" pas toujours un simple espace (tiret,
    # deux-points, etc.), et en dernier recours la phrase standard sans le
    # prefixe "Item 7" du tout (certains filings la formatent differemment).
    START_PATTERNS = [
        r"Item\s*7\.?\s*Management",
        r"Item\s*7[^A-Za-z0-9]{0,20}Management",
        r"Management['\u2019]s Discussion and Analysis",
    ]
    END_PATTERNS = [
        r"Item\s*7A\.?\s*Quantitative",
        r"Item\s*8\.?\s*Financial",
    ]

    start_matches: list[re.Match] = []
    for pattern in START_PATTERNS:
        start_matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if start_matches:
            break

    end_matches: list[re.Match] = []
    for pattern in END_PATTERNS:
        found = list(re.finditer(pattern, text, re.IGNORECASE))
        end_matches.extend(found)
    end_matches.sort(key=lambda m: m.start())

    if not start_matches:
        return None

    best_section = None
    best_len = 0
    for sm in start_matches:
        # la fin la plus proche APRES ce depart
        candidates_end = [em for em in end_matches if em.start() > sm.end()]
        end_pos = candidates_end[0].start() if candidates_end else sm.end() + max_chars
        section_len = end_pos - sm.start()
        if section_len > best_len:
            best_len = section_len
            best_section = text[sm.start():end_pos]

    if best_section is None or best_len < min_section_len:
        return None  # probablement encore la table des matieres, rien de fiable

    return best_section[:max_chars]


def fetch_mdna(ticker: str) -> str | None:
    cik = get_cik_for_ticker(ticker)
    if cik is None:
        print(f"[{ticker}] CIK introuvable.")
        return None
    doc_url = _get_latest_10k_doc_url(cik)
    if doc_url is None:
        print(f"[{ticker}] Aucun 10-K trouve.")
        return None
    resp = _get_with_retry(doc_url)
    if resp is None:
        print(f"[{ticker}] Document 10-K inaccessible apres plusieurs tentatives (erreur reseau).")
        return None
    if resp.status_code != 200:
        print(f"[{ticker}] Document 10-K inaccessible (HTTP {resp.status_code}).")
        return None
    mdna = _extract_mdna_section(resp.text)
    if mdna is None:
        print(f"[{ticker}] Section MD&A non trouvee (le decoupage regex a probablement echoue sur ce format de 10-K).")
    return mdna


def analyze_mdna(ticker: str, mdna_text: str) -> QualitativeProfile | None:
    client = anthropic.Anthropic()  # lit ANTHROPIC_API_KEY depuis l'environnement
    prompt = ANALYSIS_PROMPT.format(ticker=ticker, mdna_text=mdna_text)

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2200,  # 1000 puis 1500 tronquaient encore le JSON sur certains titres (texte francais + 4 champs)
        messages=[{"role": "user", "content": prompt}],
    )
    # content[0] n'est pas toujours du texte -- avec l'extended thinking,
    # le premier bloc peut etre un ThinkingBlock (raisonnement interne).
    # On cherche explicitement le bloc de type "text" plutot que de
    # supposer sa position. Bug trouve en conditions reelles avec Thomas.
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        print(f"[{ticker}] Reponse Claude sans bloc texte exploitable (contenu : {[b.type for b in response.content]}).")
        return None
    raw_text = text_blocks[0].strip()
    raw_text = re.sub(r"^```json\s*|\s*```$", "", raw_text)

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        print(f"[{ticker}] Reponse Claude non parsable en JSON :\n{raw_text}")
        return None

    return QualitativeProfile(
        ticker=ticker.upper(),
        cyclicity_score=parsed["cyclicity_score"],
        cyclicity_rationale=parsed["cyclicity_rationale"],
        management_credibility_score=parsed["management_credibility_score"],
        management_credibility_rationale=parsed["management_credibility_rationale"],
        reinvestment_narrative_score=parsed["reinvestment_narrative_score"],
        reinvestment_narrative_rationale=parsed["reinvestment_narrative_rationale"],
        why_discounted_summary=parsed["why_discounted_summary"],
    )


def score_qualitative_profile(ticker: str) -> QualitativeProfile | None:
    mdna = fetch_mdna(ticker)
    if not mdna:
        return None
    return analyze_mdna(ticker, mdna)


if __name__ == "__main__":
    for tk in ["AAPL", "BYRN"]:
        profile = score_qualitative_profile(tk)
        print(profile)