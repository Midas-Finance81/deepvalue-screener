@echo off
REM Lancement du cycle de rebalancement bi-mensuel via Task Scheduler.
REM Journalise tout (stdout + stderr) dans logs\ avec un nom horodate,
REM car une tache planifiee tourne sans console visible -- sans ce
REM fichier, impossible de savoir apres coup ce qui s'est passe.

cd /d C:\Users\thoma\PycharmProjects\DeepValue

if not exist logs mkdir logs

REM Horodatage du fichier de log (format AAAA-MM-JJ_HHMMSS, insensible
REM aux parametres regionaux Windows contrairement a %date%/%time%)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmmss"') do set TIMESTAMP=%%i

set LOGFILE=logs\cycle_%TIMESTAMP%.log

call .venv\Scripts\activate.bat

REM dry_run=True par defaut (voir orchestrator.py) -- reste en dry_run
REM tant que tu n'as pas explicitement decide de passer en execution reelle.
python -c "from core.orchestrator import run_rebalancing_cycle; run_rebalancing_cycle()" > "%LOGFILE%" 2>&1

echo Cycle termine. Log : %LOGFILE%
