"""Public section context and conservative fallback for conversational turns."""
import re

from sob_generator.llm.schemas import DialogueTurnOutput


def is_uncertain(message: str) -> bool:
    return bool(re.search(r"(?:weiß|wissen|weiss).*nicht|(?:noch nicht|nicht sicher|unklar|unbekannt|keine Angaben|keine Ahnung|kann.*nicht.*beurteilen)", message, re.I))


def fallback_turn(context: dict) -> DialogueTurnOutput:
    """Never synthesize new client facts when model conversation is unavailable."""
    message = context["message"]
    lower = message.lower()
    notes = context.get("notes", [])
    pending = context.get("pending_question", "")
    question = f"Welche konkrete Unterstützung ist im Bereich {context['field']} beobachtet worden?"
    note = message if context.get("intent") in ("additional_info", "confirmation", "development_update") else ""
    supersedes = []
    sufficient = False
    if "beobachtung" in lower and ("interpretation" in lower or "beschreiben" in lower):
        reply = ("Eine Beobachtung beschreibt wahrnehmbares Verhalten in einer konkreten Situation. "
                 "Eine Interpretation ordnet dieses Verhalten fachlich ein und bleibt als Hypothese kenntlich. "
                 "Zum Beispiel wäre ‚sagte den Termin ab‘ eine Beobachtung; ‚vermeidet Kontakt‘ wäre eine Deutung. "
                 "Das ist nur ein Beispiel, keine Angabe zur betreuten Person.")
        question = "Welche konkrete Situation möchtest du beschreiben?"
    elif "warum" in lower and "ziel" in lower:
        entries = context.get("entries", [])
        if entries and context.get("goals"):
            reply = (f"Der aktuelle Zielvorschlag lautet: {context['goals']} "
                     f"Als Dokumentation liegt unter anderem vor: {entries[-1]['text']} "
                     "Ob daraus dieses Ziel folgt, muss fachlich geprüft werden; eine Beobachtung allein belegt noch keinen Wunsch der Person.")
        else:
            reply = "Mir fehlt eine belastbare Grundlage, um dieses Ziel fallbezogen zu begründen. Ein Zielvorschlag ist noch keine bestätigte Vereinbarung."
        question = "Welcher Wunsch der Person soll mit dem Ziel aufgegriffen werden?"
    elif re.search(r"suizid|psychiatri|selbstgefährd|psychos|umbringen", lower):
        historical = bool(re.search(r"damals|letztes jahr|früher|vor \w+ (?:monaten|jahren)|war .*stationär", lower))
        reply = ("Du beschreibst eine vergangene Krise; daraus lässt sich keine aktuelle Gefährdung ableiten." if historical else
                 "Die Angabe zur Krise erfordert eine Klärung der aktuellen Situation. Bei unmittelbarer Gefahr hat Hilfe vor Ort Vorrang vor der Berichtsarbeit.")
        question = "Wie ist die Situation heute einzuschätzen?"
    elif is_uncertain(message):
        reply = "Die Einschätzung bleibt offen. Du kannst zunächst eine beobachtete Situation schildern oder ausdrücklich einen Entwurf mit dieser Informationslücke anfordern."
        question = "Welche einzelne Situation kannst du konkret beschreiben, ohne den gesamten Verlauf zu bewerten?"
    elif note:
        # Explicit correction of the immediately preceding note; ambiguous corrections
        # remain open for the model or professional to resolve.
        if lower.startswith("korrektur:") and notes:
            target = correction_target(message, notes)
            supersedes = [target] if target is not None else []
        reply = f"Du beschreibst: „{message}“"
        sufficient = bool(pending and len(message.split()) >= 3 and bool(re.search(r"assistenz|begleit|unterstütz|hilfsmittel|nutzt|erinnert|beisein|sicherheit|rezept", lower)) and not supersedes and message not in notes)
        if sufficient:
            question = ""
            reply += " Damit lässt sich ein vorläufiger Text formulieren; offene Angaben bleiben zur Prüfung kenntlich."
    else:
        reply = "Für eine fallbezogene Antwort fehlt mir noch die konkrete Einordnung deiner Frage."
    return DialogueTurnOutput(reply=f"{reply} {question}".strip(), public_note=note,
        supersedes=supersedes, pending_question=question, sufficient=sufficient)


def correction_target(message: str, notes: list[str]):
    """Resolve only a unique lexical match; never delete an unrelated note."""
    stopwords = {"der", "die", "das", "den", "dem", "ein", "eine", "einen", "hat", "und", "nicht", "korrektur", "herr", "sie", "er"}
    words = lambda text: set(re.findall(r"\w+", text.lower())) - stopwords
    target_words = words(message)
    scores = [len(target_words & words(note)) for note in notes]
    best = max(scores, default=0)
    if best >= 3 and scores.count(best) == 1:
        return scores.index(best)
    return None
