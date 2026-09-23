"""The checks a fixture asserts.

PURE BY CONSTRUCTION. This module imports nothing from app/, opens no socket and
makes no LLM call of its own: the judge is INJECTED as `async (criterion,
narrative, evidence, if_absent) -> dict`. Two things follow, and both are load
bearing for keeping fixtures removable:

* the check engine is unit-testable with a synthetic fixture in tmp_path, so the
  harness keeps full coverage after every real fixture has been deleted;
* the same engine grades the stored bad draft (baseline.json) offline, with no
  model in the loop, which is how the checks prove they actually detect the
  defects they claim to detect.

WHAT A CHECK LOOKS LIKE (see a fixture's expected.yaml):

    - id: mirror-provided-by-doctor
      kind: judge                 # judge | min_beats | max_duplicate_similarity
                                  #      | labels_not_english | text_not_matches
                                  #      | entity_names_not_matching (run)
      applies_to: text            # text (baseline + run) | run (needs the pipeline)
      blocking: true
      criterion: >
        The narrative says the mirror was obtained through the half-orc doctor...
      evidence: [u_00346, u_00351]
      if_absent: pass             # what to do when the narrative is silent

TWO KINDS OF CHECK, ON PURPOSE. `judge` asks a question the way a reader would,
which is the only way to catch a sentence that states a real fact about the wrong
person. The deterministic kinds are proxies: they are cheap, reproducible and
they cannot be talked out of a verdict, but they measure shape, not meaning.
Both are needed - the proxies catch the classes we know, the judge catches the
sentence nobody wrote a regex for.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.conflicts import content_words

PASS = "pass"
FAIL = "fail"
UNSURE = "unsure"
SKIPPED = "skipped"

#: Judge signature: (criterion, narrative, evidence, if_absent) -> verdict dict.
Judge = Callable[[str, str, Sequence[str], str], Awaitable[dict[str, Any]]]

#: The three outcomes that mean "this check did not pass".
NOT_PASSED = frozenset({FAIL, UNSURE})


@dataclass(frozen=True)
class Outcome:
    """One check's verdict, and the words that decided it."""

    id: str
    kind: str
    status: str
    detail: str = ""
    blocking: bool = True
    subject: str = "run"

    @property
    def passed(self) -> bool:
        return self.status == PASS

    @property
    def blocks(self) -> bool:
        """Whether this outcome should fail the run (skipped and advisory never do)."""
        return self.blocking and self.status in NOT_PASSED


class EvidenceLookup(Protocol):
    def __call__(self, refs: list[str]) -> list[str]: ...


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


async def evaluate(
    specs: Sequence[dict[str, Any]],
    *,
    subject: str,
    narrative: str,
    blocks: Sequence[dict[str, Any]] | None = None,
    beats: Sequence[str] | None = None,
    entities: Sequence[str] | None = None,
    evidence_for: EvidenceLookup | None = None,
    judge: Judge | None = None,
) -> list[Outcome]:
    """Grade one narrative (a run, or the stored baseline) against the specs.

    'subject' names what is being graded and is carried on every outcome, so a
    report can put the shipped draft and a fresh run side by side without
    guessing which rows belong to which.
    """
    outcomes: list[Outcome] = []
    for spec in specs:
        check_id = str(spec.get("id") or "?")
        kind = str(spec.get("kind") or "judge")
        blocking = bool(spec.get("blocking", True))
        applies_to = str(spec.get("applies_to") or "text")

        if applies_to == "run" and subject != "run":
            outcomes.append(
                Outcome(check_id, kind, SKIPPED, "needs a pipeline run", blocking, subject)
            )
            continue

        try:
            outcome = await _run_one(
                spec,
                kind=kind,
                blocking=blocking,
                subject=subject,
                narrative=narrative,
                blocks=blocks or [],
                beats=beats or [],
                entities=entities or [],
                evidence_for=evidence_for,
                judge=judge,
            )
        except Exception as exc:  # noqa: BLE001 — a broken check must not take the report down
            outcome = Outcome(
                check_id, kind, SKIPPED, f"check raised {type(exc).__name__}: {exc}",
                blocking, subject,
            )
        outcomes.append(outcome)
    return outcomes


async def _run_one(
    spec: dict[str, Any],
    *,
    kind: str,
    blocking: bool,
    subject: str,
    narrative: str,
    blocks: Sequence[dict[str, Any]],
    beats: Sequence[str],
    entities: Sequence[str],
    evidence_for: EvidenceLookup | None,
    judge: Judge | None,
) -> Outcome:
    check_id = str(spec.get("id") or "?")

    def out(status: str, detail: str) -> Outcome:
        return Outcome(check_id, kind, status, detail, blocking, subject)

    if kind == "judge":
        if judge is None:
            return out(SKIPPED, "no judge available (pass --judge)")
        criterion = str(spec.get("criterion") or "").strip()
        if not criterion:
            return out(SKIPPED, "the check has no criterion")
        refs = [str(r) for r in (spec.get("evidence") or [])]
        evidence = list(evidence_for(refs)) if evidence_for else []
        if refs and not evidence:
            return out(SKIPPED, f"none of the evidence refs {refs} is in the fixture")
        verdict = await judge(criterion, narrative, evidence, str(spec.get("if_absent") or UNSURE))
        status = str(verdict.get("verdict") or UNSURE).lower()
        if status not in {PASS, FAIL, UNSURE}:
            status = UNSURE
        quote = str(verdict.get("quote") or "").strip()
        reason = str(verdict.get("reason") or "").strip()
        detail = reason + (f' | "{quote}"' if quote else "")
        return out(status, detail)

    if kind == "min_beats":
        want = int(spec.get("value") or 0)
        got = len(beats)
        return out(
            PASS if got >= want else FAIL,
            f"{got} beats (want >= {want})" + _note(spec),
        )

    if kind == "max_duplicate_similarity":
        limit = float(spec.get("value") or 0.5)
        min_words = int(spec.get("min_words") or 5)
        worst, pair = _most_similar(beats, min_words)
        if pair is None:
            return out(PASS, "no two beats are near-duplicates")
        status = PASS if worst <= limit else FAIL
        return out(status, f"worst pair {worst:.2f} (limit {limit}): {pair[0]!r} / {pair[1]!r}")

    if kind == "entity_names_not_matching":
        # The drafted ENTITY names (what becomes a wiki page), not the prose. A
        # summary can read perfectly and still hand the DM a page called "Uomo
        # urlante"; this is the check that sees it.
        pattern = str(spec.get("pattern") or "")
        if not pattern:
            return out(SKIPPED, "the check has no pattern")
        offenders = [n for n in entities if re.search(pattern, n, re.IGNORECASE)]
        return out(
            PASS if not offenders else FAIL,
            (
                f"{len(entities)} character(s), none matching {pattern!r}"
                if not offenders
                else f"drafted as characters: {', '.join(sorted(set(offenders)))}"
            ),
        )

    if kind == "labels_not_english":
        return out(*labels_not_english(blocks))

    if kind in {"text_matches", "text_not_matches"}:
        pattern = str(spec.get("pattern") or "")
        if not pattern:
            return out(SKIPPED, "the check has no pattern")
        found = re.search(pattern, narrative, re.IGNORECASE)
        if kind == "text_matches":
            return out(PASS if found else FAIL, f"pattern {pattern!r} " + ("found" if found else "absent"))
        return out(FAIL if found else PASS, f"forbidden pattern {pattern!r} " + ("found" if found else "absent"))

    return out(SKIPPED, f"unknown check kind {kind!r}")


def _note(spec: dict[str, Any]) -> str:
    note = str(spec.get("note") or "").strip()
    return f" - {note}" if note else ""


#: Unambiguous English function words. Deliberately NOT content words: a
#: campaign may legitimately have an English place name, and a check that fires
#: on those would be noise. These are the words a scene label only carries when
#: the line was written in English.
_ENGLISH_MARKERS = frozenset(
    {
        "the", "of", "and", "with", "outside", "inside", "upstairs", "downstairs",
        "where", "from", "into", "near",
    }
)


def labels_not_english(blocks: Sequence[dict[str, Any]]) -> tuple[str, str]:
    """Scene labels must be written in the table's language, not the engine's.

    The v15 draft labelled its blocks with the attribution engine's own reading
    of where a scene happens ("the hospital in the city") inside an Italian
    narrative, because the stretch places are produced by an English-language
    prompt and handed to the composer as-is. This detects the case that actually
    exists - English markers in a label - rather than language in general: the
    composer is free to name a place anything the extraction supports.
    """
    offenders: list[str] = []
    for block in blocks:
        label = str(block.get("location") or "").strip()
        if not label:
            continue
        words = set(re.findall(r"[a-z]+", label.lower()))
        if words & _ENGLISH_MARKERS:
            offenders.append(label)
    if not offenders:
        return PASS, f"{len(blocks)} block(s), no English markers in their labels"
    return FAIL, "labels written in English: " + "; ".join(sorted(set(offenders)))


# --------------------------------------------------------------------------
# text similarity (the duplicate-beat proxy)
# --------------------------------------------------------------------------

# The ruler is NOT defined here. app/conflicts.py owns it, because the pipeline
# uses the same measurement in production to flag a duplicated beat for the DM,
# and a check that graded the output with a different ruler than the one the
# pipeline reasons with would be measuring a different thing. Importing it is
# what keeps them one ruler; app/conflicts.py is pure, so this module stays
# importable without the service's runtime dependencies.


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _most_similar(
    beats: Sequence[str], min_words: int
) -> tuple[float, tuple[str, str] | None]:
    """The most similar pair of beats, excluding pairs too short to judge.

    A PROXY, and labelled as one: it finds "the same moment written twice in
    different words", which is what the 10% chunk overlap produced, but two
    genuine beats about one scene also share vocabulary. It is a smoke alarm,
    not a proof - which is why a fixture can mark it non-blocking.
    """
    sets = [(beat, content_words(beat)) for beat in beats]
    sets = [(beat, words) for beat, words in sets if len(words) >= min_words]
    worst = 0.0
    worst_pair: tuple[str, str] | None = None
    for index, (left, left_words) in enumerate(sets):
        for right, right_words in sets[index + 1 :]:
            score = _jaccard(left_words, right_words)
            if score > worst:
                worst, worst_pair = score, (left, right)
    return worst, worst_pair


def _normalise(text: str) -> str:
    return " ".join(re.findall(r"[\w]+", (text or "").lower()))
