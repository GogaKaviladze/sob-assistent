"""Laufzeit-Defaults des LLMDraftGenerator (Bugfix UI-Lauf).

Der UI-Lauf scheiterte, weil der Generator ohne explizite Parameter auf die
llama.cpp-Defaults (n_ctx=512, CPU-only) zurückfiel — der erste Abschnitts-Prompt
sprengte das Kontextfenster. Diese Tests sichern, dass die Produktionsdefaults
greifen und explizite Werte (CLI) weiterhin gewinnen. Laufen ohne GGUF-Modell:
die Kwargs werden gesetzt, das Modell aber erst lazy in `.llm` geladen.
"""
import logging
from unittest.mock import MagicMock

import pytest

from sob_generator.llm.generator import (
    CHAT_FORMAT_CHATML,
    CHAT_FORMAT_NONE,
    DEFAULT_N_CTX,
    DEFAULT_N_GPU_LAYERS,
    DEFAULT_N_THREADS,
    DEFAULT_SEED,
    DEVELOPMENTS_MAX_TOKENS,
    GENERATION_TEMPERATURE,
    SECTION_MAX_TOKENS,
    LLMDraftGenerator,
    resolve_chat_format,
)
from sob_generator.llm.schemas import SectionLLMOutput


def _fake_llm(n_prompt_tokens: int = 3, n_ctx: int = DEFAULT_N_CTX) -> MagicMock:
    """Fake-Llama für _complete: liefert leeres JSON, mockt tokenize/n_ctx.

    Der n_ctx-Guard (#50/K5) tokenisiert den Prompt real — ein nackter
    MagicMock würde an len(tokenize(...)) scheitern.
    """
    fake = MagicMock(return_value={"choices": [{"text": "{}"}]})
    fake.tokenize.return_value = list(range(n_prompt_tokens))
    fake.n_ctx.return_value = n_ctx
    return fake


def test_defaults_werden_ohne_explizite_kwargs_gesetzt():
    gen = LLMDraftGenerator(model_path="egal.gguf")
    assert gen._llama_kwargs["n_ctx"] == DEFAULT_N_CTX
    assert gen._llama_kwargs["n_threads"] == DEFAULT_N_THREADS
    assert gen._llama_kwargs["n_gpu_layers"] == DEFAULT_N_GPU_LAYERS


def test_n_ctx_default_ist_gross_genug_fuer_abschnitts_prompts():
    # Regression: der UI-Lauf lief mit dem llama.cpp-Default 512 → Abbruch.
    assert LLMDraftGenerator(model_path="egal.gguf")._llama_kwargs["n_ctx"] >= 8192


def test_section_token_budget_reicht_fuer_alle_vier_felder():
    # Regression #47: mit 1024 brach der 14B-Lauf das Abschnitts-JSON vor
    # vorschlag_ziele/-massnahmen ab — beide blieben in allen 9 Abschnitten
    # leer. Das Budget muss ~384 Tokens je Feld (4 Felder) hergeben.
    assert SECTION_MAX_TOKENS >= 1536


def test_explizite_kwargs_ueberschreiben_defaults():
    gen = LLMDraftGenerator(model_path="egal.gguf", n_ctx=4096, n_gpu_layers=0)
    assert gen._llama_kwargs["n_ctx"] == 4096
    assert gen._llama_kwargs["n_gpu_layers"] == 0
    # Nicht überschriebene Defaults bleiben erhalten.
    assert gen._llama_kwargs["n_threads"] == DEFAULT_N_THREADS


def test_seed_default_wird_an_llama_kwargs_durchgereicht():
    # #35/#38: fester Seed für reproduzierbare A/B-Läufe. Seed muss im
    # llama_kwargs-Dict landen, das an den Llama(...)-Konstruktor übergeben wird.
    gen = LLMDraftGenerator(model_path="egal.gguf")
    assert gen._llama_kwargs["seed"] == DEFAULT_SEED


def test_seed_ist_explizit_konfigurierbar():
    gen = LLMDraftGenerator(model_path="egal.gguf", seed=99)
    assert gen._llama_kwargs["seed"] == 99


# --- ChatML-Wrapping für Qwen (#49) ---------------------------------------


def test_auto_erkennt_qwen_am_dateinamen():
    assert resolve_chat_format("auto", "models/Qwen2.5-7B-Instruct-Q4_K_M.gguf") == CHAT_FORMAT_CHATML
    assert resolve_chat_format("auto", "models/Meta-Llama-3.1-8B.gguf") == CHAT_FORMAT_NONE
    assert resolve_chat_format("auto", "models/phi-4.gguf") == CHAT_FORMAT_NONE


def test_explizites_chat_format_gewinnt_gegen_auto_erkennung():
    # Opt-out: none erzwingt raw completion auch bei Qwen (A/B-Läufe).
    assert resolve_chat_format("none", "models/Qwen2.5-7B.gguf") == CHAT_FORMAT_NONE
    assert resolve_chat_format("chatml", "models/phi-4.gguf") == CHAT_FORMAT_CHATML


def test_unbekanntes_chat_format_wird_abgelehnt():
    with pytest.raises(ValueError):
        resolve_chat_format("mistral", "models/egal.gguf")


def test_complete_wrappt_qwen_prompt_in_chatml_marker():
    # #49: chat_format im Llama(...)-Konstruktor wirkt nur auf
    # create_chat_completion — der Generator nutzt raw completion, daher muss
    # _complete die Marker selbst um den Prompt legen.
    gen = LLMDraftGenerator(model_path="Qwen2.5-7B-Instruct-Q4_K_M.gguf")
    fake_llm = _fake_llm()
    gen._llm = fake_llm

    gen._complete("Schreibe den Abschnitt.", SectionLLMOutput, max_tokens=16)

    prompt = fake_llm.call_args.args[0]
    assert prompt.startswith("<|im_start|>user\n")
    assert "Schreibe den Abschnitt." in prompt
    assert prompt.endswith("<|im_end|>\n<|im_start|>assistant\n")


def test_complete_laesst_nicht_qwen_prompt_unveraendert():
    gen = LLMDraftGenerator(model_path="Meta-Llama-3.1-8B.gguf")
    fake_llm = _fake_llm()
    gen._llm = fake_llm

    gen._complete("Schreibe den Abschnitt.", SectionLLMOutput, max_tokens=16)

    assert fake_llm.call_args.args[0] == "Schreibe den Abschnitt."


def test_complete_nutzt_erhoehte_temperatur_gegen_kollaps():
    # #30: nahezu greedy-Decoding (0.2) ließ alle 9 Abschnitte auf dieselbe
    # Eröffnungsfloskel kollabieren. _complete muss die erhöhte Temperatur an
    # llama.cpp durchreichen; die GBNF-Grammar sichert weiterhin die Struktur.
    gen = LLMDraftGenerator(model_path="egal.gguf")
    fake_llm = _fake_llm()
    gen._llm = fake_llm  # Lazy-Load des echten Modells umgehen.

    gen._complete("irgendein prompt", SectionLLMOutput, max_tokens=16)

    assert fake_llm.call_args.kwargs["temperature"] == GENERATION_TEMPERATURE
    # Klar über dem Greedy-/Kollaps-Regime.
    assert GENERATION_TEMPERATURE >= 0.6


# --- #50/K5: developments-Budget + n_ctx-Guard ----------------------------


def test_developments_token_budget_deckt_lange_zeitraeume():
    # developments fasst ALLE Einträge zusammen — 512 schnitt das JSON bei
    # langen Berichtszeiträumen vor dem schließenden Objekt ab.
    assert DEVELOPMENTS_MAX_TOKENS >= 768


def test_ctx_guard_warnt_wenn_prompt_plus_budget_n_ctx_sprengt(caplog):
    gen = LLMDraftGenerator(model_path="egal.gguf")
    gen._llm = _fake_llm(n_prompt_tokens=8000, n_ctx=8192)

    with caplog.at_level(logging.WARNING, logger="llm.generator"):
        gen._complete("langer prompt", SectionLLMOutput, max_tokens=1024)

    assert any("n_ctx" in rec.getMessage() for rec in caplog.records)


def test_ctx_guard_schweigt_wenn_das_budget_reicht(caplog):
    gen = LLMDraftGenerator(model_path="egal.gguf")
    gen._llm = _fake_llm(n_prompt_tokens=100, n_ctx=8192)

    with caplog.at_level(logging.WARNING, logger="llm.generator"):
        gen._complete("kurzer prompt", SectionLLMOutput, max_tokens=1024)

    assert not caplog.records


def test_classify_entries_batch_routes_correctly():
    from unittest.mock import patch
    from sob_generator.llm.generator import LLMDraftGenerator
    from sob_generator.llm.schemas import DocumentationEntry
    from sob_generator.llm.sections import SECTION_DEFINITIONS

    gen = LLMDraftGenerator(model_path="egal.gguf")
    entries = [
        DocumentationEntry(datum="01.01", kategorie="w", text="Miete und Wohnung"),
        DocumentationEntry(datum="02.01", kategorie="w", text="Wäsche gewaschen"),
    ]
    batch_json = '{"classifications": [{"id": 0, "section_id": "1.1"}, {"id": 1, "section_id": "1.2"}]}'

    with patch.object(gen, "_complete", return_value=batch_json):
        result = gen.classify_entries_batch(entries, SECTION_DEFINITIONS)

    assert len(result["1.1"]) == 1
    assert result["1.1"][0].text == "Miete und Wohnung"
    assert len(result["1.2"]) == 1
    assert result["1.2"][0].text == "Wäsche gewaschen"
    assert result["unklar"] == []


def test_classify_entries_batch_handles_empty_entries():
    from sob_generator.llm.generator import LLMDraftGenerator
    from sob_generator.llm.sections import SECTION_DEFINITIONS

    gen = LLMDraftGenerator(model_path="egal.gguf")
    result = gen.classify_entries_batch([], SECTION_DEFINITIONS)
    assert isinstance(result, dict)
    assert result["unklar"] == []
    assert all(len(entries) == 0 for entries in result.values())

