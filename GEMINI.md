# Projekt-Regeln (sob-generator)

## Sprache & Kommunikation
- Antworte in diesem Projekt **ausschließlich auf Deutsch**.
- Präzise, professionell, keine Floskeln.

## Architektur- & Sicherheitsinvariante (Mandatory)
1. **Datenschutz & DSGVO (docs/DATENSCHUTZ.md):**
   - In Logs (`logs/chat_app.log`, Stream-Logs etc.) dürfen **niemals Klientennamen oder personenbezogene Daten** stehen.
   - Ausschließlich technische Metadaten protokollieren (z. B. Zielanzahl, Tokens/s; niemals Klientennamen oder benutzerkontrollierte Dateinamen mit Personenbezug).

2. **Plan-Adapter & Datenintegrität (ADR-4 / ADR-10):**
   - `PreviousPlanData` hält Abschnitte in `.sections: Dict[str, PlanSectionGoal]`.
   - Der Adapter in `chat_app/plan_loader.py` mappt primär über `sections.values()` ohne Datenverlust (inkl. `stand_zielerreichung`, `vorschlag_ziele`, Zeitraum).
   - Fallback-Parser werden nur aktiviert, wenn keine Abschnitte extrahiert werden konnten.

3. **Kontext-Budgetierung & Token-Limits:**
   - In `to_compact_context()` müssen alle dynamischen Freitextfelder (Entwicklungen, Stände, Vorschläge) gemeinsam quotiert werden.
   - Kein hartes Abschneiden späterer Abschnitte; das Gesamtbudget von max. 500 Tokens / 2.400 Zeichen muss durch proportionale Kürzung der Inhalte eingehalten werden.

4. **Nebenläufigkeit im Chatbot:**
   - Dialog-Turns in `chat_stream()` müssen über einen Turn-Lock serialisiert werden, um Message-Interleaving bei parallelen Anfragen zu verhindern.

5. **Pivot-Konsolidierung (Issue #97):**
   - `chat_app` fungiert als alleiniger Produkt- und Applikationsstandard. Veraltete Batch- und Desktop-Komponenten (`app/`, `pywebview`) sind vollständig bereinigt.
