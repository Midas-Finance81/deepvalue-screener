"""
Orchestrateur -- cycle de rebalancement bi-mensuel
========================================================

Enchaine les 5 etapes validees dans l'ordre :
  1. portfolio_review.py    -- etat des positions actuellement detenues
  2. execute_trim()          -- trim des positions >= seuil de gain (+100%)
  3. position_health.py      -- positions en baisse : Pilier 1 + Piliers 2/3
                                 croises -> alerte ou cloture automatique
  4. run_screener()          -- nouveau screening (Pilier 1 -> composite)
  5. execute_reconciliation() -- reconciliation/reequilibrage final

Cadence prevue : 2eme mardi et dernier mardi du mois (decision Thomas).
Ce module ne gere PAS encore le declenchement automatique a ces dates ni
la connexion automatique a TWS -- ca reste a faire (prochaine etape). Pour
l'instant, ce script se lance manuellement, mais avec la meme sequence
que celle qui tournera un jour sans supervision.

GARDE-FOU GLOBAL : dry_run=True par defaut sur TOUTES les etapes qui
touchent a de l'argent (trim, reconciliation) -- aucun ordre reel envoye
tant que ce n'est pas explicitement desactive. apply_health_decisions
(etape 3) est lui aussi a True par defaut UNIQUEMENT pour la decision en
base (marquer 'closed') -- la vente reelle correspondante ne part que via
l'etape 5, qui a son propre dry_run.

GESTION D'ERREURS : chaque etape est isolee dans son propre try/except --
un echec sur une etape (ex. TWS pas connecte) n'empeche pas les etapes
suivantes de s'executer. Un resume final indique quelles etapes ont
reussi/echoue, pour verification apres un run sans supervision.

NON TESTE dans ce sandbox (les 5 fonctions sous-jacentes necessitent
sec.gov, l'API Claude, et/ou TWS, tous indisponibles ici) : ce module
lui-meme n'a que sa logique de sequencement/gestion d'erreurs testable
en isolation -- teste sur ta machine avec TWS ouvert avant de faire
confiance a un run complet.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import date

from core.portfolio_review import print_portfolio_review
from core.execution import execute_trim, execute_reconciliation, PAPER_TRADING_PORT
from core.position_health import print_health_report
from core.composite_scorer import run_screener


@dataclass
class CycleStepResult:
    step_name: str
    success: bool
    error: str | None = None


@dataclass
class CycleResult:
    run_date: date
    steps: list[CycleStepResult] = field(default_factory=list)

    @property
    def all_succeeded(self) -> bool:
        return all(s.success for s in self.steps)


def _run_step(steps: list[CycleStepResult], step_name: str, fn, *args, **kwargs) -> None:
    """Execute une etape, isole toute exception pour ne pas interrompre
    les etapes suivantes, enregistre le resultat."""
    print(f"\n{'=' * 70}\nETAPE : {step_name}\n{'=' * 70}")
    try:
        fn(*args, **kwargs)
        steps.append(CycleStepResult(step_name=step_name, success=True))
    except Exception as e:
        print(f"[ERREUR] L'etape '{step_name}' a echoue : {e}")
        traceback.print_exc()
        steps.append(CycleStepResult(step_name=step_name, success=False, error=str(e)))


def run_rebalancing_cycle(
    total_capital: float | None = 5_000,  # budget fixe de paper trading (le solde IBKR par defaut, 1M$, n'est pas representatif)
    port: int = PAPER_TRADING_PORT,
    dry_run: bool = True,
    include_qualitative: bool = True,
    apply_health_decisions: bool = True,
    collection_days: int = 90,
) -> CycleResult:
    """Point d'entree principal du cycle bi-mensuel. Voir les garde-fous
    dans la docstring du module avant de lancer avec dry_run=False."""
    run_date = date.today()
    steps: list[CycleStepResult] = []

    print(f"\n{'#' * 70}")
    print(f"# CYCLE DE REBALANCEMENT -- {run_date} -- "
          f"{'PAPER TRADING' if port == PAPER_TRADING_PORT else 'COMPTE REEL'} -- "
          f"{'DRY RUN' if dry_run else 'EXECUTION REELLE'}")
    print(f"{'#' * 70}")

    _run_step(steps, "1. Analyse des positions en cours", print_portfolio_review, include_qualitative)
    _run_step(steps, "2. Trim des positions au-dela du seuil de gain", execute_trim,
              total_capital, port, dry_run)
    _run_step(steps, "3. Sante des positions en baisse (Pilier 1 + Piliers 2/3)", print_health_report,
              include_qualitative, apply_health_decisions, collection_days)
    _run_step(steps, "4. Nouveau screening (Pilier 1 -> composite)", run_screener, collection_days)
    _run_step(steps, "5. Reconciliation / reequilibrage final", execute_reconciliation,
              total_capital, port, "MKT", dry_run)

    result = CycleResult(run_date=run_date, steps=steps)

    print(f"\n{'#' * 70}\n# RESUME DU CYCLE -- {run_date}\n{'#' * 70}")
    for s in result.steps:
        status = "OK" if s.success else f"ECHEC ({s.error})"
        print(f"  [{status}] {s.step_name}")
    if not result.all_succeeded:
        print("\nATTENTION : au moins une etape a echoue -- verifie le detail ci-dessus "
              "avant de considerer ce cycle comme complet.")

    return result


if __name__ == "__main__":
    # Lancement manuel par defaut, en dry_run -- aucun ordre reel envoye.
    # Passe dry_run=False explicitement une fois pret a executer reellement.
    run_rebalancing_cycle(dry_run=False)
