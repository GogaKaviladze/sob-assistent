"""Tests for 2-Schritt-Fachberatungs-Modus (Issue #60).

Verifies:
- 2-step dialogue progression per section (FIELD_REFLECT -> FIELD_DRAFT -> CONFIRM)
- Hypothesis + 2 SMART goals + 1-2 reflection questions in Schritt 1
- High priority integration of professional reflections into the report text
- Consistent collegial 'Du' address throughout the chat
"""
from pathlib import Path
import pytest

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import DialoguePhase, SectionStatus
from sob_generator.chat_store import ChatSessionStore
from sob_generator.dialogue_router import DialogueIntent, DialogueIntentRouter
from sob_generator.llm.prompts import (
    build_field_goal_clarification_prompt,
    build_refine_section_prompt,
    build_section_prompt,
)
from sob_generator.llm.schemas import DocumentationEntry
from sob_generator.llm.sections import SectionDefinition
from sob_generator.stub_generator import StubDraftGenerator

DUMMY_DIR = Path(__file__).resolve().parent.parent / "dummy"
CSV_PATH = DUMMY_DIR / "Hans_Peter_Vivendi_Export.csv"
DOCX_PATH = DUMMY_DIR / "Hans_Peter_SVB_ausgefuellt.docx"


@pytest.fixture
def engine():
    if not CSV_PATH.exists():
        pytest.skip("dummy/ ist lokal (gitignored)")
    generator = StubDraftGenerator()
    store = ChatSessionStore()
    return ChatDialogueEngine(generator=generator, session_store=store)


@pytest.mark.anyio
async def test_fachberatung_two_step_roundtrip(engine):
    """Prüft, dass der Lebensfeld-Durchlauf über Reflexion und Vertiefungsfrage zum fertigen Entwurf führt."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # 1. Screening bestätigen -> Schritt 1: FIELD_REFLECT
    session = await engine.process_message(sid, "Passt so!")
    assert session.phase == DialoguePhase.FIELD_REFLECT

    step1_msg = session.messages[-1].content
    assert "Fachliche Hypothese" not in step1_msg
    assert not session.sections[session.active_section_id].proposed_goals  # Stub invents no goals
    assert step1_msg.count("?") == 1

    # 2. Fachkraft antwortet mit Reflexion -> Schritt 2: FIELD_QUESTIONS (Text noch NICHT ausgefüllt)
    reflection_input = "Am Anfang gehemmt, inzwischen selbstständig mit Freunden, aber Rückzug bei Fremden."
    session = await engine.process_message(sid, reflection_input)
    assert session.phase == DialoguePhase.FIELD_QUESTIONS

    active_sec = session.sections[session.active_section_id]
    assert not active_sec.bericht_zielerreichung  # Nicht schon in 2. Antwort ganzer Text ausgefüllt!
    step2_msg = session.messages[-1].content
    assert reflection_input in step2_msg
    assert "Danke für deine Einordnung" not in step2_msg
    assert step2_msg.count("?") == 1

    # 3. Fachkraft beantwortet Vertiefungsfrage -> Schritt 3: FIELD_DRAFT generiert
    session = await engine.process_message(sid, "Begleitung im Gruppenraum gibt ihm Sicherheit.")
    assert session.phase == DialoguePhase.FIELD_DRAFT

    # Fachkraft-Antwort muss prominent im Fließtext enthalten sein
    assert reflection_input in active_sec.bericht_zielerreichung

    # Assistentin spiegelt die Dynamik fachlich wider und verweist auf die Vorschau
    step3_msg = session.messages[-1].content
    assert "Vorschau" in step3_msg
    assert "prüfe" in step3_msg

    # 4. Freigabe durch Fachkraft
    session = await engine.confirm_section(sid, session.active_section_id)
    assert active_sec.status == SectionStatus.CONFIRMED


def test_collegial_du_in_prompts_and_clarification():
    """Prüft, dass Prompts und Klarstellungen konsequent das kollegiale 'Du' erzwingen."""
    sec = SectionDefinition(index=0, id="2.1", title="Soziale Beziehungen", description="Kontakte")
    entries = [DocumentationEntry(datum="01.03.2026", kategorie="", text="Kegelgruppe besucht")]

    prompt = build_field_goal_clarification_prompt(
        section=sec,
        entries=entries,
        anrede="Herr",
        nachname="Peter",
        vorname="Hans",
    )

    assert "Schritt 1 von 2" in prompt
    assert "Duz die Fachkraft durchgehend und kollegial" in prompt
    assert "Reflexionsfrage" in prompt


def test_refine_prompt_gives_highest_priority_to_fachkraft_input():
    """Prüft, dass Chat-Eingaben der Fachkraft höchste Priorität im Refine-Prompt erhalten."""
    sec = SectionDefinition(index=0, id="1.1", title="Wohnen", description="Wohnen")
    entries = [DocumentationEntry(datum="01.03.2026", kategorie="", text="Miete gezahlt")]

    user_note = "Zieht sich bei Konflikten im Hausflur stark zurück."
    prompt = build_refine_section_prompt(
        section=sec,
        entries=entries,
        user_notes=[user_note],
        anrede="Herr",
        nachname="Peter",
        vorname="Hans",
    )

    assert "HÖCHSTE PRIORITÄT FÜR DEN BERICHT" in prompt
    assert "korrigierte Angabe ersetzt die frühere Aussage" in prompt
    assert user_note in prompt


def test_router_classifies_reflection_answer_in_field_reflect():
    """Prüft, dass inhaltliche Antworten auf die Reflexionsfrage als ADDITIONAL_INFO geroutet werden."""
    router = DialogueIntentRouter()

    reflection_text = "Herr Peter dockt in der Gruppe gut an, braucht aber im 1:1 Gespräch noch emotionale Rückversicherung."
    res = router.classify(reflection_text, DialoguePhase.FIELD_REFLECT)
    assert res.intent == DialogueIntent.ADDITIONAL_INFO
    assert res.extracted_domain_fact == reflection_text


def test_clean_dialogue_response_strips_letter_boilerplate_and_smart_brackets():
    """Prüft, dass Briefanreden, Signaturen, Platzhalter und Akronym-Klammern bereinigt werden."""
    from sob_generator.llm.generator import clean_dialogue_response

    raw_llm_output = (
        "Liebe Kollegin,\n"
        "1. Verlaufshypothese: Herr Peter zeigt anhaltendes Interesse und Bemühen, seine Wohnung zu pflegen.\n"
        "2. SMART-Ziele:\n"
        "a) Bis zum 31.05.2026 reinigt Herr Peter seine Wohnung einmal wöchentlich "
        "(Spezifisch: einmalig, Messbar: regelmäßig, Akzeptabel: selbstständig, Realistisch: machbar, Zeitbegrenzt: bis 31.05.2026).\n"
        "3. Reflexionsfragen:\n"
        "- Frage an dich: Wie erlebst du die Zusammenarbeit?\n"
        "Liebe Grüße\n"
        "[Dein Name]"
    )

    cleaned = clean_dialogue_response(raw_llm_output)

    assert "Liebe Kollegin" not in cleaned
    assert "Liebe Grüße" not in cleaned
    assert "[Dein Name]" not in cleaned
    assert "(Spezifisch:" not in cleaned
    assert "Messbar:" not in cleaned
    assert "1. Verlaufshypothese: Herr Peter zeigt anhaltendes Interesse" in cleaned
    assert "reinigt Herr Peter seine Wohnung einmal wöchentlich." in cleaned or "reinigt Herr Peter seine Wohnung einmal wöchentlich" in cleaned
    assert "Frage an dich: Wie erlebst du die Zusammenarbeit?" in cleaned


def test_llm_draft_generator_implements_conversational_protocol():
    """Stellt sicher, dass LLMDraftGenerator das DraftGenerator-Protokoll und alle Dialog-Methoden erfüllt."""
    from sob_generator.draft_generator import DraftGenerator
    from sob_generator.llm.generator import LLMDraftGenerator

    assert issubclass(LLMDraftGenerator, DraftGenerator)
    assert hasattr(LLMDraftGenerator, "generate_field_goals")
    assert hasattr(LLMDraftGenerator, "revise_field_goals")
    assert hasattr(LLMDraftGenerator, "generate_field_questions")
    assert hasattr(LLMDraftGenerator, "classify_intent")
    assert callable(getattr(LLMDraftGenerator, "generate_field_goals"))
    assert callable(getattr(LLMDraftGenerator, "revise_field_goals"))
    assert callable(getattr(LLMDraftGenerator, "generate_field_questions"))
    assert callable(getattr(LLMDraftGenerator, "classify_intent"))


@pytest.mark.anyio
async def test_screening_passt_nicht_does_not_confirm_and_provides_guidance(engine):
    """'passt nicht' im Screening darf keinesfalls zu FIELD_REFLECT führen, sondern muss nachfragen."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id
    assert session.phase == DialoguePhase.SCREENING

    session = await engine.process_message(sid, "passt nicht")
    # Phase MUSS im SCREENING bleiben!
    assert session.phase == DialoguePhase.SCREENING

    msg = session.messages[-1].content
    assert "Einteilung" in msg or "Lebensfelder" in msg
    assert "ändern" in msg or "anpassen" in msg

    # Erst bei echter Zustimmung ("Passt so") darf gewechselt werden
    session = await engine.process_message(sid, "Jetzt passt so!")
    assert session.phase == DialoguePhase.FIELD_REFLECT


def test_clean_dialogue_response_strips_meta_comments_and_kommentar_blocks():
    """Prüft, dass versehentliche **Kommentar:**-Blöcke und didaktische Erläuterungen am Ende bereinigt werden."""
    from sob_generator.llm.generator import clean_dialogue_response

    output_with_meta = (
        "1. Verlaufshypothese: Herr Peter stabilisiert seine Selbstversorgung im Wohnbereich stetig.\n"
        "2. SMART-Ziele:\n"
        "- Herr Peter bereitet an 3 Tagen wöchentlich selbstständig eine Mahlzeit zu.\n"
        "- Herr Peter trennt den anfallenden Müll eigenverantwortlich.\n"
        "3. Reflexionsfrage an dich:\n"
        "Welche Assistenzimpulse unterstützen ihn beim Kochen am besten?\n\n"
        "**Kommentar:** Die Reflexionsfragen zielen darauf ab, den konkreten pädagogischen Unterstützungsbedarf für die Assistenzplanung zu präzisieren."
    )

    cleaned = clean_dialogue_response(output_with_meta)

    assert "**Kommentar:**" not in cleaned
    assert "Die Reflexionsfragen zielen darauf ab" not in cleaned
    assert "Welche Assistenzimpulse unterstützen ihn beim Kochen am besten?" in cleaned
    assert "1. Verlaufshypothese:" in cleaned


def test_field_goal_prompt_forbids_meta_comments_and_restricts_to_three_sections():
    """Prüft, dass der Prompt für die Zielklärung Meta-Kommentare explizit verbietet und auf 3 Abschnitte limitiert."""
    sec = SectionDefinition(index=0, id="1.1", title="Wohnen", description="Wohnen")
    prompt = build_field_goal_clarification_prompt(
        section=sec,
        entries=[],
        anrede="Herr",
        nachname="Muster",
        vorname="Max",
    )

    assert "Strenges Verbot von Meta-Kommentaren" in prompt
    assert "Gib AUSSCHLIESSLICH genau diese 3 Abschnitte aus" in prompt
    assert "Die Reflexionsfragen zielen darauf ab" in prompt  # Mentioned in negative constraint


def test_extract_single_question_prunes_question_batteries_to_single_item():
    """Prüft, dass extract_single_question ausufernde 4-Fragen-Batterien strikt auf genau 1 Frage eindampft."""
    from sob_generator.llm.generator import extract_single_question

    raw_user_reported_wall = (
        "1. Wie erlebt du die zunehmende Selbstständigkeit bei den Telefonaten mit deiner Familie? "
        "Welche konkreten Unterstützungsmöglichkeiten kann ich dir anbieten, um sicherzustellen, dass du auch in Zukunft selbstständig und unangestrengt telefonieren kannst?\n"
        "2. Welche speziellen Strategien nutzt du zur Reizregulation vor Stresssituationen wie dem bevorstehenden Weihnachtsbesuch oder den Konflikten mit Kollegen? "
        "Wie können wir gemeinsam herausfinden, welche weiterführenden Hilfen dir in solchen Situationen helfen könnten, um deine psychische Stabilität zu gewährleisten und potentielle Überforderungen frühzeitig abzufangen?\n"
        "3. Welche Dynamiken erlebst du im Umgang mit deinen Kollegen? Kannst du bestimmte Merkmale oder Verhaltensweisen benennen, die eine positive Interaktion fördern oder deren Fehlen einen Konflikt auslöst? "
        "Wie können wir diese Erkenntnisse nutzen, um deine interpersonellen Situationen zu verbessern und sicherzustellen, dass du auch in Zukunft selbstständig mit Herausforderungen umgehen kannst?\n"
        "4. Welche konkreten Handlungsschritte hast du im Notfallplan eingetragen? "
        "Wie können wir sicherstellen, dass dieser Plan immer aktuell ist und du ihn jederzeit einsetzen kannst, falls du wieder in eine akute psychische Krise gerätst?"
    )

    # 1. Ohne Krise -> Nimmt Frage 1 (und schneidet Folgefragen 2, 3, 4 sowie Zweitsatz ab)
    single_default = extract_single_question(raw_user_reported_wall, prefer_crisis=False)
    assert single_default.startswith("Wie erlebt du")
    assert "Weihnachtsbesuch" not in single_default
    assert "Kollegen" not in single_default
    assert "Notfallplan" not in single_default
    assert single_default.count("?") == 1

    # 2. Mit Krise -> Selektiert gezielt die Notfall-/Krisenfrage aus der Batterie
    single_crisis = extract_single_question(raw_user_reported_wall, prefer_crisis=True)
    assert "Notfallplan" in single_crisis
    assert single_crisis.startswith("Welche konkreten Handlungsschritte hast du im Notfallplan eingetragen")
    assert "Familie" not in single_crisis
    assert "Weihnachtsbesuch" not in single_crisis
    assert single_crisis.count("?") == 1


def test_field_questions_prompt_enforces_single_question_and_brevity():
    """Prüft, dass der Prompt für den Living Draft strikt genau eine Frage und Chat-Kompaktheit verlangt."""
    from sob_generator.llm.prompts import build_field_questions_prompt

    sec = SectionDefinition(index=0, id="1.4", title="Psychische Verfassung", description="Krisen")
    prompt = build_field_questions_prompt(
        section=sec,
        entries=[],
        anrede="Herr",
        nachname="Muster",
        vorname="Max",
        is_living_draft=True,
    )

    assert "STRENGES SINGLE-QUESTION-PRINZIP" in prompt
    assert "STRENG GENAU EINE FRAGE" in prompt
    assert "Maximal 35 Wörter" in prompt


@pytest.mark.anyio
async def test_living_draft_assistant_message_is_concise_and_dialogic(engine):
    """Prüft, dass die Nachricht kurz und dialogisch bleibt und der Entwurf erst in Turn 3 generiert wird."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id
    await engine.process_message(sid, "Passt so!")

    # 1. Antwort der Fachkraft (Reflexion) -> Turn 2 (FIELD_QUESTIONS, Entwurf noch NICHT ausgefüllt)
    session = await engine.process_message(sid, "Klient kocht sonntags gerne einfache Gerichte.")
    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    assert not session.sections[session.active_section_id].bericht_zielerreichung
    last_msg = session.messages[-1].content

    # Keine Textwüsten: Chat-Nachricht muss prägnant und dialogisch sein (< 80 Wörter)
    words = last_msg.split()
    assert len(words) < 80
    assert last_msg.count("?") == 1
    assert "Vertiefungsfrage zur Konkretisierung:" not in last_msg

    # 2. Antwort der Fachkraft (Vertiefung) -> Turn 3 (FIELD_DRAFT, jetzt Entwurf ausgefüllt)
    session = await engine.process_message(sid, "Er nutzt bebilderte Rezeptkarten.")
    assert session.phase == DialoguePhase.FIELD_DRAFT
    assert session.sections[session.active_section_id].bericht_zielerreichung
    step3_msg = session.messages[-1].content
    assert len(step3_msg.split()) < 80
    assert "Vorschau" in step3_msg
    assert "prüfe" in step3_msg



