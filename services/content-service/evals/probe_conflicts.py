"""Probe: would a conflict detector be worth asking the DM about?

NOT A CHECK. This measures a feature that does not exist yet, so that the
decision to build it is made on a number instead of on an argument. A check says
whether the pipeline is good enough; this says whether a PROPOSED addition would
earn its place.

The proposal (see the fixture README): the summary review is passive - the DM has
to notice a contradiction unaided. Two beats written from different parts of the
session can describe the same being in different words, and the composer, which
sees both, is the wrong place to fix that; the evidence says so. So detect the
conflict where it becomes visible, and ASK: "these two lines may be the same
moment - one, or two?".

What this probe answers, on real runs of the real pipeline:

* how many conflicts each candidate detector proposes per session (the DM's
  cost: every proposal is a question they have to answer);
* whether the detector finds the conflict we KNOW is there (the girl carrying
  lunch, described as "una ragazza" from one part and "una nana" from the next);
* and it prints, for every proposal, the transcript lines that mention the same
  words - so the verdict on each one can be read off the recording rather than
  taken from a second model. No LLM judges this probe: a detector and a judge
  built from the same model agree with each other, not with the session.

Two detectors, because they fail differently:

* LEXICAL - different spans, content-word Jaccard over a threshold. Free,
  reproducible, blind to meaning: it scores 0.45 where a true duplicate would
  score 1.0, because the two calls that wrote the real one disagreed about the
  words.
* SEMANTIC - one call over the merged beats asking which pairs describe the same
  being or situation. Costs a call, sees meaning, and cannot be reasoned about
  without measuring it.

Run from the service directory (see evals/README.md for the container form):

    python -m evals.probe_conflicts --fixture bugie_inutili --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.extraction import LLMClient
from app.merger import merge_extractions
from app.workers.generate import _artifact_views
from evals import fixtures as store
from evals.checks import content_words

#: The lexical detector's threshold. Same value the fixture's advisory
#: no-duplicated-moments check uses, and the reason that check is advisory: on
#: this session the real duplicate scores 0.45 and the worst innocent pair 0.31.
LEXICAL_THRESHOLD = 0.35

#: How many transcript lines to print as evidence per beat. Enough to decide by
#: reading, few enough to read thirty of them.
EVIDENCE_LINES = 5


class _NoStorage:
    """_artifact_views takes a storage handle this path must never touch."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"_artifact_views reached for storage.{name}")


SEMANTIC_SYSTEM_PROMPT = """You are auditing a tabletop RPG session record for ONE defect.

You receive the beats of a session in order. Each beat is labelled with the span
of the session it was read from, in square brackets.

The session was cut into fixed-size pieces so it could be read, and the pieces
OVERLAP. A scene that falls on a cut is therefore read from both sides, by
readings that could not see each other, and described twice in different words.
The result is a summary in which ONE person or thing appears as two.

Report the pairs of beats that describe the SAME being, object or situation in
the same place, and that should therefore be told ONCE. Two beats carrying the
SAME span were written together by one reading and are never this defect.

Be strict. A pair that merely shares a scene, a place or a topic is NOT the
defect. Only report a pair when reading both as separate things is the mistake.

Respond with a single JSON object:

{"pairs": [{"a": 1, "b": 4, "why": "<one sentence>"}]}

"a" and "b" are the 1-based numbers of the beats. Use an empty list when there
is nothing to report: that is the normal answer.
"""


@dataclass
class Proposal:
    detector: str
    run: int
    a: int
    b: int
    score: float | None
    why: str = ""


# --------------------------------------------------------------------------
# the two detectors
# --------------------------------------------------------------------------


def lexical_pairs(beats: list[dict[str, str]]) -> list[Proposal]:
    """Different spans, similar words. Cheap, reproducible, blind to meaning."""
    out: list[Proposal] = []
    for (index_a, beat_a), (index_b, beat_b) in combinations(enumerate(beats), 2):
        if (beat_a.get("from"), beat_a.get("to")) == (
            beat_b.get("from"),
            beat_b.get("to"),
        ):
            continue  # one reading wrote both: not the defect
        words_a, words_b = content_words(beat_a["text"]), content_words(beat_b["text"])
        if not words_a or not words_b:
            continue
        score = len(words_a & words_b) / len(words_a | words_b)
        if score >= LEXICAL_THRESHOLD:
            out.append(
                Proposal("lexical", 0, index_a, index_b, round(score, 3))
            )
    return sorted(out, key=lambda proposal: -(proposal.score or 0.0))


async def semantic_pairs(llm: LLMClient, beats: list[dict[str, str]]) -> list[Proposal]:
    """One call over the merged beats: which pairs are one thing read twice."""
    listing = "\n".join(
        f'{number}. [{beat.get("from")}-{beat.get("to")}] {beat["text"]}'
        for number, beat in enumerate(beats, start=1)
    )
    payload = await llm._complete_json(
        SEMANTIC_SYSTEM_PROMPT,
        f"Beats:\n{listing}\n\nWhich pairs describe the same thing?",
        "the conflict probe",
        extraction=False,
    )
    out: list[Proposal] = []
    for pair in payload.get("pairs") or []:
        if not isinstance(pair, dict):
            continue
        try:
            a, b = int(pair["a"]) - 1, int(pair["b"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= a < len(beats) and 0 <= b < len(beats) and a != b:
            out.append(Proposal("semantic", 0, a, b, None, str(pair.get("why") or "")))
    return out


# --------------------------------------------------------------------------
# reading the evidence off the recording
# --------------------------------------------------------------------------


_REF_NUMBER = re.compile(r"u_(\d+)")


def ref_number(ref: str) -> int:
    match = _REF_NUMBER.search(ref or "")
    return int(match.group(1)) if match else -1


def evidence_lines(
    utterances: list[dict[str, Any]], span: tuple[str, str], text: str
) -> list[str]:
    """The lines of a span that mention what the beat is about.

    The beat does not say WHICH of its span's lines produced it, and a span is a
    hundred and fifty lines: printing all of them would bury the decision. So the
    lines that share a content word with the beat are shown, most relevant first.
    """
    low, high = ref_number(span[0]), ref_number(span[1])
    wanted = content_words(text)
    scored: list[tuple[int, str]] = []
    for utterance in utterances:
        number = ref_number(str(utterance.get("id") or ""))
        if not (low <= number <= high):
            continue
        overlap = len(content_words(str(utterance.get("text") or "")) & wanted)
        if not overlap:
            continue
        speaker = utterance.get("speaker") or {}
        name = speaker.get("character_name") or speaker.get("label") or "?"
        at = int(float(utterance.get("start") or 0))
        said = str(utterance.get("text") or "")[:150]
        scored.append((overlap, f'{utterance.get("id")} [{at}s] {name}: {said}'))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [line for _, line in scored[:EVIDENCE_LINES]]


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------


async def run_once(llm: LLMClient, fixture: store.Fixture, settings: Any) -> dict[str, Any]:
    views, _party, dm_names, owned = await _artifact_views(
        fixture.artifact, settings, _NoStorage()  # type: ignore[arg-type]
    )
    extractions = await llm.extract_many(
        views,
        concurrency=settings.llm_chunk_concurrency,
        out_of_world=dm_names or None,
        owned=owned,
    )
    merged = merge_extractions(extractions, owned=owned)
    beats = list(merged.get("summary_beats") or [])
    return {
        "beats": beats,
        "lexical": lexical_pairs(beats),
        "semantic": await semantic_pairs(llm, beats),
    }


def report(run: int, result: dict[str, Any], utterances: list[dict[str, Any]]) -> None:
    beats = result["beats"]
    print(f"\n===== run {run}: {len(beats)} beats, {len({
        (b.get('from'), b.get('to')) for b in beats})} spans =====")
    for detector in ("lexical", "semantic"):
        proposals = result[detector]
        print(f"  {detector.upper()}: {len(proposals)} proposal(s)")
        for number, proposal in enumerate(proposals, start=1):
            beat_a, beat_b = beats[proposal.a], beats[proposal.b]
            score = f" score={proposal.score}" if proposal.score is not None else ""
            print(f"   {detector[0]}{number}{score}")
            print(f"     A [{beat_a.get('from')}-{beat_a.get('to')}] {beat_a['text'][:150]}")
            print(f"     B [{beat_b.get('from')}-{beat_b.get('to')}] {beat_b['text'][:150]}")
            if proposal.why:
                print(f"     detector says: {proposal.why}")
            for label, beat in (("A", beat_a), ("B", beat_b)):
                span = (str(beat.get("from") or ""), str(beat.get("to") or ""))
                for line in evidence_lines(utterances, span, beat["text"]):
                    print(f"       {label}| {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.probe_conflicts")
    parser.add_argument("--fixture", default="bugie_inutili")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    fixture = store.load(args.fixture)
    settings = get_settings()
    llm = LLMClient(settings)
    out_root = (
        Path(args.out) if args.out else Path(__file__).resolve().parent / "out" / "conflicts"
    )
    out_root.mkdir(parents=True, exist_ok=True)

    utterances = fixture.utterances()
    collected: list[dict[str, Any]] = []
    for run in range(1, args.runs + 1):
        result = asyncio.run(run_once(llm, fixture, settings))
        report(run, result, utterances)
        collected.append(
            {
                "run": run,
                "beats": result["beats"],
                "lexical": [vars(p) for p in result["lexical"]],
                "semantic": [vars(p) for p in result["semantic"]],
            }
        )

    (out_root / f"{args.fixture}.json").write_text(
        json.dumps(collected, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\nwritten to {out_root / (args.fixture + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
