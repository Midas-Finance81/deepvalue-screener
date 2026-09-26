DeepValue — Composite Deep Value Equity Screener

A US equity screener built on a three-pillar composite method: clustered insider buying, debt quality, and operational performance. The idea is simple — a stock is interesting when the people who know the business best are buying it with their own money, the balance sheet can survive a bad year, and the cash generation is real rather than accounting.

Each pillar is scored independently from primary sources (SEC EDGAR filings, XBRL financial data), then combined into a 0-100 composite used to build a 20-position portfolio.

Method
Pillar 1 — Clustered insider buying (core/insider_clusters.py)

Scans Form 4 filings from the SEC EDGAR daily index to detect open-market buying clusters: at least 3 distinct insiders, cumulative value ≥ $200,000, within a rolling 60-day window (configurable).

Score bonuses are applied when:

a key officer (CEO / CFO / President / Chairman) is among the buyers;
the buying happened near the low of the 52-week range (buying the dip, not the rip);
the clustering insiders hold a significant share of the company (approximation, ≥ 20%).

Filings already processed are cached in a local SQLite database, and collection runs in parallel (ThreadPoolExecutor with a shared rate limiter kept under the SEC's 10 requests/second limit). This makes a 3-month collection window feasible instead of an overnight job.

Pillar 2 — Debt quality (core/bond_quality.py)

Interest coverage ratio (EBIT / interest expense, computed on a trailing twelve months basis) mapped onto Damodaran's synthetic rating table (NYU Stern).

For financial institutions (SIC 6xxx) and cases where interest coverage is not meaningful (captive finance subsidiaries, stale XBRL data), the module falls back automatically to a leverage ratio (equity / total assets) calibrated on FDIC regulatory thresholds (Prompt Corrective Action framework).

Pillar 3 — Operational performance

Quantitative (core/operational_profile.py): cash position, capex / D&A ratio (TTM), 5-year free cash flow trend via linear regression, and detection of productive-asset disposals.

Qualitative (core/quality_profile.py): extracts the MD&A section of the latest 10-K and scores it through the Claude API on three axes — cyclicality, management credibility, and the reinvestment narrative.

Composite scoring (core/composite_scorer.py)

The three pillars are combined (equal weighting) into a 0-100 composite, then:

adjusted by a momentum guardrail: 5 regimes based on the 6-month trend and the 1-month direction, worth ±2 points — enough to break ties, not enough to override the fundamentals;
filtered on a $3 price floor and a US-only universe (foreign private issuers filing 20-F are excluded, since the data model assumes 10-K/10-Q).

The final portfolio holds 20 positions split into 4 quartiles of 5, weighted 40% / 30% / 20% / 10% of capital by quartile, renormalised to 100% when the target count is not reached.

Persistence (core/persistence.py)

Every run is written to data/portfolio.db: full score history, open and closed positions, and a rebalancing journal (buys / sells / weight adjustments). This is the groundwork for automation (an Interactive Brokers bridge) and for measuring the strategy against its own past decisions rather than against memory.

Architecture
DeepValue/
├── core/
│   ├── insider_clusters.py     # Pillar 1 — Form 4 clusters (SEC EDGAR)
│   ├── bond_quality.py         # Pillar 2 — interest coverage / leverage
│   ├── operational_profile.py  # Pillar 3a — operational performance (quant)
│   ├── quality_profile.py      # Pillar 3b — MD&A analysis (Claude API)
│   ├── composite_scorer.py     # Orchestration + portfolio construction
│   └── persistence.py          # Run history and position tracking (SQLite)
├── data/
│   ├── sec_cache.db            # Processed SEC filings cache (auto-generated)
│   └── portfolio.db            # Score and position history (auto-generated)
├── .env                        # ANTHROPIC_API_KEY (never committed)
└── requirements.txt
Running a full screen
powershell
.venv\Scripts\Activate.ps1
python -m core.composite_scorer

Default collection window is 60 days. For a different window:

powershell
python -c "from core.composite_scorer import run_screener; run_screener(collection_days=90)"

At the end of a run you get the final portfolio, a checklist for the manual FINRA bond-tradability check, and a rebalancing summary against the previous run.

Known limitations

These are deliberate trade-offs, documented rather than hidden:

Financial institutions and captive finance subsidiaries: the leverage ratio is an approximation, not a strict equivalent of a regulatory Tier 1 ratio.
Insider ownership (Pillar 1): only insiders who have recently transacted appear in Form 4 data, so total insider ownership is likely underestimated.
FINRA bond tradability: manual check only — no CUSIP is available in the data to automate the lookup.
Institutional and activist holders (13F): not implemented.
Refinancing risk: maturity walls are not modelled; the debt pillar measures the ability to service debt, not the ability to roll it.
Stack

Python · SEC EDGAR API (Form 4, 10-K, XBRL Company Facts) · SQLite · Anthropic API · pandas / numpy / scipy

Built and maintained by Thomas Roques.
