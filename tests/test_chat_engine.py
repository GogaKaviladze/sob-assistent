"""Unit tests for ChatDialogueEngine (Issue #53)."""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sob_generator.chat_engine import ChatDialogueEngine
from sob_generator.chat_session import DialoguePhase, SectionStatus
from sob_generator.chat_store import ChatSessionStore
from sob_generator.report_template import TEMPLATE_REGISTRY
from sob_generator.stub_generator import StubDraftGenerator

DUMMY_DIR = Path(__file__).resolve().parent.parent / "dummy"
CSV_PATH = DUMMY_DIR / "Hans_Peter_Vivendi_Export.csv"
DOCX_PATH = DUMMY_DIR / "Hans_Peter_SVB_ausgefuellt.docx"

pytestmark = pytest.mark.skipif(
    not CSV_PATH.exists(),
    reason="dummy/ ist lokal (gitignored)",
)


@pytest.fixture
def engine():
    generator = StubDraftGenerator()
    store = ChatSessionStore()
    return ChatDialogueEngine(generator=generator, session_store=store)


@pytest.mark.anyio
async def test_start_session_creates_valid_screening_phase(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, anrede="Herr")
    assert session is not None
    assert session.phase == DialoguePhase.SCREENING
    assert session.client.anrede == "Herr"
    assert session.client.name == "Peter"
    assert session.client.vorname == "Hans"
    assert len(session.messages) == 2
    assert "dich als Fachkraft" in session.messages[0].content
    assert "bestätigst jeden Abschnitt" in session.messages[0].content
    assert "Vorschlag" in session.messages[1].content


@pytest.mark.anyio
async def test_screening_adjustment_and_confirmation(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # 1. Adjust sections
    session = await engine.process_message(sid, "Bitte 1.3 aktivieren und 2.2 weglassen")
    assert session.phase == DialoguePhase.SCREENING
    assert session.sections["1.3"].is_active_field is True
    assert session.sections["2.2"].is_active_field is False

    # 2. Confirm screening
    session = await engine.process_message(sid, "Passt so, danke!")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    assert "Lebensfeld" in session.messages[-1].content


@pytest.mark.anyio
async def test_screening_development_note_condolence(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    session = await engine.process_message(sid, "Die Mutter ist leider kürzlich verstorben.")
    assert session.developments != ""
    assert "verstorben" in session.developments
    assert "Beileid" in session.messages[-1].content


@pytest.mark.anyio
async def test_field_progression(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # Screening confirm -> FIELD_REFLECT (Schritt 1: Hypothese + Ziele + Reflexionsfrage)
    session = await engine.process_message(sid, "Passt.")
    assert session.phase == DialoguePhase.FIELD_REFLECT

    # FIELD_REFLECT -> FIELD_QUESTIONS (Schritt 2: Vertiefungsfrage vor Entwurf)
    session = await engine.process_message(sid, "Klient kocht selbstständig.")
    assert session.phase == DialoguePhase.FIELD_QUESTIONS

    # FIELD_QUESTIONS -> FIELD_DRAFT (Schritt 3: Entwurf nach Vertiefungsantwort)
    session = await engine.process_message(sid, "Die Assistenz begleitet einmal wöchentlich bei der Planung.")
    assert session.phase == DialoguePhase.FIELD_DRAFT


@pytest.mark.anyio
async def test_confirm_section_cycles_or_finalizes(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # Confirm all active sections
    active_sids = [s for s, sec in session.sections.items() if sec.is_active_field]
    for current_sid in active_sids:
        session = await engine.confirm_section(sid, current_sid)
        assert session.sections[current_sid].status == SectionStatus.CONFIRMED

    assert session.phase == DialoguePhase.FINALIZED
    assert "Hervorragend" in session.messages[-1].content


@pytest.mark.anyio
async def test_select_section_and_export_markdown(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    session = engine.select_section(sid, "2.1")
    assert session.active_section_id == "2.1"

    md_content, filename = engine.export_markdown(sid)
    assert "Chatverlauf" in filename
    assert "# SOB Chatverlauf" in md_content


@pytest.mark.anyio
async def test_goal_critique_stays_in_field_goals_and_revises(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # Confirm screening -> FIELD_REFLECT
    session = await engine.process_message(sid, "Passt.")
    assert session.phase == DialoguePhase.FIELD_REFLECT

    # 1. Critique: "1. ziel ist nicht smart" -> MUST STAY in FIELD_REFLECT
    session = await engine.process_message(sid, "1. ziel ist nicht smart")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    assert "bisherige Stand bleibt erhalten" in session.messages[-1].content
    assert "1. ziel ist nicht smart" not in session.sections[session.active_section_id].user_notes
    assert "Rückfragen" not in session.messages[-1].content

    # 2. Second Critique: "ziel ist gut aber schreib es mir smart. genauer formuliert mit zeitlicher grenze"
    session = await engine.process_message(
        sid, "ziel ist gut aber schreib es mir smart. genauer formuliert mit zeitlicher grenze"
    )
    assert session.phase == DialoguePhase.FIELD_REFLECT
    assert "bisherige Stand bleibt erhalten" in session.messages[-1].content
    assert "1. ziel ist nicht smart" not in session.sections[session.active_section_id].user_notes
    assert "Rückfragen" not in session.messages[-1].content

    # 3. Finally confirm goals -> Advance to FIELD_DRAFT
    session = await engine.process_message(sid, "Passt so, danke!")
    assert session.phase == DialoguePhase.FIELD_DRAFT
    assert "Textentwurf" in session.messages[-1].content or "Vorschau" in session.messages[-1].content


@pytest.mark.anyio
async def test_screening_guidance_and_swap(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # 1. User inputs "ändern" -> Must provide guidance, NOT classify as development note
    session = await engine.process_message(sid, "ändern")
    assert session.phase == DialoguePhase.SCREENING
    assert "Einteilung" in session.messages[-1].content or "anpassen" in session.messages[-1].content
    assert "ändern" not in session.developments

    # 2. User inputs "einteilung ändern"
    session = await engine.process_message(sid, "einteilung ändern")
    assert session.phase == DialoguePhase.SCREENING
    assert "einteilung ändern" not in session.developments

    # 3. User swaps sections: "1.1 und 2.1 tauschen"
    session = await engine.process_message(sid, "1.1 und 2.1 tauschen")
    assert session.phase == DialoguePhase.SCREENING
    assert session.active_section_id == "2.1"
    assert session.sections["2.1"].is_active_field is True
    assert session.sections["1.1"].is_active_field is True
    assert "2.1" in session.messages[-1].content


@pytest.mark.anyio
async def test_domain_facts_in_dialogue_incorporated_into_draft(engine):
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # Screening confirm -> FIELD_REFLECT
    session = await engine.process_message(sid, "Passt.")
    assert session.phase == DialoguePhase.FIELD_REFLECT

    # User answers reflection question with specific domain detail -> advances to FIELD_QUESTIONS
    session = await engine.process_message(
        sid,
        "wäscht 1x wöchentlich, schaltet maschine auf die richtige temperatur und legt wasche zusammen mit bebilderung im schrank",
    )
    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    active_sec = session.sections[session.active_section_id]
    assert any("wäscht 1x wöchentlich" in note for note in active_sec.user_notes)
    assert not active_sec.bericht_zielerreichung  # Noch kein Entwurf in 2. Antwort

    # User answers deepening question -> generates FIELD_DRAFT
    session = await engine.process_message(sid, "Die Assistenz bebildert den Kleiderschrank zur Orientierung.")
    assert session.phase == DialoguePhase.FIELD_DRAFT

    # Verify both notes are preserved and flow into the refined section draft
    active_sec = session.sections[session.active_section_id]
    assert "wäscht 1x wöchentlich" in active_sec.bericht_zielerreichung
    assert "Die Assistenz bebildert den Kleiderschrank zur Orientierung." in active_sec.bericht_zielerreichung


@pytest.mark.anyio
async def test_screening_wohnen_deactivation_and_development_note_acceptance_criteria(engine):
    """Prüft Akzeptanzkriterium: 'wohnen ist erledigt der klient hat einen neue wohnung. kannst du deaktivieren'

    - Deaktivierung des Lebensfelds Wohnen (1.1 in Hamburg)
    - Aufnahme der Entwicklungsnotiz
    - Keine starren Floskeln ('antworte einfach kurz mit Passt')
    - Flexibler Übergang zu FIELD_REFLECT bei 'alles klar, weiter'
    """
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH)
    sid = session.session_id

    # 1. Fachkraft deaktiviert Wohnen mit Begründung (Lebensereignis neue Wohnung)
    session = await engine.process_message(
        sid, "wohnen ist erledigt der klient hat einen neue wohnung. kannst du deaktivieren"
    )
    assert session.phase == DialoguePhase.SCREENING
    assert session.sections["1.1"].is_active_field is False
    assert "wohnung" in session.developments.lower()
    last_msg = session.messages[-1].content
    assert "antworte einfach kurz mit" not in last_msg.lower()
    assert "wohnung" in last_msg.lower()

    # 2. Fachkraft bestätigt flexibel mit 'alles klar, weiter'
    session = await engine.process_message(sid, "alles klar, weiter")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    assert "Lebensfeld" in session.messages[-1].content


@pytest.mark.anyio
async def test_screening_op_de_wisch_template_section_4_wohnen(engine):
    """Prüft dynamisches Screening auf Op-de-Wisch-Vorlage:

    - 'wohnen' wird Lebensfeld 4 zugeordnet und deaktiviert
    - Antwort nennt korrekte Nummern (4) und keine Hamburg-Nummern (1.1, 2.2)
    - Phasenwechsel bei 'lass uns loslegen'
    """
    op_de_wisch_tmpl = TEMPLATE_REGISTRY["hamburg_op_de_wisch"]
    session = await engine.start_session(
        csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, template=op_de_wisch_tmpl
    )
    sid = session.session_id

    session = await engine.process_message(
        sid, "wohnen ist erledigt der klient hat einen neue wohnung. kannst du deaktivieren"
    )
    assert session.phase == DialoguePhase.SCREENING
    assert session.sections["4"].is_active_field is False
    assert "wohnung" in session.developments.lower()

    last_msg = session.messages[-1].content
    assert "4" in last_msg or "Wohnen" in last_msg
    assert "1.1" not in last_msg
    assert "2.2" not in last_msg
    assert "antworte einfach kurz mit" not in last_msg.lower()

    # Übergang mit 'lass uns loslegen'
    session = await engine.process_message(sid, "lass uns loslegen")
    assert session.phase == DialoguePhase.FIELD_REFLECT


@pytest.mark.anyio
async def test_crisis_validation_in_chat_avoids_robotic_response(engine):
    """Prüft Issue #71: Auf Akutkrisen (Psychose, Suizidalität, Psychiatrie) reagiert die Assistentin fachlich sensibel und validierend statt mit 'Danke für deine Einordnung!'."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, anrede="Herr")
    sid = session.session_id

    # Screening bestätigen
    session = await engine.process_message(sid, "Passt so, danke!")
    assert session.phase == DialoguePhase.FIELD_REFLECT

    # Fachkraft schildert psychiatrische Akutkrise
    crisis_msg = "er war in psychiatrie stationär aufgenommen und dort bedröt sich umzubringen in akute psychose"
    session = await engine.process_message(sid, crisis_msg)

    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    reply = session.messages[-1].content

    # Absolutes Verbot von technokratischem "Danke für deine Einordnung!" bei Akutkrisen
    assert "Danke für deine Einordnung" not in reply

    # Sensible und fachlich validierende Quittierung
    assert "heute" in reply
    assert "Krise" in reply
    assert "Schutzmaßnahmen" not in reply


@pytest.mark.anyio
async def test_lf1_psychische_verfassung_goals_and_anrede_convention(engine):
    """Prüft Issue #71: LF 1 in Op-de-Wisch Vorlage generiert Krisen-/Selbstsorgeziele und verwendet durchgehend 'Herr Peter'."""
    op_de_wisch_tmpl = TEMPLATE_REGISTRY["hamburg_op_de_wisch"]
    session = await engine.start_session(
        csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, anrede="Herr", template=op_de_wisch_tmpl
    )
    sid = session.session_id

    # Screening bestätigen -> Start bei LF 1: Psychische Verfassung / Selbstsorge
    session = await engine.process_message(sid, "Passt alles so!")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    assert session.active_section_id == "1"

    goal_msg = session.messages[-1].content
    assert "Psychische Verfassung / Selbstsorge" in goal_msg

    # Korrekte Anrede-Konvention für Herrn Peter (niemals informell bloß "Peter" oder "Hans")
    assert "Herr Peter" in goal_msg or "Wunsch der Person" in goal_msg
    assert " Peter " not in goal_msg or "Herr Peter" in goal_msg

    # Gegenstandsschärfe: Krisen- und Deeskalationsziele statt Beziehungs- oder Arbeitsdrift
    assert "Krisenpass" not in goal_msg  # No invented intervention from a field ID
    assert "Telefonate mit Verwandten" not in goal_msg
    assert "Besuchen bei der Mutter" not in goal_msg


@pytest.mark.anyio
async def test_multi_turn_living_draft_within_section(engine):
    """Prüft Issue #72: Multi-Turn Living Draft mit fortlaufender Aktualisierung und Vertiefungsfragen."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, anrede="Herr")
    sid = session.session_id

    # 1. Screening bestätigen -> FIELD_REFLECT
    session = await engine.process_message(sid, "Passt so, danke!")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    active_sid = session.active_section_id

    # 2. Turn 1: Fachkraft antwortet auf Reflexionsfrage -> Schritt 2: Vertiefungsfrage (FIELD_QUESTIONS, Entwurf noch leer)
    session = await engine.process_message(
        sid, "Herr Peter kocht sonntags gerne selbstständig, benötigt aber noch Unterstützung bei der Menüplanung."
    )
    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    sec_1 = session.sections[active_sid]
    assert sec_1.turn_count == 1
    assert not sec_1.bericht_zielerreichung
    last_msg = session.messages[-1].content
    assert last_msg.count("?") == 1
    assert "Vertiefungsfrage zur Konkretisierung:" not in last_msg

    # 3. Turn 2: Fachkraft beantwortet Vertiefungsfrage mit Hilfsmitteln -> Erzeugt Living Draft
    session = await engine.process_message(
        sid, "Als Hilfsmittel nutzt er ein bebildertes Kochbuch und eine Einkaufs-App auf dem Smartphone."
    )
    assert session.phase == DialoguePhase.FIELD_DRAFT
    assert sec_1.turn_count == 2
    # Living Draft aktualisiert sich inkrementell in der Vorschau
    assert "bebildertes kochbuch" in sec_1.bericht_zielerreichung.lower()
    assert "menüplanung" in sec_1.bericht_zielerreichung.lower()

    # 4. Turn 3: Fachkraft ergänzt Assistenzfrequenz
    session = await engine.process_message(
        sid, "Die Assistenz begleitet 2x monatlich beim Großeinkauf im Supermarkt."
    )
    assert session.phase == DialoguePhase.FIELD_DRAFT
    assert sec_1.turn_count == 3
    assert "großeinkauf" in sec_1.bericht_zielerreichung.lower()

    # 5. Turn 4: Fachkraft stellt eine Meta-Frage (darf den Entwurf nicht verschmutzen)
    session = await engine.process_message(
        sid, "Wie wird die Begleitung beim Einkauf üblicherweise im Bericht dokumentiert?"
    )
    assert session.phase == DialoguePhase.FIELD_DRAFT
    # Meta-Frage darf nicht in user_notes landen
    assert not any("Wie wird die Begleitung" in n for n in sec_1.user_notes)
    assert "Frage" in session.messages[-1].content

    # 6. Turn 5: Fachkraft sendet explizites Freigabe-Signal im Chat ("Abschnitt freigeben")
    session = await engine.process_message(sid, "Abschnitt freigeben")
    assert sec_1.status == SectionStatus.CONFIRMED
    assert session.active_section_id != active_sid
    assert "freigegeben" in session.messages[-2].content or "freigegeben" in session.messages[-1].content


@pytest.mark.anyio
async def test_strict_section_scoped_isolation_between_sections(engine):
    """Prüft Issue #72: Strikte Bereichs-Kontextkapselung beim Wechsel von Lebensfeld N zu N+1."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, anrede="Herr")
    sid = session.session_id

    # Screening bestätigen
    session = await engine.process_message(sid, "Passt so!")
    assert session.phase == DialoguePhase.FIELD_REFLECT
    sec1_id = session.active_section_id

    # Abschnitt 1 bearbeiten: Spezifische Notiz hinterlegen
    session = await engine.process_message(sid, "Klient wäscht 1x wöchentlich seine Wäsche im Keller.")
    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    assert session.sections[sec1_id].user_notes == ["Klient wäscht 1x wöchentlich seine Wäsche im Keller."]

    # Abschnitt 1 freigeben -> Übergang zu Abschnitt 2 (confirm_section generiert Entwurf automatisch)
    session = await engine.confirm_section(sid, sec1_id)
    assert "wäsche im keller" in session.sections[sec1_id].bericht_zielerreichung.lower()
    sec2_id = session.active_section_id
    assert sec2_id != sec1_id
    assert session.sections[sec1_id].status == SectionStatus.CONFIRMED

    # Strikte Bereichsisolation vor ersten Eingaben in Abschnitt 2 prüfen:
    assert session.sections[sec2_id].user_notes == []
    assert session.sections[sec2_id].turn_count == 0
    assert session.get_section_notes(sec2_id) == []

    # Erste Notiz in Abschnitt 2 erfassen
    session = await engine.process_message(sid, "Erhält Taschengeld wöchentlich bar von der Assistenz.")
    sec2 = session.sections[sec2_id]
    assert sec2.user_notes == ["Erhält Taschengeld wöchentlich bar von der Assistenz."]
    assert session.phase == DialoguePhase.FIELD_QUESTIONS

    # Abschnitt 2 freigeben -> Entwurf wird befüllt
    session = await engine.confirm_section(sid, sec2_id)
    assert "taschengeld wöchentlich bar" in sec2.bericht_zielerreichung.lower()

    # KEINERLEI Verschleppung aus Abschnitt 1:
    assert "wäsche im keller" not in sec2.bericht_zielerreichung.lower()
    assert not any("wäsche" in n.lower() for n in sec2.user_notes)

    # Scoped messages prüfen
    sec2_msgs = session.get_section_messages(sec2_id)
    assert all(m.section_id == sec2_id for m in sec2_msgs)


@pytest.mark.anyio
async def test_selective_global_development_transfer_during_section_turn(engine):
    """Prüft Issue #72: Biografische Lebensereignisse während eines Lebensfeld-Turns fließen in developments ein."""
    session = await engine.start_session(csv_path=CSV_PATH, alt_sob_path=DOCX_PATH, anrede="Herr")
    sid = session.session_id

    # Screening bestätigen
    session = await engine.process_message(sid, "Passt.")
    active_sid = session.active_section_id

    # Im Dialog des Lebensfelds wird ein akutes Lebensereignis erwähnt
    session = await engine.process_message(
        sid, "Er war im Juli für 3 Wochen stationär in der Psychiatrie nach einer psychotischen Krise."
    )
    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    # Globales Lebensereignis muss in session.developments erfasst sein
    assert "stationär" in session.developments.lower() or "psychiatrie" in session.developments.lower()

    # Nach Freigabe und Wechsel zum nächsten Lebensfeld:
    session = await engine.confirm_section(sid, active_sid)
    next_sid = session.active_section_id
    assert next_sid != active_sid

    # Das Lebensereignis bleibt global erhalten
    assert "stationär" in session.developments.lower() or "psychiatrie" in session.developments.lower()
    # Der scoped Dialog von active_sid ist nicht in next_sid
    assert session.sections[next_sid].user_notes == []


