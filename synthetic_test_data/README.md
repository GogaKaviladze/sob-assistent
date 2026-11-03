# Synthetische Testdaten (Synthetic Test Data)

Dieser Ordner enthält strukturierte, **100 % synthetische Beispieldaten** zum manuellen Testen, für Vorführungen und zur Abnahme der Chat-App (`chat_app`).

> 🔒 **DSGVO & Datenschutz:**  
> Alle enthaltenen Namen, Geburtsdaten, Adressen und Verlaufsberichte sind frei erfunden. Es handelt sich um synthetische Testdatensätze ohne realen Personenbezug.

---

## 📁 Ordnerstruktur & Testfälle

```
synthetic_test_data/
├── 01_fall_hans_peter/
│   ├── Hans_Peter_Vivendi_Export.csv         # Vivendi PD Verlaufsbericht (57 Einträge, 2024–2026)
│   ├── Hans_Peter_Vorbericht_Hamburg.docx    # Ausgefüllter Hamburger Vorbericht (ASP)
│   └── Hans_Peter_Vorbericht_OpDeWisch.docx  # Ausgefüllter Op-de-Wisch Vorbericht
├── 02_fall_hans_zimmer/
│   ├── Hans_Zimmer_Vivendi_Export.csv        # Weiterer Vivendi PD Export (für Fall-Konflikt-Tests)
│   └── Hans_Zimmer_Vorbericht_Hamburg.docx   # Zugehöriger Vorbericht Hamburg
├── 03_fall_max_mustermann/
│   ├── Max_Mustermann_Vivendi_Export.csv     # Vivendi PD Export (Alltag & Haushalt)
│   └── Max_Mustermann_Vorbericht_Hamburg.docx# Zugehöriger Vorbericht Hamburg
└── 04_edge_cases/
    ├── export_neutral_name.csv               # Vivendi-Export ohne Namen im Dateinamen (prüft Erkennung aus Zeile 4)
    ├── dienstplan_generisch.csv              # Reguläre CSV (kein Vivendi; prüft Fallback/Standard-CSV-Handling)
    └── vivendi_korrupt_ohne_daten.csv        # Vivendi-Header ohne Einträge (prüft Fail-Closed-Verhalten)
```

---

## 🧪 Verwendung in der Web-App (`http://127.0.0.1:8000`)

### Szenario 1: Vollständiger Standardfall (Hamburg)
1. **Plan laden:** `01_fall_hans_peter/Hans_Peter_Vorbericht_Hamburg.docx`
2. **CSV laden:** `01_fall_hans_peter/Hans_Peter_Vivendi_Export.csv`
3. **Ergebnis:** 
   - Klient*in: `Hans Peter`
   - CSV-Status: `57 Einträge aktiv`
   - Plan: Ziele und Entwicklungen aus allen Lebensbereichen geladen.

### Szenario 2: Namenserkennung aus Dokumentendaten (Issue #135)
1. **CSV laden:** `04_edge_cases/export_neutral_name.csv`
2. **Ergebnis:**
   - Obwohl die Datei `export_neutral_name.csv` heißt, zeigt die Fach-Karte `Hans Peter`, da der Name direkt aus Zeile 4 des Vivendi-Exports extrahiert wird.

### Szenario 3: Reguläre CSV (Kein Vivendi-Verlust)
1. **CSV laden:** `04_edge_cases/dienstplan_generisch.csv`
2. **Ergebnis:**
   - Datei wird als `Verlaufsdokumentation` geladen.
   - Alle Datensätze bleiben erhalten und werden nicht fälschlich als Vivendi fragmentiert.

### Szenario 4: Fallwechsel & Konfliktprüfung (Issue #133)
1. Lade `01_fall_hans_peter/Hans_Peter_Vivendi_Export.csv` hoch.
2. Lade anschließend `02_fall_hans_zimmer/Hans_Zimmer_Vivendi_Export.csv` hoch.
3. **Ergebnis:**
   - Die App erkennt den Klientenwechsel und fordert eine Bestätigung zur Bereinigung des vorherigen Falls an (Hard Reset).
