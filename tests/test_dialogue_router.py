"""Unit tests for DialogueIntentRouter (Issue #58)."""
import pytest

from sob_generator.chat_session import DialoguePhase
from sob_generator.dialogue_router import DialogueIntent, DialogueIntentRouter


@pytest.fixture
def router():
    return DialogueIntentRouter()


VALID_SIDS = {"1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "3"}


# --- Screening Phase Tests ---


def test_screening_guidance_on_generic_change_request(router):
    for prompt in ("ändern", "einteilung ändern", "aufteilung ändern", "lebensfelder anpassen", "wie kann ich ändern"):
        res = router.classify(prompt, DialoguePhase.SCREENING, VALID_SIDS)
        assert res.intent == DialogueIntent.SCREENING_GUIDANCE, f"Failed for {prompt}"


def test_screening_swap_detection(router):
    for prompt in ("1.1 und 2.1 tauschen", "tausche 1.1 und 2.1", "1.1 mit 2.1 tauschen", "2.1 vor 1.1"):
        res = router.classify(prompt, DialoguePhase.SCREENING, VALID_SIDS)
        assert res.intent == DialogueIntent.SCREENING_SWAP, f"Failed for {prompt}"
        assert res.swap_pair in (("1.1", "2.1"), ("2.1", "1.1"))


def test_screening_section_adjust(router):
    res = router.classify("1.3 aktivieren und 2.2 weglassen", DialoguePhase.SCREENING, VALID_SIDS)
    assert res.intent == DialogueIntent.SCREENING_ADJUST
    assert res.to_activate == ["1.3"]
    assert res.to_deactivate == ["2.2"]
    assert res.has_confirmation is False


def test_screening_life_events(router):
    res_sensitive = router.classify("Die Mutter ist leider kürzlich verstorben.", DialoguePhase.SCREENING, VALID_SIDS)
    assert res_sensitive.intent == DialogueIntent.DEVELOPMENT_UPDATE
    assert res_sensitive.is_sensitive is True

    res_general = router.classify("Klient ist umgezogen in eine neue Wohnung.", DialoguePhase.SCREENING, VALID_SIDS)
    assert res_general.intent == DialogueIntent.DEVELOPMENT_UPDATE
    assert res_general.is_sensitive is False


def test_screening_confirmation(router):
    for prompt in ("passt", "passt so", "passt, weiter", "passt weiter", "start", "los", "einverstanden", "alles gut"):
        res = router.classify(prompt, DialoguePhase.SCREENING, VALID_SIDS)
        assert res.intent == DialogueIntent.CONFIRMATION, f"Failed for {prompt}"
        assert res.has_confirmation is True


# --- Field Goals Phase Tests ---


def test_field_goals_critique_and_revision(router):
    critiques = [
        "1. ziel ist nicht smart",
        "ziel ist gut aber schreib es mir smart. genauer formuliert mit zeitlicher grenze",
        "mach es smarter",
        "zeitliche grenze fehlt",
        "überarbeiten bitte",
        "ändere ziel 1",
        "nicht ziel 1, sondern anderes ziel",
        "zu allgemein formuliert",
        "konkreter fassen",
    ]
    for prompt in critiques:
        res = router.classify(prompt, DialoguePhase.FIELD_GOALS)
        assert res.intent == DialogueIntent.REVISION_REQUEST, f"Failed for {prompt}"


def test_field_goals_confirmation(router):
    for prompt in ("passt", "passt, weiter", "passt weiter", "passt so, weiter", "ziele passen so", "ziele sind super", "stimmig", "einverstanden", "weiter"):
        res = router.classify(prompt, DialoguePhase.FIELD_GOALS)
        assert res.intent == DialogueIntent.CONFIRMATION, f"Failed for {prompt}"
        assert res.has_confirmation is True
        assert res.extracted_domain_fact == ""


def test_field_goals_confirmation_with_domain_fact(router):
    res = router.classify(
        "Ziele passen so, Schwerpunkt ist Supermarkt-Training und Einkauf.",
        DialoguePhase.FIELD_GOALS,
    )
    assert res.intent == DialogueIntent.CONFIRMATION
    assert res.has_confirmation is True
    assert "Supermarkt-Training" in res.extracted_domain_fact


def test_field_goals_additional_info(router):
    res = router.classify(
        "wäscht 1x wöchentlich, schaltet maschine auf die richtige temperatur und legt wasche zusammen",
        DialoguePhase.FIELD_GOALS,
    )
    assert res.intent == DialogueIntent.ADDITIONAL_INFO
    assert "wäscht 1x wöchentlich" in res.extracted_domain_fact


def test_field_goals_clarification_question(router):
    res = router.classify("Was bedeutet SMART in diesem Zusammenhang?", DialoguePhase.FIELD_GOALS)
    assert res.intent == DialogueIntent.CLARIFICATION_QUESTION


# --- Field Questions Phase Tests ---


def test_field_questions_additional_info_and_confirmation(router):
    res_info = router.classify(
        "1. Begleitung wird als verlässliches Geländer erlebt. 2. Fortschritte beim Kochen.",
        DialoguePhase.FIELD_QUESTIONS,
    )
    assert res_info.intent == DialogueIntent.ADDITIONAL_INFO

    res_confirm = router.classify("Passt so, weiter zum Entwurf", DialoguePhase.FIELD_QUESTIONS)
    assert res_confirm.intent == DialogueIntent.CONFIRMATION

    res_rev = router.classify("Ziele ändern", DialoguePhase.FIELD_QUESTIONS)
    assert res_rev.intent == DialogueIntent.REVISION_REQUEST


# --- Negation & Conversational Routing Tests (Issue #60 Follow-Up) ---


def test_passt_nicht_is_never_confirmation_in_any_phase(router):
    """'passt nicht' darf in KEINER Phase als CONFIRMATION gewertet werden."""
    for phase in DialoguePhase:
        res = router.classify("passt nicht", phase, VALID_SIDS)
        assert res.intent != DialogueIntent.CONFIRMATION, f"'passt nicht' became CONFIRMATION in {phase}"
        assert res.has_confirmation is False, f"'passt nicht' had has_confirmation=True in {phase}"


def test_negations_reliably_detected_and_rejected_as_confirmation(router):
    """Prüft, dass diverse Negationen zuverlässig erkannt werden und nie CONFIRMATION sind."""
    negations = [
        "nicht",
        "keineswegs",
        "überhaupt nicht",
        "stimmt nicht",
        "nein",
        "gar nicht",
        "nicht einverstanden",
        "passt mir gar nicht",
        "stimmt so keineswegs",
        "überhaupt nicht passend",
    ]
    for prompt in negations:
        # In screening: must be guidance
        res_screening = router.classify(prompt, DialoguePhase.SCREENING, VALID_SIDS)
        assert res_screening.intent == DialogueIntent.SCREENING_GUIDANCE, f"Expected SCREENING_GUIDANCE for '{prompt}', got {res_screening.intent}"
        assert res_screening.has_confirmation is False

        # In goals/reflect: must be revision request
        res_goals = router.classify(prompt, DialoguePhase.FIELD_GOALS)
        assert res_goals.intent == DialogueIntent.REVISION_REQUEST, f"Expected REVISION_REQUEST for '{prompt}', got {res_goals.intent}"
        assert res_goals.has_confirmation is False


def test_screening_change_wishes_and_open_questions(router):
    """Prüft freie Änderungswünsche und offene Fragen im Screening."""
    for prompt in ("anders einteilen", "andere einteilung", "anders aufteilen", "einteilung anpassen"):
        res = router.classify(prompt, DialoguePhase.SCREENING, VALID_SIDS)
        assert res.intent == DialogueIntent.SCREENING_GUIDANCE, f"Failed for {prompt}"
        assert res.has_confirmation is False

    for question in ("Warum ist 1.1 aktiv?", "Wie viele Lebensfelder gibt es?", "Was bedeutet 2.1?"):
        res_q = router.classify(question, DialoguePhase.SCREENING, VALID_SIDS)
        assert res_q.intent == DialogueIntent.CLARIFICATION_QUESTION, f"Failed for {question}"
        assert res_q.has_confirmation is False


def test_router_with_llm_classifier_integration():
    """Prüft, dass ein übergebener LLM-Klassifizierer verwendet wird, aber Negations-Guards strikt greifen."""
    # 1. LLM classifies as revision_request
    llm_mock = lambda text, phase: DialogueIntent.REVISION_REQUEST
    router_llm = DialogueIntentRouter(llm_classifier=llm_mock)
    res = router_llm.classify("Könnten wir den Fokus eher auf soziale Kontakte legen?", DialoguePhase.FIELD_REFLECT)
    assert res.intent == DialogueIntent.REVISION_REQUEST

    # 2. Safety guard: Even if LLM erroneously returned CONFIRMATION on negated text, router overrides it
    faulty_llm = lambda text, phase: DialogueIntent.CONFIRMATION
    router_guarded = DialogueIntentRouter(llm_classifier=faulty_llm)
    res_guarded = router_guarded.classify("passt überhaupt nicht", DialoguePhase.SCREENING, VALID_SIDS)
    assert res_guarded.intent != DialogueIntent.CONFIRMATION
    assert res_guarded.has_confirmation is False


def test_screening_adjust_semantic_wohnen_deactivation(router):
    """Stellt sicher, dass Deaktivierung von Wohnen auch ohne explizite Ziffer '1.1' bzw. '4' als SCREENING_ADJUST erkannt wird."""
    # Hamburg (1.1)
    res_hh = router.classify(
        "wohnen ist erledigt der klient hat einen neue wohnung. kannst du deaktivieren",
        DialoguePhase.SCREENING,
        VALID_SIDS,
    )
    assert res_hh.intent == DialogueIntent.SCREENING_ADJUST
    assert "1.1" in res_hh.to_deactivate

    # Op de Wisch (4)
    op_de_wisch_sids = {"1", "2", "3", "4", "5", "6", "7", "8", "9"}
    res_odw = router.classify(
        "wohnen ist erledigt der klient hat einen neue wohnung. kannst du deaktivieren",
        DialoguePhase.SCREENING,
        op_de_wisch_sids,
    )
    assert res_odw.intent == DialogueIntent.SCREENING_ADJUST
    assert "4" in res_odw.to_deactivate


def test_draft_phase_intents_and_explicit_release(router):
    """Prüft Issue #72: Absichten in der DRAFT-Phase inklusive expliziter Freigabe und Living Draft Enrichment."""
    # 1. Explizite Freigabe-Signale
    for release_msg in ("abschnitt freigeben", "freigeben", "lebensfeld freigeben", "bitte freigeben", "jetzt freigeben"):
        res = router.classify(release_msg, DialoguePhase.FIELD_DRAFT)
        assert res.intent == DialogueIntent.CONFIRMATION, f"Failed for {release_msg}"
        assert res.has_confirmation is True
        assert res.is_explicit_release is True

    # 2. Allgemeine Bestätigungen (ohne expliziten Freigabe-Befehl)
    for confirm_msg in ("passt so", "passt", "sieht gut aus", "können wir so machen", "stimmig"):
        res = router.classify(confirm_msg, DialoguePhase.FIELD_DRAFT)
        assert res.intent == DialogueIntent.CONFIRMATION
        assert res.has_confirmation is True
        assert res.is_explicit_release is False

    # 3. Living Draft Ergänzungen / Fakten (ADDITIONAL_INFO)
    for info_msg in (
        "Er kocht sonntags gerne selbstständig mit Bildrezepten",
        "Assistenz begleitet 2x monatlich beim Einkauf",
        "Als Hilfsmittel nutzt er einen bebilderten Wochenplan",
    ):
        res = router.classify(info_msg, DialoguePhase.FIELD_DRAFT)
        assert res.intent == DialogueIntent.ADDITIONAL_INFO
        assert res.extracted_domain_fact == info_msg

    # 4. Kritik / Überarbeitungswünsche (REVISION_REQUEST)
    for rev_msg in ("1. ziel ist nicht smart", "bitte kürzen", "zu allgemein formuliert", "ziele anpassen"):
        res = router.classify(rev_msg, DialoguePhase.FIELD_DRAFT)
        assert res.intent == DialogueIntent.REVISION_REQUEST

    # 5. Fragen an den Assistenten (CLARIFICATION_QUESTION)
    for q_msg in ("Warum wird das erwähnt?", "Wie formuliert das der Kostenträger üblicherweise?"):
        res = router.classify(q_msg, DialoguePhase.FIELD_DRAFT)
        assert res.intent == DialogueIntent.CLARIFICATION_QUESTION



