"""Behavioral acceptance for #76; exclusively synthetic, section-scoped data."""
from copy import deepcopy
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from sob_generator.chat_engine import ChatDialogueEngine, format_goal_proposal, professional_goal_reply
from sob_generator.chat_session import ChatSession, SectionChatState, DialoguePhase, SectionStatus
from sob_generator.chat_store import ChatSessionStore
from sob_generator.llm.generator import GenerationResult, LLMDraftGenerator
from sob_generator.llm.grammar import build_grammar
from sob_generator.llm.schemas import GoalProposalOutput, DialogueTurnOutput, SectionLLMOutput
from sob_generator.stub_generator import StubDraftGenerator


class Recorder(StubDraftGenerator):
    def __init__(self):
        self.contexts = []
        self.drafts = []
        self.fail = False

    def converse(self, context):
        self.contexts.append(deepcopy(context))
        return super().converse(context)

    def revise_field_goals(self, **kwargs):
        if self.fail:
            return GenerationResult(GoalProposalOutput(goals='', reply='Fehler'), True, '', 'failure')
        return GenerationResult(GoalProposalOutput(goals='Ein selbst gewählter Brief wird bis zum vereinbarten Termin geöffnet (Termin noch offen).', reply='Bitte prüfe die neue Formulierung.'), False, '')

    def refine_section(self, **kwargs):
        self.drafts.append(deepcopy(kwargs))
        if self.fail:
            raise RuntimeError('synthetic unavailable')
        return super().refine_section(**kwargs)


def setup(phase=DialoguePhase.FIELD_REFLECT):
    generator=Recorder()
    session=ChatSession(phase=phase, active_section_id='1.1', sections={
        '1.1': SectionChatState(section_id='1.1', title='Wohnsituation', smarte_ziele='Historisches Ziel'),
        '2.3': SectionChatState(section_id='2.3', title='Gesundheit')})
    store=ChatSessionStore();store.store(session)
    return ChatDialogueEngine(generator, session_store=store), session, generator


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
@pytest.mark.parametrize('phase', [DialoguePhase.FIELD_REFLECT, DialoguePhase.FIELD_QUESTIONS, DialoguePhase.FIELD_DRAFT])
async def test_goal_revision_survives_confirmation_draft_export_and_failure(phase):
    engine, session, generator=setup(phase)
    state=session.sections['1.1']
    state.confirmed_goals='Bisher bestätigter Vorschlag'
    await engine.process_message(session.session_id,'Bitte die Ziele konkreter formulieren.')
    revised=state.proposed_goals
    assert revised.startswith('Ein selbst gewählter Brief')
    assert state.confirmed_goals == 'Bisher bestätigter Vorschlag'
    assert state.smarte_ziele == 'Historisches Ziel'
    assert state.user_notes == []
    await engine.process_message(session.session_id,'Ziele bestätigen')
    assert state.confirmed_goals == revised
    await engine.process_message(session.session_id,'Entwurf erstellen')
    assert state.vorschlag_ziele == revised
    assert revised in generator.drafts[-1]['freitext']
    assert session.to_report_draft().sections[0].vorschlag_ziele == revised
    generator.fail=True
    await engine.process_message(session.session_id,'Bitte die Ziele überarbeiten.')
    assert state.proposed_goals == revised
    assert 'bleibt erhalten' in session.messages[-1].content


@pytest.mark.anyio
async def test_questions_actions_uncertainty_and_pending_question():
    engine,session,generator=setup()
    state=session.sections['1.1']
    state.pending_question='Welche Unterstützung benötigt die Person beim Öffnen der Post?'
    replies=[]
    for suggestion in session.get_reflection_suggestions():
        await engine.process_message(session.session_id,suggestion.text,suggestion.action)
        replies.append(session.messages[-1].content)
    assert replies[0] != replies[1]
    assert 'Grundlage' in replies[0]
    assert 'Hypothese' in replies[1]
    assert not state.user_notes and not generator.drafts
    pending=state.pending_question
    await engine.process_message(session.session_id,'Das kann ich noch nicht beurteilen.')
    assert state.pending_question == pending
    assert state.uncertainties == ['Das kann ich noch nicht beurteilen.']
    assert session.phase == DialoguePhase.FIELD_QUESTIONS
    assert not generator.drafts
    await engine.process_message(session.session_id,'Entwurf erstellen')
    assert 'Offene Angaben' in generator.drafts[-1]['freitext']


@pytest.mark.anyio
async def test_start_explains_workflow_without_inferring_stability_from_missing_entries(tmp_path):
    from dataclasses import replace
    from sob_generator.importer import VivendiImport
    from sob_generator.models import ClientData, ReportMeta
    from sob_generator.report_template import TEMPLATE_REGISTRY

    template = TEMPLATE_REGISTRY['hamburg_soziale_dienste']
    imported = VivendiImport(client=ClientData(name='Mustermann', vorname='Max'), meta=ReportMeta(), entries=[])
    class EmptyImporter:
        def load(self):
            return imported
    template = replace(template, importer_factory=lambda _: EmptyImporter())
    engine = ChatDialogueEngine(Recorder(), template=template)
    session = await engine.start_session(tmp_path / 'synthetic.csv')
    opening = '\n'.join(message.content for message in session.messages)
    assert 'dich als Fachkraft' in opening
    assert 'Die Bestätigung betrifft nur die Auswahl' in opening
    assert 'keine Angaben vorhanden – bitte prüfen' in opening
    assert 'stabil / entfällt' not in opening
    assert all(section.status is SectionStatus.EMPTY for section in session.sections.values())


def test_json_goal_proposal_is_presented_as_readable_professional_text():
    raw = ('[{"zielinhalt":"Herr Mustermann öffnet einen Brief.",'
           '"messbar":"Ein Brief", "zeitlich_begrenzt":"Termin offen",'
           '"tatsächlichkeitsgeprüft":""}]')
    formatted = format_goal_proposal(raw)
    assert raw not in formatted
    assert '**Zielvorschlag**' in formatted
    assert '- Ziel: Herr Mustermann öffnet einen Brief.' in formatted
    assert '- Prüfkriterium: Ein Brief' in formatted
    assert '- Zeitrahmen: Termin offen' in formatted


def test_goal_prompt_addresses_professional_instead_of_client():
    from sob_generator.llm.prompts import build_goal_proposal_prompt
    prompt = build_goal_proposal_prompt({'message': 'Synthetische Beobachtung'})
    assert 'ausschließlich die Fachkraft' in prompt
    assert 'keine direkte Frage' in prompt


def test_direct_client_address_in_goal_reply_is_replaced_deterministically():
    reply = 'Hans, wie war es, die Situation selbst zu meistern?'
    safe = professional_goal_reply(reply, 'Hans', has_goals=True)
    assert 'Hans' not in safe
    assert safe == 'Bitte prüfe den folgenden Zielvorschlag fachlich und ergänze fehlende Angaben.'


def test_instructional_model_reply_is_neutralized_when_goal_exists():
    safe = professional_goal_reply(
        'Du solltest das Selbstvertrauen stärken.',
        'Hans',
        has_goals=True,
        context='Lebensfeld 1 (Psychische Verfassung)',
    )
    assert safe == ('Bitte prüfe den folgenden Zielvorschlag für **Lebensfeld 1 '
                    '(Psychische Verfassung)** fachlich und ergänze fehlende Angaben.')


@pytest.mark.anyio
async def test_correction_replaces_note_and_duplicate_does_not_advance():
    engine,session,generator=setup()
    await engine.process_message(session.session_id,'Beim Besuch öffnet er den Brief selbst.')
    await engine.process_message(session.session_id,'Korrektur: Beim Besuch öffnet die Assistenz den Brief.')
    state=session.sections['1.1']
    assert state.user_notes == ['Korrektur: Beim Besuch öffnet die Assistenz den Brief.']
    assert 'Beim Besuch öffnet er den Brief selbst.' not in session.memory.learned_facts
    await engine.process_message(session.session_id,'Korrektur: Beim Besuch öffnet die Assistenz den Brief.')
    assert len(state.user_notes) == 1
    assert not generator.drafts


@pytest.mark.anyio
async def test_explicit_correction_does_not_depend_on_model_note_extraction():
    engine, session, generator = setup()
    state = session.sections['1.1']
    old_note = 'Beim letzten Besuch öffnete Herr Mustermann den Brief selbst.'
    state.user_notes = [old_note]
    session.memory.learned_facts = [old_note]
    generator.converse = lambda context: GenerationResult(DialogueTurnOutput(
        public_note='', supersedes=[], pending_question='', sufficient=False,
        reply='Die Assistenz hat den Brief geöffnet.'), False, '')

    await engine.process_message(
        session.session_id,
        'Korrektur: Beim letzten Besuch öffnete die Assistenz den Brief.',
    )

    assert state.user_notes == [
        'Korrektur: Beim letzten Besuch öffnete die Assistenz den Brief.'
    ]
    assert old_note not in session.memory.learned_facts


@pytest.mark.anyio
async def test_field_switch_keeps_context_isolated():
    engine,session,generator=setup()
    await engine.process_message(session.session_id,'Post wird gemeinsam geöffnet.')
    await engine.process_message(session.session_id,'Wechsel zu Feld 2.3')
    await engine.process_message(session.session_id,'Termine werden telefonisch vereinbart.')
    context=generator.contexts[-1]
    assert context['field'] == engine.get_section_def('2.3').title
    assert 'Post' not in repr(context)
    assert 'Wechsel' not in repr(context['notes'])


@pytest.mark.anyio
async def test_untrusted_extraction_and_failed_draft_are_non_destructive():
    engine,session,generator=setup()
    state=session.sections['1.1'];state.bericht_zielerreichung='Bestehender Bericht.'
    generator.converse=lambda context: GenerationResult(DialogueTurnOutput(reply='Welche Angabe meinst du genau?', public_note='Erfundene Diagnose', supersedes=[], pending_question='Rückfrage?', sufficient=False),False,'')
    await engine.process_message(session.session_id,'Eine konkrete öffentliche Beobachtung.')
    assert state.user_notes == []
    generator.fail=True
    await engine.process_message(session.session_id,'Entwurf erstellen')
    assert state.bericht_zielerreichung == 'Bestehender Bericht.'
    assert 'bleibt erhalten' in session.messages[-1].content


@pytest.mark.parametrize('schema', [GoalProposalOutput, DialogueTurnOutput])
def test_structured_contract_uses_generated_grammar(schema):
    assert build_grammar(schema)._grammar


def test_real_generator_contract_and_prompt_separate_instructions():
    generator=LLMDraftGenerator('unused.gguf')
    result=GoalProposalOutput(goals='Prüfbarer Zielinhalt.',reply='Bitte prüfen.')
    with patch.object(generator,'_complete',return_value=result.model_dump_json()) as complete:
        from sob_generator.llm.sections import SECTION_DEFINITIONS
        revised=generator.revise_field_goals(SECTION_DEFINITIONS[0],'Vorheriges Ziel','Kürzer formulieren',[])
    assert revised.value == result
    assert complete.call_args.args[1] == GoalProposalOutput
    assert 'Kürzer formulieren' in complete.call_args.args[0]
    assert 'Bearbeitungsauftrag selbst' in complete.call_args.args[0]


def test_dynamic_extraction_contract_rejects_paraphrased_facts():
    from pydantic import ValidationError
    from sob_generator.llm.schemas import dialogue_output_contract
    schema=dialogue_output_contract('Er öffnet einen Brief. Häufigkeit unbekannt.')
    assert build_grammar(schema)._grammar
    with pytest.raises(ValidationError):
        schema(public_note='Er öffnet täglich Briefe.', supersedes=[], pending_question='', sufficient=False,reply='Welche Unterstützung benötigt er?')


@pytest.mark.anyio
async def test_typed_actions_rejected_when_stale_or_tampered():
    engine,session,generator=setup()
    with pytest.raises(ValueError):
        await engine.process_message(session.session_id,'Erfundener Fakt','explain_goal')
    assert session.messages == [] and not session.memory.learned_facts


def test_correction_target_is_unique_and_does_not_remove_unrelated_note():
    from sob_generator.conversation import correction_target
    notes=['Beim letzten Besuch hat er den Brief selbst geöffnet.', 'Das kann ich noch nicht beurteilen.']
    assert correction_target('Korrektur: Beim letzten Besuch hat die Assistenz den Brief geöffnet.',notes) == 0
    assert correction_target('Korrektur: Der Termin war telefonisch.',notes) is None
    assert correction_target('Korrektur: Beim letzten Besuch hat die Assistenz den Brief geöffnet.',[notes[0],notes[0]]) is None


@pytest.mark.anyio
async def test_echoed_professional_question_is_not_asked_back():
    engine, session, generator = setup()
    question = 'Wie unterscheide ich eine Beobachtung von einer Interpretation?'
    answer = 'Beobachtungen beschreiben Verhalten. Deutungen sind Hypothesen.'
    generator.converse = lambda context: GenerationResult(DialogueTurnOutput(
        public_note='', supersedes=[], pending_question=question, sufficient=False,
        reply=answer + ' ' + question), False, '')
    await engine.process_message(session.session_id, question)
    assert session.messages[-1].content == answer
    assert not session.sections['1.1'].pending_question
    assert not generator.drafts


@pytest.mark.anyio
async def test_correction_cannot_silently_resolve_pending_support_question():
    engine, session, generator = setup()
    state = session.sections['1.1']
    state.user_notes = ['Beim letzten Besuch hat er den Brief selbst geöffnet.']
    state.pending_question = 'Welche Unterstützung wurde beim Lesen benötigt?'
    generator.converse = lambda context: GenerationResult(DialogueTurnOutput(
        public_note=context['message'], supersedes=[0], pending_question='',
        sufficient=True, reply='Die korrigierte Beobachtung ist aufgenommen.'), False, '')
    await engine.process_message(session.session_id,
        'Korrektur: Beim letzten Besuch hat die Assistenz den Brief geöffnet.')
    assert state.pending_question == 'Welche Unterstützung wurde beim Lesen benötigt?'
    assert len(state.user_notes) == 1 and 'Assistenz' in state.user_notes[0]
    assert not generator.drafts


def test_clinical_prompt_does_not_prescribe_crisis_intervention_without_evidence():
    from sob_generator.llm.prompts import _clinical_guardrails
    from sob_generator.llm.sections import SectionDefinition
    section = SectionDefinition(index=0, id='1.4', title='Emotionale Entwicklung', description='')
    text = _clinical_guardrails(section, 'Arzttermin selbst telefonisch vereinbart.')
    assert 'Krisenmanagement & Notfallplanung' not in text
    assert 'Verwende zwingend' not in text
    historical = _clinical_guardrails(section, 'Letztes Jahr stationär in der Psychiatrie.')
    assert 'Historische Krisen begründen keine aktuelle Diagnose' in historical


def test_dialogue_sampling_preserves_verbatim_evidence_without_changing_report_sampling():
    from unittest.mock import MagicMock
    from sob_generator.llm.generator import GENERATION_TEMPERATURE, DEFAULT_REPEAT_PENALTY
    from sob_generator.llm.schemas import dialogue_output_contract
    generator = LLMDraftGenerator('synthetic-qwen.gguf')
    generator._llm = MagicMock(return_value={'choices': [{'text': '{}'}]})
    generator._llm.tokenize.return_value = [1]
    generator._llm.n_ctx.return_value = 8192
    for schema in (dialogue_output_contract('Eine Beobachtung.'), GoalProposalOutput):
        generator._complete('Synthetischer Test', schema, 30)
        assert generator._llm.call_args.kwargs['temperature'] == 0.2
        assert generator._llm.call_args.kwargs['repeat_penalty'] == 1.0
    generator._complete('Synthetischer Test', SectionLLMOutput, 30)
    assert generator._llm.call_args.kwargs['temperature'] == GENERATION_TEMPERATURE
    assert generator._llm.call_args.kwargs['repeat_penalty'] == DEFAULT_REPEAT_PENALTY


@pytest.mark.parametrize('audit', [
    '{"unsupported_claims": ["Erfundener Termin"], "supported": false}',
    '{"unsupported_claims": ["Widerspruch"], "supported": true}',
    'invalid',
])
def test_unverified_draft_is_not_accepted(audit):
    from sob_generator.llm.sections import SECTION_DEFINITIONS
    draft = SectionLLMOutput(bericht_zielerreichung='Er öffnete den Brief selbst.',
        vorschlag_ziele='Zwei Briefe nächste Woche.', vorschlag_massnahmen='Assistenz.', realisierung_andere='')
    generator = LLMDraftGenerator('unused.gguf')
    with patch.object(generator, '_complete', side_effect=[draft.model_dump_json(), audit, audit]):
        result = generator.refine_section(SECTION_DEFINITIONS[0], [],
            user_notes=['Korrektur: Die Assistenz öffnete den Brief.'])
    assert result.needs_review and result.error


def test_grounded_single_sentence_draft_does_not_require_artificial_expansion():
    from sob_generator.llm.sections import SECTION_DEFINITIONS
    from sob_generator.llm.schemas import DocumentationEntry, GroundingCheckOutput
    draft = SectionLLMOutput(bericht_zielerreichung='Er vereinbart Arzttermine selbst telefonisch.',
        vorschlag_ziele='Weitere Wünsche sind offen.', vorschlag_massnahmen='Bedarf offen.', realisierung_andere='')
    generator = LLMDraftGenerator('unused.gguf')
    with patch.object(generator, '_complete', side_effect=[draft.model_dump_json(),
            '{"unsupported_claims": [], "supported": true}']) as completion:
        result = generator.refine_section(SECTION_DEFINITIONS[4],
            [DocumentationEntry(datum='', kategorie='', text=draft.bericht_zielerreichung)])
    assert not result.needs_review and result.value == draft
    assert completion.call_count == 2
    assert completion.call_args.args[1] is GroundingCheckOutput
    assert build_grammar(GroundingCheckOutput)._grammar
