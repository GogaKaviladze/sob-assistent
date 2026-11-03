# Schnelle Dialogevaluation für Issue 76

Die Evaluation trennt drei Aufgaben:

1. `evals/issue76_cases.json` enthält ausschließlich synthetische Dialogfälle und klare Erwartungen.
2. `evals/issue76_runner.py` führt Fälle reproduzierbar aus und prüft harte Regeln.
3. `notebooks/issue76_feedback.ipynb` zeigt die Ergebnisse für die fachliche Bewertung.

Das Notebook enthält keine eigene Testlogik. Dadurch liefern CLI, automatisierte Tests und Jupyter dieselben Ergebnisse.

## Schnelltest

```bash
cd /Users/home/Dev/sob-generator-lab
.venv/bin/python scripts/run_issue76_eval.py --mode stub
```

Der Stub-Lauf prüft in wenigen Millisekunden Dialogzustände, Korrekturen, Unsicherheiten und technische Regeln. Ein Exit-Code `1` bedeutet, dass mindestens ein fachlicher Testfall fehlgeschlagen ist; der Runner selbst kann trotzdem korrekt funktionieren.

## Einzelner Fall mit dem realen Modell

```bash
.venv/bin/python scripts/run_issue76_eval.py \
  --mode model \
  --case einzelne_beobachtung
```

Mehrere `--case`-Argumente können kombiniert werden. Das Modell wird dabei nur einmal geladen.

## Vollständige Modellprüfung

```bash
.venv/bin/python scripts/run_issue76_eval.py --mode model --no-cache
```

Ohne `--no-cache` werden unveränderte Fälle aus `output/evals/issue76_cache.json` geladen. Der Cache berücksichtigt Testfall, Generator, Modellpfad, Git-Revision und lokale Änderungen am Produktcode.

## Jupyter

Jupyter im Worktree starten und den Kernel `SOB Generator` auswählen:

```bash
cd /Users/home/Dev/sob-generator-lab
jupyter lab
```

Danach `notebooks/issue76_feedback.ipynb` öffnen. Für schnelle Tests bleibt `ECHTES_MODELL = False`. Nur für eine fachliche Modellabnahme wird der Wert auf `True` gesetzt.

## Ergebnisse bewerten

Automatisch geprüft werden unter anderem:

- Ziel ohne erwartete Grundlage,
- fehlende fachliche Rückfrage,
- sichtbares Roh-JSON,
- direkte Ansprache der betreuten Person,
- verlorene Korrektur,
- verlorene Unsicherheit,
- ausdrücklich verbotene oder erforderliche Formulierungen.

Die automatische Prüfung ersetzt nicht die fachliche Abnahme. Bei fehlgeschlagenen Fällen werden zusätzlich Quellenbindung, fachliche Qualität und Verständlichkeit der Antwort beurteilt. Neue Fehler werden zuerst als kleiner synthetischer Fall in `issue76_cases.json` ergänzt und anschließend im Produktcode behoben.
