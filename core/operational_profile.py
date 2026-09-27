"""
Pilier 3 — Performance operationnelle (brique quantitative)
==============================================================

  - "Quelle est la reserve de cash ?"          -> cash_and_equivalents
  - "Est-ce qu'ils reinvestissent dans le business ?" -> capex vs D&A
  - "Vendent-ils leurs outils de production ?"  -> proceeds from asset sales
  - Tendance du FCF sur plusieurs annees (pas juste le niveau, comme demande)

Source : meme pipeline que bond_quality.py (SEC EDGAR XBRL company facts).
Reutilise get_cik_for_ticker() de ce module pour eviter la duplication de
code (meme convention "core/" que le reste du programme).

  - cash_and_equivalents : valeur INSTANTANEE la plus recente (10-K ou 10-Q,
    peu importe), pas seulement le dernier exercice annuel.
  - capex / depreciation_amortization (utilises dans reinvestment_ratio) :
    calcul TTM (12 mois glissants), pas le dernier exercice annuel seul.
  - fcf_by_year (tendance sur plusieurs annees) : RESTE en annuel (10-K
    seul) -- une tendance multi-annees n'a pas de sens en TTM glissant,
    seul le point le plus recent devait etre rafraichi.
"""
 
from __future__ import annotations

from dataclasses import dataclass, field

import requests

from core.bond_quality import (
    SEC_HEADERS,
    COMPANY_FACTS_URL,
    get_cik_for_ticker,
    _latest_instant_value,
    _ttm_duration_value,
)

# Tags XBRL candidats, par ordre de preference.
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements", "PaymentsToAcquireProductiveAssets"]
DEPRECIATION_TAGS = ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation"]
ASSET_SALE_PROCEEDS_TAGS = ["ProceedsFromSaleOfPropertyPlantAndEquipment"]
OPERATING_CASH_FLOW_TAGS = ["NetCashProvidedByUsedInOperatingActivities"]


@dataclass
class OperationalProfile:
    ticker: str
    cik: str
    cash_and_equivalents: float | None
    capex: float | None
    depreciation_amortization: float | None
    asset_sale_proceeds: float | None
    fcf_by_year: dict[str, float] = field(default_factory=dict)  # {"2023": ..., "2024": ..., "2025": ...}

    @property
    def reinvestment_ratio(self) -> float | None:
        """Capex / D&A. > 1 = investit plus qu'il n'amortit (croissance des
        actifs productifs). < 1 = sous-investissement (signal d'alerte
        deep-value : l'entreprise "consomme" son outil de production)."""
        if self.capex is None or not self.depreciation_amortization:
            return None
        return round(self.capex / self.depreciation_amortization, 2)

    @property
    def is_selling_productive_assets(self) -> bool | None:
        """Vend-elle une part significative de ses outils de production
        (>10% du capex de la meme annee) ? Signal potentiel de detresse."""
        if self.asset_sale_proceeds is None or not self.capex:
            return None
        return self.asset_sale_proceeds > 0.10 * self.capex

    @property
    def fcf_trend(self) -> str | None:
        """Tendance du FCF sur les annees disponibles, basee sur la pente
        d'une regression lineaire (pas une simple comparaison premier/dernier
        point, trop sensible au bruit sur des series courtes et volatiles).
        Pente normalisee par le niveau moyen absolu du FCF -> % par an.
        > +5%/an = 'hausse', < -5%/an = 'baisse', sinon 'stable'."""
        years = sorted(self.fcf_by_year.keys())
        if len(years) < 2:
            return None
        values = [self.fcf_by_year[y] for y in years]
        n = len(values)
        xs = list(range(n))
        x_mean = sum(xs) / n
        y_mean = sum(values) / n
        denom = sum((x - x_mean) ** 2 for x in xs)
        if denom == 0:
            return None
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values)) / denom
        avg_abs_level = sum(abs(v) for v in values) / n
        if avg_abs_level == 0:
            return None
        normalized_slope = slope / avg_abs_level
        if normalized_slope > 0.05:
            return "hausse"
        if normalized_slope < -0.05:
            return "baisse"
        return "stable"


def _all_annual_values(facts_json: dict, tags: list[str]) -> dict[str, float]:
    """Retourne TOUTES les valeurs annuelles disponibles (10-K, FY),
    indexees par annee fiscale de fin de periode -- utilise UNIQUEMENT
    pour la serie de tendance FCF sur plusieurs annees (fcf_by_year),
    volontairement laissee en annuel pur (cf. note TTM en tete de fichier)."""
    us_gaap = facts_json.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        tag_data = us_gaap.get(tag)
        if not tag_data:
            continue
        usd_units = tag_data.get("units", {}).get("USD", [])
        annual_points = [p for p in usd_units if p.get("form") == "10-K" and p.get("fp") == "FY"]
        if not annual_points:
            continue
        # dedup par annee (garder le depot le plus recent si plusieurs)
        by_year: dict[str, float] = {}
        for p in annual_points:
            year = p["end"][:4]
            by_year[year] = float(p["val"])
        return by_year
    return {}


def _latest(by_year: dict[str, float]) -> float | None:
    """Conserve pour compatibilite eventuelle -- plus utilise directement
    dans score_operational_profile (remplace par les helpers TTM/instantane
    de bond_quality.py), mais garde son utilite si besoin d'un point annuel
    brut ailleurs."""
    if not by_year:
        return None
    latest_year = max(by_year.keys())
    return by_year[latest_year]


def score_operational_profile(ticker: str, years_for_trend: int = 5) -> OperationalProfile | None:
    cik = get_cik_for_ticker(ticker)
    if cik is None:
        print(f"[{ticker}] CIK introuvable.")
        return None

    resp = requests.get(COMPANY_FACTS_URL.format(cik10=cik), headers=SEC_HEADERS, timeout=30)
    if resp.status_code != 200:
        print(f"[{ticker}] company facts indisponibles (HTTP {resp.status_code}).")
        return None
    facts = resp.json()

    # Serie annuelle (10-K seul) -- UNIQUEMENT pour la tendance FCF sur
    # plusieurs annees, qui n'a pas de sens en TTM glissant.
    capex_by_year = _all_annual_values(facts, CAPEX_TAGS)
    ocf_by_year = _all_annual_values(facts, OPERATING_CASH_FLOW_TAGS)
    common_years = sorted(set(ocf_by_year) & set(capex_by_year))[-years_for_trend:]
    fcf_by_year = {y: ocf_by_year[y] - capex_by_year[y] for y in common_years}

    # Valeurs "actuelles" du profil -- TTM pour les flux, instantane pour le
    # cash (photo la plus recente possible, cf. decision fraicheur/TTM).
    latest_capex = _ttm_duration_value(facts, CAPEX_TAGS)
    latest_dep = _ttm_duration_value(facts, DEPRECIATION_TAGS)
    latest_asset_sales = _ttm_duration_value(facts, ASSET_SALE_PROCEEDS_TAGS)
    latest_cash = _latest_instant_value(facts, CASH_TAGS)

    return OperationalProfile(
        ticker=ticker.upper(),
        cik=cik,
        cash_and_equivalents=latest_cash,
        capex=latest_capex,  # XBRL "Payments..." = deja positif = montant depense
        depreciation_amortization=latest_dep,
        asset_sale_proceeds=latest_asset_sales,
        fcf_by_year=fcf_by_year,
    )


if __name__ == "__main__":
    for tk in ["AAPL", "F", "BYRN"]:
        profile = score_operational_profile(tk)
        if profile:
            print(profile.ticker, "| cash:", profile.cash_and_equivalents,
                  "| capex/D&A:", profile.reinvestment_ratio,
                  "| vend ses actifs:", profile.is_selling_productive_assets,
                  "| tendance FCF:", profile.fcf_trend, profile.fcf_by_year)