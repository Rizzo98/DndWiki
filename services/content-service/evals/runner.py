"""Run the real content pipeline over a fixture, grade it, and keep the evidence.

WHAT THIS DOES NOT DO: it does not re-implement phase 1. It calls the SAME
functions `app.workers.generate.process_job` calls, in the same order, and simply
stops before the parts that need a database, a message broker and a wiki. An eval
that reimplements the pipeline measures the reimplementation, and the whole reason
this exists is that we could not tell whether a change made summaries better.

    _artifact_views -> extract_many -> merge_extractions -> apply_gate
                    -> _compose_summary

Three things are graded, in this order, cheapest first:

1. the STORED baseline (baseline.json), offline, no model: the deterministic
   checks must fire on the draft that shipped, or the eval measures nothing;
2. a FRESH run of the pipeline over the fixture's transcript;
3. the judge checks, which are the only ones that can see that a sentence states a
   real fact about the wrong person.

Every intermediate artefact is written under the output directory, so two runs can
be DIFFED instead of argued about - which is the point. A prompt change is a
hypothesis; this is the experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.attribution import apply_gate
from app.chunking import build_view_lines, chunk_ranges, owned_parts
from app.conflicts import conflicts_payload, find_conflicts
from app.core.config import ServiceSettings, get_settings
from app.extraction import LLMClient
from app.merger import (
    exclude_character_names,
    is_narrator_name,
    merge_extractions,
    rename_characters,
)
from app.roster import CampaignRoster, roster_from_members
from app.services.summaries import summary_lines
from app.summary import blocks_to_text
from app.workers.generate import (
    _artifact_player_names,
    _artifact_views,
    _compose_summary,
    _scene_reading,
    party_character_names,
    resolve_speaker_names,
)
from evals import checks, history
from evals import compare as benchmark
from evals import fixtures as store


class _NoStorage:
    """_artifact_views takes a storage handle that this path must never touch.

    The fixture IS the transcript, so a run that suddenly needed MinIO would mean
    the eval had silently stopped measuring the fixture. Failing loudly here is
    better than downloading a different session's transcript and grading that.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"_artifact_views reached for storage.{name}: the eval must run from "
            "the fixture alone"
        )


class _NoUsers:
    """resolve_speaker_names takes a user-service client; offline there is none.

    The worker resolves a label through user-service only when the campaign
    member carries no CHARACTER name; a party member is named by their character,
    which the fixture's speaker map already holds. So an empty resolver loses
    nothing the fixture actually declared, and a label the map does not cover
    keeps its raw diarization name - exactly as it does in production when nobody
    has identified that speaker yet.
    """

    async def display_names(self, user_ids: list[Any]) -> dict[str, str]:
        return {}


async def _transcript_views(
    fixture: store.Fixture, settings: ServiceSettings, llm: Any | None = None
) -> tuple[list[str], list[str], list[str], list[Any]]:
    """The DIARIZED input -> views, the way generate._build_chunk_views does it.

    This is the worker's OTHER branch: no attributed artifact, so the view comes
    from transcript.json plus a speaker map (build_speaker_map's role, played
    here by the fixture's own 'speakers'/'dm_speakers' metadata). It calls the
    worker's real functions - build_view_lines, chunk_ranges, owned_parts - so a
    change to the chunker reaches the eval the moment it reaches the worker.

    What it does NOT have offline: the campaign-service lookup that names the
    DM's speaker. A fixture declares it instead (dm_speakers), and a fixture that
    declares nothing runs the way a session runs before anyone is identified.
    """
    speaker_map = {
        label: {"user_id": None, "display_name": None, "character_name": name}
        for label, name in fixture.speakers.items()
    }
    speaker_names = await resolve_speaker_names(speaker_map, _NoUsers())  # type: ignore[arg-type]
    dm_names = [str(name) for name in (fixture.meta.get("dm_speakers") or []) if name]
    party = [
        name
        for name in party_character_names(speaker_map)
        if name not in dm_names and not is_narrator_name(name)
    ]
    lines = build_view_lines(
        list(fixture.transcript.get("segments") or []), speaker_names
    )
    ranges = chunk_ranges(
        lines, max_tokens=settings.chunk_tokens, overlap=settings.chunk_overlap
    )
    if not ranges:
        raise ValueError(
            f"fixture {fixture.name}: the transcript has no segments to generate from"
        )
    if len(ranges) > settings.max_chunks_per_session:
        raise ValueError(
            f"fixture {fixture.name} yields {len(ranges)} chunks "
            f"(cap {settings.max_chunks_per_session}); the worker would refuse too"
        )
    # The campaign's roster, exactly as the worker gets it (app/roster.py): through
    # roster_from_members() over campaign-service's member rows, which the fixture
    # declares. Best-effort in production; absent here means a session summarised
    # without it.
    roster = roster_from_members(fixture.roster_members)
    if not party and roster.players:
        party = roster.character_names
    if roster.dm_names:
        dm_names = sorted({*dm_names, *roster.dm_names})
    party_note = (
        "Party (player characters): " + ", ".join(sorted(set(party))) + "\n\n"
        if party
        else ""
    )
    # The same reading the worker takes: a fixture with no speaker map runs the way
    # a session runs before anyone is identified, and the "[Cast]" note is what the
    # session's own words establish about its voices (app/speakers.py).
    cast = ""
    if llm is not None and settings.cast_reading_enabled and not fixture.speakers:
        try:
            reading = await llm.read_speakers(lines)
        except Exception as exc:  # noqa: BLE001 - a reading is never required
            print(f"{fixture.name}: could not read the cast ({exc}); continuing without it")
            reading = None
        if reading is not None and not reading.empty:
            cast = reading.note()
            # Narrators only: see SpeakerReading.note for the measurement.
            dm_names = sorted({*dm_names, *reading.narrators})
    table = roster.note() if settings.roster_note_enabled else ""
    views = [
        party_note + table + cast + "\n".join(lines[start:end]) for start, end in ranges
    ]
    return views, party, dm_names, owned_parts(lines, ranges), roster


JUDGE_SYSTEM_PROMPT = """You are auditing one paragraph of a tabletop RPG session record.

You receive a CRITERION, the NARRATIVE a summariser wrote, and TRANSCRIPT EVIDENCE:
lines taken from the recording itself.

Decide whether the NARRATIVE satisfies the CRITERION. Judge it only against the
evidence you are given, and judge what the text SAYS, not what is plausible: a
sentence can be fluent, sensible and still state a real fact about the wrong
person, which is the failure this audit exists to find.

Respond with a single JSON object:

{"addressed": true, "verdict": "pass", "quote": "", "reason": ""}

* "addressed": false when the narrative never touches what the criterion is about.
* "verdict": "pass" when the narrative satisfies the criterion, "fail" when it
  contradicts it, "unsure" only when the narrative is genuinely ambiguous.
* "quote": the narrative's own words that decided it, or "" when none did.
* "reason": one sentence, in English, saying why.
"""


@dataclass
class RunResult:
    fixture: str
    subject: str
    narrative: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    beats: list[str] = field(default_factory=list)
    outcomes: list[checks.Outcome] = field(default_factory=list)
    artefacts: dict[str, Any] = field(default_factory=dict)
    out_dir: Path | None = None
    #: The draft measured against the fixture's benchmark.txt, when it has one.
    comparison: benchmark.Comparison | None = None

    @property
    def blocking_failures(self) -> list[checks.Outcome]:
        return [outcome for outcome in self.outcomes if outcome.blocks]


# --------------------------------------------------------------------------
# the judge
# --------------------------------------------------------------------------


def make_judge(llm: LLMClient):
    """Wrap the service's own JSON completion as an adjudicator.

    It reuses `LLMClient._complete_json` rather than calling litellm here so the
    judge inherits the same provider wiring, local JSON repair, truncation guard
    and corrective retry the pipeline relies on. Model behaviour differences
    between the judge and the thing being judged would be one more variable in an
    experiment that already has too many.
    """

    async def judge(
        criterion: str, narrative: str, evidence: Sequence[str], if_absent: str
    ) -> dict[str, Any]:
        message = (
            f"CRITERION:\n{criterion.strip()}\n\n"
            "TRANSCRIPT EVIDENCE (the recording this narrative summarises):\n"
            + ("\n".join(evidence) if evidence else "(none supplied)")
            + f"\n\nNARRATIVE:\n{narrative.strip()}\n\n"
            "Does the narrative satisfy the criterion?"
        )
        try:
            payload = await llm._complete_json(
                JUDGE_SYSTEM_PROMPT, message, "the fixture judge", extraction=False
            )
        except Exception as exc:  # noqa: BLE001 — a failed judge call is "unsure", not a crash
            return {"verdict": checks.UNSURE, "reason": f"judge call failed: {exc}"}
        if not payload.get("addressed", True):
            return {
                "verdict": if_absent,
                "reason": "the narrative does not address the criterion",
            }
        return payload

    return judge


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------


async def run_pipeline(
    fixture: store.Fixture, settings: ServiceSettings
) -> dict[str, Any]:
    """Phase 1, exactly as the worker runs it, minus the side effects."""
    llm = LLMClient(settings)
    started = time.monotonic()
    timings: dict[str, float] = {}

    mark = time.monotonic()
    if fixture.input_kind == store.INPUT_ATTRIBUTED:
        views, party, dm_names, owned = await _artifact_views(
            fixture.artifact, settings, _NoStorage()  # type: ignore[arg-type]
        )
        roster = CampaignRoster()
    else:
        views, party, dm_names, owned, roster = await _transcript_views(fixture, settings, llm)
    timings["views"] = round(time.monotonic() - mark, 1)

    mark = time.monotonic()
    extractions = await llm.extract_many(
        views,
        concurrency=settings.llm_chunk_concurrency,
        out_of_world=dm_names or None,
        owned=owned,
    )
    timings["extract"] = round(time.monotonic() - mark, 1)

    merged = merge_extractions(extractions, owned=owned, corpus="\n".join(views))
    # The v18 measurement of the span rule was run against beats with EMPTY
    # spans, because this call kept the old signature while the worker's was
    # updated: the compose prompt was told to use spans that never arrived, and
    # the result was reported as "the rule does not work". An eval that silently
    # exercises a different pipeline than the worker is worse than no eval, so
    # the failure is loud from here on.
    beats = merged.get("summary_beats") or []
    if owned and any(not beat.get("from") for beat in beats):
        raise AssertionError(
            "the merged beats carry no span: the compose call would be told to "
            "use markers that are not there (see evals/runner.py merge call)"
        )
    if dm_names:
        merged = exclude_character_names(merged, dm_names)
    if not roster.empty:
        # Mirrors process_job: the people at the table are not characters, and a
        # player's name standing in for their character in the prose is rewritten
        # to the character (app/roster.py).
        merged = exclude_character_names(merged, roster.player_names)
        merged = rename_characters(merged, roster.rename_map())

    # Mirrors the worker exactly: the eval must run the pipeline the worker runs,
    # or it reports confident numbers about a different program (which it did,
    # once, for three runs - see the note in the fixture README).
    merged["conflicts"] = conflicts_payload(
        find_conflicts(merged.get("summary_beats") or [])
    )

    gate_report: dict[str, Any] = {}
    # The worker gates on "there IS an artifact", not on the flag: with the flag on
    # and no attributed transcript it would have failed before reaching here
    # (generate._load_attribution). A transcript fixture therefore runs ungated,
    # which is the only honest thing to do - there is no certainty data to gate on.
    if fixture.input_kind == store.INPUT_ATTRIBUTED and settings.attribution_enabled:
        merged, gate_report = apply_gate(
            merged, fixture.artifact, player_names=_artifact_player_names(fixture.artifact)
        )

    beats = summary_lines(str(merged.get("session_summary") or ""))

    mark = time.monotonic()
    composed = await _compose_summary(
        llm, merged, scenes=_scene_reading(fixture.artifact), settings=settings
    )
    timings["compose"] = round(time.monotonic() - mark, 1)
    timings["total"] = round(time.monotonic() - started, 1)

    return {
        "views": views,
        "extractions": extractions,
        "merged": merged,
        "beats": beats,
        "blocks": composed,
        "narrative": blocks_to_text(composed) if composed else str(merged.get("session_summary") or ""),
        "party": party,
        "dm_names": dm_names,
        #: Where each chunk's part started and how big it was - recorded so a
        #: report can show that the partition and the budget reached the model
        #: rather than trusting that they did. Flattened because OwnedPart is a
        #: dataclass and these artefacts are written as JSON.
        "owned": [
            {"start": part.start, "end": part.end, "lines": part.lines} for part in owned
        ],
        #: The beats with the span each came from: what the compose call reads.
        "summary_beats": merged.get("summary_beats") or [],
        "gate": gate_report,
        "timings": timings,
        "input": fixture.input_kind,
        "settings": {
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_temperature": settings.llm_temperature,
            "prompt_version": settings.prompt_version,
            "chunk_tokens": settings.chunk_tokens,
            "chunk_overlap": settings.chunk_overlap,
            "attribution_enabled": settings.attribution_enabled,
            # The compression this run applied. Recorded per run because it is the
            # one setting that changes how much material the composer is handed,
            # and an A/B on it has to be readable from the artefacts afterwards.
            "beat_lines": settings.beat_lines,
        },
    }


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------


async def grade(
    fixture: store.Fixture, *, judge: Any | None, with_pipeline: bool
) -> list[RunResult]:
    """Grade the stored baseline, then a fresh run, and return both.

    A fixture with NO baseline.json skips step 1: its target is a human-written
    benchmark, not a previously shipped draft, and grading an empty narrative
    would pass every "must not contain" check vacuously and read as a green
    baseline - a report that says nothing while looking like it says something.
    """
    settings = get_settings()
    results: list[RunResult] = []

    # 1. the stored draft, offline. Cheap, and it is the self-test.
    baseline_blocks = list(fixture.baseline.get("summary_blocks") or [])
    baseline = RunResult(
        fixture=fixture.name,
        subject="baseline",
        narrative=str(fixture.baseline.get("summary") or ""),
        blocks=baseline_blocks,
        outcomes=await checks.evaluate(
            fixture.checks(),
            subject="baseline",
            narrative=str(fixture.baseline.get("summary") or ""),
            blocks=baseline_blocks,
            evidence_for=fixture.evidence,
            judge=judge,
        ),
    )
    if fixture.baseline:
        results.append(baseline)
    if not with_pipeline:
        return results

    # 2. a fresh run of the real pipeline.
    artefacts = await run_pipeline(fixture, settings)
    run = RunResult(
        fixture=fixture.name,
        subject="run",
        narrative=artefacts["narrative"],
        blocks=list(artefacts["blocks"]),
        beats=list(artefacts["beats"]),
        artefacts=artefacts,
        outcomes=await checks.evaluate(
            fixture.checks(),
            subject="run",
            narrative=artefacts["narrative"],
            blocks=list(artefacts["blocks"]),
            beats=list(artefacts["beats"]),
            # What the run would hand the wiki as pages, so a check can see a
            # character entry nobody should get ("Uomo urlante") even when the
            # prose around it reads fine.
            entities=[
                str(c.get("name") or "")
                for c in (artefacts["merged"].get("characters") or [])
            ],
            evidence_for=fixture.evidence,
            judge=judge,
        ),
    )
    results.append(run)

    # 4. the draft against the human summary of the same session, when the
    #    fixture carries one. This is the measurement the defect checks cannot
    #    make: a draft can pass every one of them and still be a worse summary
    #    than the one a person wrote for the same recording.
    if fixture.benchmark:
        run.comparison = await benchmark.compare(
            LLMClient(settings),
            fixture.name,
            run.narrative,
            fixture.benchmark,
            fixture.truth,
            judge=judge is not None,
        )
    return results


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

_MARK = {
    checks.PASS: "PASS",
    checks.FAIL: "FAIL",
    checks.UNSURE: "UNSURE",
    checks.SKIPPED: "skip",
}


def render_report(
    fixture: store.Fixture, results: Sequence[RunResult], *, when: str
) -> str:
    lines: list[str] = [
        f"# eval: {fixture.name}",
        "",
        f"- session `{fixture.session_id}`",
        f"- captured: {fixture.meta.get('captured_at')}",
        f"- run at: {when}",
        "",
    ]
    for result in results:
        title = "stored baseline (the draft that shipped)" if result.subject == "baseline" else "fresh run"
        lines += [f"## {title}", ""]
        if result.subject == "run" and result.artefacts:
            counts = {
                "chunks": len(result.artefacts["views"]),
                "beats": len(result.beats),
                "blocks": len(result.blocks),
                "chars": len(result.narrative),
            }
            parts = result.artefacts.get("owned") or []
            boundaries = [
                f"{p['start']}-{p['end']} ({p['lines']} lines)" for p in parts if p["start"]
            ]
            lines += [
                "  ".join(f"{key}={value}" for key, value in counts.items()),
                "",
                f"timings: {result.artefacts['timings']}",
                f"prompt: {result.artefacts['settings'].get('prompt_version')}",
                "",
            ]
            if boundaries:
                lines += [
                    "each chunk narrated from: " + ", ".join(boundaries),
                    "",
                ]
        lines += ["| check | status | detail |", "| --- | --- | --- |"]
        for outcome in result.outcomes:
            detail = outcome.detail.replace("|", "\\|")
            lines.append(f"| {outcome.id} | {_MARK.get(outcome.status, outcome.status)} | {detail} |")
        known = set(fixture.known_baseline_failures())
        if result.subject == "baseline":
            missing = [
                outcome.id
                for outcome in result.outcomes
                if outcome.id in known and outcome.status == checks.PASS
            ]
            if missing:
                lines += [
                    "",
                    (
                        "**The self-test is broken**: these checks are listed as known "
                        f"failures of the shipped draft but passed: {', '.join(missing)}"
                    ),
                ]
        lines.append("")
    if result.comparison is not None:
        lines += [benchmark.render(result.comparison), ""]
    return "\n".join(lines)


def write_outputs(result: RunResult, out_root: Path, when: str) -> Path:
    stamp = when.replace(":", "").replace("-", "").replace("+00:00", "Z")
    directory = out_root / result.fixture / stamp
    # --repeat runs two sessions inside the same second; a collision would make the
    # second run overwrite the first and quietly halve the sample.
    suffix = 2
    while directory.exists():
        directory = out_root / result.fixture / f"{stamp}-{suffix}"
        suffix += 1
    directory.mkdir(parents=True, exist_ok=True)
    result.out_dir = directory

    artefacts = result.artefacts
    if artefacts:
        _dump(directory / "views.json", artefacts["views"])
        _dump(directory / "extractions.json", artefacts["extractions"])
        _dump(directory / "merged.json", artefacts["merged"])
        _dump(directory / "blocks.json", artefacts["blocks"])
        _dump(
            directory / "run.json",
            {
                key: value
                for key, value in artefacts.items()
                if key not in {"views", "extractions", "merged", "blocks"}
            },
        )
        (directory / "beats.txt").write_text(
            "\n".join(f"{index}. {beat}" for index, beat in enumerate(result.beats, 1)),
            encoding="utf-8",
        )
    (directory / f"narrative.{result.subject}.md").write_text(
        result.narrative, encoding="utf-8"
    )
    if result.comparison is not None:
        _dump(directory / "comparison.json", result.comparison.as_dict())
    return directory


def _dump(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description=(
            "Grade a summary against a captured session. With no fixture named, "
            "every installed fixture is run - and an empty set is not an error."
        ),
    )
    parser.add_argument("fixtures", nargs="*", help="fixture names (default: all)")
    parser.add_argument("--list", action="store_true", help="show installed fixtures and exit")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="grade only the stored draft: no pipeline, no API cost",
    )
    parser.add_argument(
        "--judge",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "run the judge checks (the only ones that read meaning). Default: on "
            "for a full run, off for --baseline, so that grading the stored draft "
            "stays free. --judge --baseline is the cheap way to check that the "
            "semantic checks still fire on the draft that shipped."
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        metavar="N",
        help=(
            "run each fixture N times and print the aggregate. A single run is a "
            "sample: llm_temperature is 0.2, so the same code produces a different "
            "set of defects every time, and a change judged on one run is a coin "
            "toss. The aggregate reports per-check pass rates and the "
            "contradictions by how many runs produced them."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        metavar="T",
        help=(
            "sampling temperature for this run (default: the service setting, 0.2). "
            "USE 0 TO DECIDE A CHANGE AND 0.2 TO JUDGE THE RESULT: at 0.2 the same "
            "code produces 0 to 12 contradictions on the same fixture, so a prompt "
            "change smaller than about four contradictions cannot be resolved by "
            "five runs an arm - and a three-run sample resolves nothing at all. At 0 "
            "the pipeline is (nearly) deterministic, which makes 'did this sentence "
            "help?' a question two runs can answer. Runs at 0 are for comparing "
            "configurations; they say nothing about the spread a DM would see."
        ),
    )
    parser.add_argument("--out", default=None, help="output directory (default: evals/out)")
    parser.add_argument("--drop", metavar="NAME", help="delete a fixture and exit")
    parser.add_argument("--yes", action="store_true", help="confirm --drop")
    args = parser.parse_args(argv)

    if args.temperature is not None:
        # The settings object is the cached singleton every run reads, so this is
        # the one place a run's temperature is decided; the artefacts already
        # record it per run (settings.llm_temperature in run.json).
        get_settings().llm_temperature = float(args.temperature)

    if args.list:
        return _list()
    if args.drop:
        return _drop(args.drop, args.yes)

    names = args.fixtures or store.available()
    if not names:
        print(
            "no fixtures installed under "
            f"{store.FIXTURE_ROOT} - nothing to run. "
            "Capture one with: python -m evals.capture --session <uuid> --name <name>"
        )
        return 0

    out_root = Path(args.out) if args.out else Path(__file__).resolve().parent / "out"
    exit_code = 0
    for name in names:
        try:
            fixture = store.load(name)
        except store.FixtureError as exc:
            print(f"{name}: {exc}")
            exit_code = max(exit_code, 2)
            continue
        judge_enabled = args.judge if args.judge is not None else not args.baseline
        failures: list[str] = []
        for attempt in range(max(1, args.repeat)):
            if args.repeat > 1:
                print(f"\n===== {name}: run {attempt + 1} of {args.repeat} =====")
            try:
                outcome = asyncio.run(
                    _run_one(
                        fixture,
                        baseline_only=args.baseline,
                        # --baseline is documented as offline and free, so the judge
                        # - the only part that costs anything - stays off unless it is
                        # asked for explicitly. A full run grades the baseline too,
                        # and there it is on by default.
                        judge_enabled=judge_enabled,
                        out_root=out_root,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # A RUN THAT CRASHES IS A RESULT, NOT THE END OF THE MEASUREMENT.
                # The pipeline is stochastic and its loudest failure - a provider
                # cutting the answer off at the token cap - kills the whole job, so
                # a repeater that dies on the first one cannot measure anything: the
                # first five-run sample this harness took lasted one run. The
                # failure is counted, named and reported; the sample continues.
                failures.append(f"{type(exc).__name__}: {exc}")
                print(f"!!! {name} run {attempt + 1} FAILED: {type(exc).__name__}: {exc}")
                continue
            exit_code = max(exit_code, outcome)
        if args.repeat > 1 and not args.baseline:
            # The aggregate is the whole point of repeating: a rate across runs,
            # not a verdict from one.
            if failures:
                print(
                    f"\n**{len(failures)} of {max(1, args.repeat)} runs FAILED** "
                    "(the pipeline could not produce a draft at all):"
                )
                for failure in failures:
                    print(f"- {failure}")
            print("\n" + history.render(name, out_root=out_root))
        if failures:
            exit_code = max(exit_code, 1)
    return exit_code


async def _run_one(
    fixture: store.Fixture, *, baseline_only: bool, judge_enabled: bool, out_root: Path
) -> int:
    judge = make_judge(LLMClient(get_settings())) if judge_enabled else None

    when = datetime.now(UTC).isoformat(timespec="seconds")
    results = await grade(fixture, judge=judge, with_pipeline=not baseline_only)

    report = render_report(fixture, results, when=when)
    directories = [write_outputs(result, out_root, when) for result in results]
    if directories:
        (directories[0].parent / "report.md").write_text(report, encoding="utf-8")
    print(report)
    if directories:
        print(f"artefacts: {directories[0].parent}")
    else:
        print(f"{fixture.name}: nothing to grade offline (no baseline.json)")

    failed = [outcome for result in results for outcome in result.blocking_failures]
    if baseline_only:
        # The baseline is EXPECTED to fail; that is the self-test, and
        # tests/test_evals_fixtures.py enforces it. Exit status stays neutral.
        return 0
    return 1 if failed else 0


def _list() -> int:
    names = store.available()
    if not names:
        print(f"no fixtures installed under {store.FIXTURE_ROOT}")
        return 0
    for name in names:
        try:
            fixture = store.load(name)
        except store.FixtureError as exc:
            print(f"  {name}  [unusable: {exc}]")
            continue
        print(
            f"  {name:24} {store.size_mb(name):6.2f} MB  "
            f"{len(fixture.utterances()):5d} utterances  "
            f"{fixture.meta.get('captured_at', '?')}  "
            f"{len(fixture.checks())} checks"
        )
    return 0


def _drop(name: str, confirmed: bool) -> int:
    if not confirmed:
        print(f"refusing to delete {name!r} without --yes")
        return 2
    try:
        path = store.drop(name)
    except store.FixtureError as exc:
        print(f"{exc}")
        return 2
    print(f"removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
