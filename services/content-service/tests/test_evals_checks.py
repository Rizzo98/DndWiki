"""The check engine, exercised without any fixture and without a model.

These tests are the reason a fixture can be deleted safely: they build their own
inputs, so the harness keeps full coverage when evals/fixtures/ is empty.
"""

from __future__ import annotations

from typing import Any

from evals import checks


def spec(**overrides: Any) -> dict[str, Any]:
    base = {"id": "c1", "kind": "min_beats", "blocking": True, "applies_to": "run"}
    base.update(overrides)
    return base


async def grade(specs: list[dict[str, Any]], **kwargs: Any) -> list[checks.Outcome]:
    kwargs.setdefault("subject", "run")
    kwargs.setdefault("narrative", "")
    return await checks.evaluate(specs, **kwargs)


# --- min_beats --------------------------------------------------------------


async def test_min_beats_passes_at_the_floor_and_reports_the_count():
    (out,) = await grade([spec(value=3, note="current pipeline produces 2")], beats=["a", "b", "c"])
    assert out.status == checks.PASS
    assert "3 beats (want >= 3)" in out.detail
    assert "current pipeline produces 2" in out.detail


async def test_min_beats_fails_below_the_floor():
    (out,) = await grade([spec(value=25)], beats=["a", "b"])
    assert out.status == checks.FAIL
    assert out.blocks


# --- max_duplicate_similarity ------------------------------------------------


async def test_duplicate_beats_are_flagged():
    """The two beats the chunk seam actually produced, from a re-run of the pipeline.

    A PROXY, and it scores 0.45 where a true duplicate would be 1.0: the two calls
    that wrote these beats disagreed about the words ("ragazza" vs "nana"). That is
    exactly why this check is worth only a smoke alarm - the semantic version of it
    is the judge's job - but it is free, reproducible and it does fire here.
    """
    beats = [
        "Fuori dall'ospedale il gruppo incontra una ragazza che porta il pranzo a suo zio",
        (
            "Il gruppo, fuori dall'ospedale, incontra una nana che porta il pranzo a suo zio, "
            "il primario, e la lascia passare dopo un controllo"
        ),
    ]
    (out,) = await grade([spec(kind="max_duplicate_similarity", value=0.35)], beats=beats)
    assert out.status == checks.FAIL
    assert "worst pair" in out.detail


async def test_distinct_beats_pass_the_duplicate_check():
    beats = [
        "Il gruppo giunge all'ospedale con un uomo svenuto e le guardie lo affidano al medico",
        "Nel corridoio Rendar e Letho si affrontano e Letho punta un pugnale alla gola",
        "Un'esplosione arriva dal centro della citta mentre l'uomo guarda fuori dalla finestra",
    ]
    (out,) = await grade([spec(kind="max_duplicate_similarity", value=0.35)], beats=beats)
    assert out.status == checks.PASS


async def test_very_short_beats_are_not_compared():
    (out,) = await grade(
        [spec(kind="max_duplicate_similarity", value=0.1)], beats=["il gruppo entra", "il gruppo esce"]
    )
    assert out.status == checks.PASS
    assert "near-duplicates" in out.detail


# --- labels_not_english ------------------------------------------------------


async def test_a_label_written_in_english_fails():
    """The v15 defect: the engine's English scene reading leaked into the blocks."""
    blocks = [
        {"location": "the hospital in the city", "text": "..."},
        {"location": "the hospital, upstairs room where the unconscious man is treated", "text": "..."},
        {"location": "Ospedale di Fatumastra", "text": "..."},
    ]
    (out,) = await grade([spec(kind="labels_not_english", applies_to="text")], blocks=blocks)
    assert out.status == checks.FAIL
    assert "the hospital in the city" in out.detail


async def test_labels_in_the_table_language_pass():
    blocks = [
        {"location": "Ospedale di Fatumastra", "text": "..."},
        {"location": "", "text": "..."},  # "same place as before" is always allowed
        {"location": "Ospedale di Fatumastra, corridoio", "text": "..."},
    ]
    (out,) = await grade([spec(kind="labels_not_english", applies_to="text")], blocks=blocks)
    assert out.status == checks.PASS


# --- the judge ---------------------------------------------------------------


def fake_judge(verdict: dict[str, Any], seen: list[dict[str, Any]] | None = None):
    async def judge(criterion, narrative, evidence, if_absent):
        if seen is not None:
            seen.append(
                {"criterion": criterion, "narrative": narrative, "evidence": list(evidence), "if_absent": if_absent}
            )
        return verdict

    return judge


async def test_the_judge_receives_the_criterion_and_the_evidence():
    seen: list[dict[str, Any]] = []
    specs = [
        spec(
            kind="judge",
            applies_to="text",
            criterion="the girl is one person",
            evidence=["u_00434"],
            if_absent="pass",
        )
    ]
    (out,) = await grade(
        specs,
        narrative="Fuori dall'ospedale il gruppo incontra una ragazza.",
        evidence_for=lambda refs: [f"[{ref}] line" for ref in refs],
        judge=fake_judge({"verdict": "fail", "quote": "una ragazza", "reason": "invented"}, seen),
    )
    assert out.status == checks.FAIL
    assert out.detail == 'invented | "una ragazza"'
    assert seen[0]["criterion"] == "the girl is one person"
    assert seen[0]["evidence"] == ["[u_00434] line"]
    assert seen[0]["if_absent"] == "pass"


async def test_an_unknown_verdict_from_the_judge_is_unsure_not_a_pass():
    (out,) = await grade(
        [spec(kind="judge", applies_to="text", criterion="x")],
        judge=fake_judge({"verdict": "maybe"}),
    )
    assert out.status == checks.UNSURE
    assert out.blocks  # unsure is not a pass


async def test_a_judge_check_without_evidence_refs_available_is_skipped():
    (out,) = await grade(
        [spec(kind="judge", applies_to="text", criterion="x", evidence=["u_99999"])],
        evidence_for=lambda refs: [],
        judge=fake_judge({"verdict": "pass"}),
    )
    assert out.status == checks.SKIPPED
    assert not out.blocks


async def test_judge_checks_are_skipped_when_no_judge_is_available():
    (out,) = await grade([spec(kind="judge", applies_to="text", criterion="x")])
    assert out.status == checks.SKIPPED
    assert not out.blocks


# --- the safety net ----------------------------------------------------------


async def test_run_checks_are_skipped_when_grading_the_baseline():
    (out,) = await grade([spec(kind="min_beats", value=25)], subject="baseline", beats=["a"])
    assert out.status == checks.SKIPPED
    assert not out.blocks


async def test_a_broken_check_is_skipped_instead_of_taking_the_report_down():
    (out,) = await grade([spec(kind="min_beats", value="not-a-number")], beats=[])
    assert out.status == checks.SKIPPED
    assert "raised" in out.detail


async def test_an_unknown_kind_is_skipped():
    (out,) = await grade([spec(kind="no-such-kind")])
    assert out.status == checks.SKIPPED


async def test_advisory_checks_report_but_do_not_block():
    (out,) = await grade([spec(kind="min_beats", value=25, blocking=False)], beats=[])
    assert out.status == checks.FAIL
    assert not out.blocks


async def test_text_patterns_can_forbid_a_phrase():
    (out,) = await grade(
        [spec(kind="text_not_matches", applies_to="text", pattern=r"zio\s+primario")],
        narrative="una nana che porta il pranzo allo zio primario",
    )
    assert out.status == checks.FAIL


async def test_content_words_drop_stopwords():
    words = checks.content_words("Il gruppo entra nella stanza con il gruppo")
    assert words == {"entra", "stanza"}
