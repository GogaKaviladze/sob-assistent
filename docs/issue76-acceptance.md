# Issue 76: Implementierung und fachliche Abnahme

Stand: 14.09.2026. Branch `feature_grounded_dialogue_76`, Basis `75926a5`.
Ausschließlich synthetische Daten; keine echten Klientendaten oder Modellgewichte im Commit.

> **Nachtrag (DSGVO-Audit M5):** Die damalige Sonderbehandlung markierter
> Notizen im Chat (Abtrennen und getrenntes Vorhalten im RAM) wurde vollständig
> entfernt. Die zugehörigen Prüfschritte und Regressionstests sind in diesem
> Dokument nicht mehr enthalten; jede Eingabe wird heute gleich behandelt.
> `issue76-dialogue.json` bleibt als unverändertes Protokoll des damaligen Laufs erhalten.

## Ergebnis

Die reproduzierten Zustands- und UI-Fehler sind behoben: neutrale, nicht automatisch gesendete
Chips; getrennte historische, vorgeschlagene und bestätigte Ziele; strukturierte
Zielrevision; wortgetreue Notizextraktion; explizite Korrekturen; offene Rückfragen;
editierbare Vorschau; Erhalt des Export-Lebenszyklus aus #74.

**Die fachliche Gesamtabnahme ist noch nicht erteilt.** Reale Qwen-7B-Läufe
zeigen trotz besserer Gesprächsführung verbleibende unbelegte Deutungen und
Verallgemeinerungen in frei generierten Berichten. Ein technisch gültiges JSON
oder ein grünes Export-Qualitäts-Gate belegt keine fachliche Richtigkeit.
Issue #76 und #73 deshalb nicht schließen oder diese Änderung als vollständig
fachlich abgenommen ausweisen. Nächster Schwerpunkt: belegbezogene Prüfung der
Entwurfsbehauptungen und fallbezogene Qualität der Rückfragen, ohne zusätzliche
UI-Abkürzungen oder alleinigen Modellwechsel.

## Automatische Prüfung

- `.venv/bin/python -m pytest -q`: 400 bestanden, eine bestehende
  Starlette/httpx-Deprecation-Warnung.
- Alle 75 versionierten und neu hinzugefügten Python-Dateien mit
  `py_compile.compile(..., doraise=True)` kompiliert.
- Ausführbare Node-Tests prüfen Chips einschließlich unveränderter Texteingabe
  und fehlendem automatischen Submit. Bestehende Export-Bridge-Tests bleiben grün.
- Neue Regressionen: Zielrevision in allen drei Feldphasen, Fehler erhalten
  gültigen Stand, falsche Extraktion wird abgewiesen,
  Korrektur ohne Verlust offener Frage, Fachfrage wird nicht zurückgespiegelt,
  Vorschauänderung hebt Freigabe auf, HTMX ersetzt nur den Chat-Wurzelknoten.

## Tatsächliche Exportprüfung

Mit einer synthetisch vorbefüllten Sitzung, echtem FastAPI-Server und tatsächlicher
pywebview-`JsApi` auf macOS ausgeführt. Für die UI-Prüfung war ausschließlich die
Textgenerierung durch den Stub ersetzt; Save-Dialog und Dateisystemzugriff waren echt.

1. Nativer Zwischenexport öffnete den macOS-Speicherdialog.
2. Abbrechen zeigte „Speichern abgebrochen — die Datei wurde nicht gesichert.“;
   die Sitzung blieb bearbeitbar.
3. Erneuter Export und Speichern nach `/tmp/issue76-native-synthetic.docx`
   zeigte „Gespeichert!“; tatsächliche DOCX-Datei, 30.528 Bytes, geprüft.
4. Anschließend Berichtstext geändert und gespeichert; Änderung sichtbar,
   erneute Freigabe erforderlich.
5. Im tatsächlichen Browser Vorschauänderung gespeichert, Abschnitt freigegeben,
   DOCX heruntergeladen. Die Datei mit der synthetischen Person wurde geprüft
   (30.520 Bytes); die Sitzung bestand nach Download weiter.
6. Erst „Datei gespeichert – Lauf abschließen“ zeigte sichtbar
   „Bericht exportiert — Lauf abgeschlossen“. Ein anschließender HTTP-Aufruf
   der synthetischen Sitzung lieferte 404.

Dabei wurde ein realer HTMX-Fehler behoben: vollständige Antwortseiten waren
ineinander verschachtelt, wodurch beim Abschluss auch die Abschlussanzeige
versteckt wurde. `hx-select="#chat-app"` und `hx-swap="outerHTML"` verhindern dies.

## Modellprüfung

Mehrere reale Läufe mit `Qwen2.5-7B-Instruct-Q4_K_M.gguf` auf Apple Silicon.
Die letzte Ausführung ist in `issue76-dialogue.json` zusammengefasst; sie enthält
Eingaben, Antworten, Laufzeiten, öffentliche Notizen und den resultierenden Bericht.

Aus den Vorläufen gefundene und behobene Ursachen:

- Freie Notizparaphrasen: dynamische JSON-Grammatik erlaubt nur wörtliche
  Satzkandidaten der aktuellen Eingabe.
- Krisenvorgaben zwangen pauschal Diagnoseterminologie und Krisenziele auf:
  nur belegte Begriffe; historische Krisen nicht als akute Gefährdung behandeln.
- Berichtsprompt überfrachtet: Chat-Verfeinerung verwendet einen eigenen,
  kürzeren Prompt mit ausdrücklicher Quellentrennung und Korrekturpriorität.
- Neuigkeitsdruck bei Dialogen: neue Dialog- und Zielverträge verwenden
  Temperatur 0,2 und Wiederholungsstrafe 1,0, damit Belege wiederholt werden können.
  Bestehende Berichtsgenerierung behält ihre bisherigen Sampling-Einstellungen.
- Zwei frühe Testläufe verwendeten versehentlich eine abweichende Gesundheits-
  Bezeichnung für Feld 1.4. Der letzte Lauf verwendet korrekt Feld 1.5;
  frühe Resultate sind kein Nachweis korrekter Feldzuordnung.

## Letzter Lauf und Restgrenzen

Der letzte Lauf dauerte 72.76 Sekunden für zwölf Beiträge
zuzüglich initialer Zielgenerierung. Die Fachfragen erhielten unterschiedliche,
fallbezogene Antworten. Unsicherheit und Korrektur lösten keinen Entwurf aus.
Die Zielrevision blieb als Vorschlag erhalten; der Wechsel führte korrekt zu 1.5.

Die zusätzliche, schema-validierte Quellenprüfung hat beide fehlerhaften
Berichtskandidaten tatsächlich abgewiesen: selbstständiges Brieföffnen trotz
Korrektur sowie erfundene regelmäßige Gesundheitschecks. Beide Berichtsfelder
blieben leer; vorhandene Zielvorschläge blieben erhalten. Ein vorhandener
Entwurf bleibt bei diesem Fehlerpfad unverändert (automatisiert geprüft).
Das verbessert die Fehlerbegrenzung, belegt aber noch keine zuverlässig gute
Entwurfserzeugung. Die Quellenprüfung verwendet dasselbe Modell und kann
Fehler übersehen oder belegte Texte fälschlich zurückweisen.

Weitere Grenzen: explizite Korrekturen werden nur bei eindeutiger Zuordnung
per Wortüberlappung automatisch ersetzt; sonst wird nachgefragt. Rückfragen
bleiben stellenweise wiederholend, und SMART-Vorschläge benennen fehlende
Termine nicht immer ausdrücklich. Der letzte Satz der deterministischen
Unwissen-Antwort wurde nach Start dieses Modelllaufs auf eine konkrete
Einzelsituation ausgerichtet; dieser kleine Wortlautwechsel ist durch den
anschließenden vollständigen Testlauf, nicht durch einen weiteren Modelllauf
geprüft. Die automatische Mindestlänge und automatische klinische
Umformulierungen wurden im Chat-Entwurfspfad entfernt, damit sie keine
unbelegten Ergänzungen erzwingen.

## Reproduktion

Modell lokal bereitstellen, folgenden Code aus dem Repository-Wurzelverzeichnis
mit `.venv/bin/python` ausführen (`PYTHONPATH=.` setzen, falls als Datei außerhalb
des Repositorys gespeichert). Ausgabe nach `/tmp/issue76_model.json`;
Modellgenerierung bleibt lokal. Der feste Seed steht in den aufgezeichneten
Modellsettings; Plattform und llama.cpp-Version können Ausgaben beeinflussen.

```python
import asyncio, json, time
from pathlib import Path
from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import ChatSession, SectionChatState, DialoguePhase
from sob_generator.chat_store import ChatSessionStore
from sob_generator.llm.generator import LLMDraftGenerator
from sob_generator.llm.schemas import DocumentationEntry
from sob_generator.models import ClientData

class RecordingGenerator(LLMDraftGenerator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls=[]
    def _generate(self, prompt, model_cls, max_tokens):
        result=super()._generate(prompt, model_cls, max_tokens)
        self.calls.append({'schema':model_cls.__name__, 'prompt':prompt, 'output':result.value.model_dump(), 'error':result.error})
        return result

async def main():
    generator=RecordingGenerator('models/Qwen2.5-7B-Instruct-Q4_K_M.gguf', verbose=False)
    session=ChatSession(client=ClientData(name='Mustermann',vorname='Max',anrede='Herr'),phase=DialoguePhase.FIELD_REFLECT,active_section_id='1.1',sections={
      '1.1':SectionChatState(section_id='1.1',title='Wohnsituation',smarte_ziele='Herr Mustermann erhält seinen Wohnraum.'),
      '1.5':SectionChatState(section_id='1.5',title='Gesundheitsförderung und -erhaltung')})
    session.memory.section_entries={'1.1':[DocumentationEntry(datum='01.09.2026',kategorie='Wohnen',text='Herr Mustermann möchte seine Wohnung behalten. Er öffnet Briefe der Vermietung gemeinsam mit der Assistenz.')],'1.5':[]}
    store=ChatSessionStore();store.store(session)
    engine=ChatDialogueEngine(generator,session_store=store)
    output={'model':generator._model_path,'settings':generator._llama_kwargs,'repeat_penalty':generator._repeat_penalty,'turns':[]}
    start=time.monotonic()
    reply=await engine._goal_reply(session,engine.get_section_def('1.1'))
    session.add_assistant_message(reply)
    output['initial_reply']=reply
    for message in [
      'Beim letzten Besuch hat er den Brief selbst geöffnet und nach dem nächsten Schritt gefragt.',
      'Wie unterscheide ich eine Beobachtung von einer Interpretation?',
      'Warum schlägst du dieses Ziel vor?',
      'Das kann ich noch nicht beurteilen.',
      'Korrektur: Beim letzten Besuch hat die Assistenz den Brief geöffnet, nicht Herr Mustermann.',
      'Bitte die Ziele so ändern, dass er bis zum nächsten vereinbarten Gespräch einen Brief selbst öffnet. Den Termin haben wir noch nicht festgelegt.',
      'Letztes Jahr war er wegen Suizidgedanken in der Psychiatrie. Zur aktuellen Gefährdung habe ich keine Angaben.',
      'Er möchte Briefe zunächst gemeinsam besprechen.',
      'Entwurf erstellen',
      'Wechsel zu Feld 1.5',
      'Er vereinbart Arzttermine selbst telefonisch.',
      'Entwurf erstellen',
    ]:
      before=len(session.messages); t=time.monotonic()
      await engine.process_message(session.session_id,message)
      turn={'input':message,'elapsed_seconds':round(time.monotonic()-t,2),'replies':[m.content for m in session.messages[before:] if m.role.value=='assistant'],'phase':session.phase.value,'sections':{k:v.model_dump() for k,v in session.sections.items()},'report':session.to_report_draft().model_dump()}
      output['generator_calls']=generator.calls;output['turns'].append(turn);output['elapsed_seconds']=round(time.monotonic()-start,2)
      Path('/tmp/issue76_model.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
      print(message[:50],turn['elapsed_seconds'],flush=True)
asyncio.run(main())

```
