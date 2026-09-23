"""How far is a run's draft from the human-written summary of the same session?

WHY THIS EXISTS. The fixtures in this package used to assert defect classes
("this sentence names one person twice"). Those are pass/fail on a known bug. A
BENCHMARK is a different question: the session summary a human wrote for the same
recording sits next to the draft, and the draft is measured against it. That is
the only way to ask "is this summary as good as it should be", which is the
question a DM actually has.

THREE MEASUREMENTS, cheapest first:

* SHAPE - words, sentences, paragraph blocks. A benchmark of 250 words and a
  draft of 2,000 is not a detail problem, it is a compression problem, and no
  judge call is needed to see it.
* NAMES - the proper nouns the benchmark uses (Shiran, Sir Lucius, Fatumastra).
  A draft that never says who did what has not summarised the session, whatever
  its prose looks like. Deterministic, and it fails loudly.
* COVERAGE - one judge call that reads both and reports what the benchmark
  covers and the draft does not. This is the only one that can see a missing
  EVENT, which is what "level of detail" means in practice.

Every number here is a measurement of ONE run at llm_temperature > 0. Compare
classes of failure across runs, never a single run against another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ']+")
_SENTENCE = re.compile(r"[.!?]+\s|\n\n")

#: Words that are capitalized for reasons other than being a name.
_NOT_A_NAME = frozenset(
    {
        "il", "lo", "la", "i", "gli", "le", "un", "uno", "una",
        "questo", "questa", "quel", "quella", "poi", "dopo", "mentre", "intanto",
        "the", "a", "an", "and", "but", "then", "when", "while", "after",
    }
)


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(text or "") if part.strip()]


def words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text or "")


def proper_nouns(text: str) -> set[str]:
    """The names a summary uses: capitals that are NOT the first word of a sentence.

    Sentence-initial capitals in Italian are every sentence's first word ("Il
    gruppo..."), so without this exclusion the "names" of a summary would be its
    commonest articles and verbs.
    """
    found: set[str] = set()
    for sentence in sentences(text):
        for index, token in enumerate(_TOKEN.findall(sentence)):
            if index == 0 or len(token) <= 2 or not token[:1].isupper():
                continue
            if token.lower() not in _NOT_A_NAME:
                found.add(token)
    return found


@dataclass
class Comparison:
    """One draft measured against one benchmark."""

    fixture: str
    draft_words: int = 0
    benchmark_words: int = 0
    draft_sentences: int = 0
    benchmark_sentences: int = 0
    draft_blocks: int = 0
    benchmark_blocks: int = 0
    #: Names the benchmark uses and the draft never mentions.
    missing_names: list[str] = field(default_factory=list)
    #: Names the draft uses that the benchmark does not (not an error: the draft
    #: may name someone the benchmark left implicit - reported, not scored).
    extra_names: list[str] = field(default_factory=list)
    #: THE measurement that matters: the benchmark's own sentences the draft does
    #: not carry, decided one item at a time (see COVERAGE_SYSTEM_PROMPT).
    missing_content: list[str] = field(default_factory=list)
    #: Sentences of the benchmark, so the coverage ratio has a denominator.
    benchmark_items: int = 0
    #: Statements the ground truth says are FALSE.
    contradicted: list[str] = field(default_factory=list)
    #: Statements the ground truth does not mention at all. Reported apart from
    #: 'contradicted' on purpose: the human account is a summary too, and a moment
    #: it skipped is not a moment the draft got wrong.
    unsupported: list[str] = field(default_factory=list)
    judge_reason: str = ""
    judge_failed: str = ""
    #: THE OBJECTIVE'S OWN QUESTION, answered 1-5 per axis: is this draft as good
    #: as the summary a human wrote for the same session? 5 means "as good as the
    #: benchmark". Everything above measures agreement; this measures quality, and
    #: a draft can agree with the benchmark on every fact and still read worse.
    quality: dict[str, Any] = field(default_factory=dict)

    @property
    def content_coverage(self) -> float:
        """Fraction of the benchmark's claims the draft carries."""
        if not self.benchmark_items:
            return 0.0
        return round((self.benchmark_items - len(self.missing_content)) / self.benchmark_items, 3)

    @property
    def name_coverage(self) -> float:
        total = len(self.missing_names) + len(self.shared_names)
        return round(len(self.shared_names) / total, 3) if total else 1.0

    #: Filled by measure(); kept out of __init__ so the dataclass stays simple.
    shared_names: list[str] = field(default_factory=list)
    benchmark_names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "words": {"draft": self.draft_words, "benchmark": self.benchmark_words},
            "sentences": {
                "draft": self.draft_sentences,
                "benchmark": self.benchmark_sentences,
            },
            "blocks": {"draft": self.draft_blocks, "benchmark": self.benchmark_blocks},
            "names": {
                "coverage": self.name_coverage,
                "benchmark": self.benchmark_names,
                "shared": self.shared_names,
                "missing": self.missing_names,
                "extra": self.extra_names,
            },
            "content": {
                "coverage": self.content_coverage,
                "items": self.benchmark_items,
                "missing": self.missing_content,
            },
            "missing_content": self.missing_content,
            "contradicted": self.contradicted,
            "unsupported": self.unsupported,
            "quality": self.quality,
            "judge_reason": self.judge_reason,
            "judge_failed": self.judge_failed,
        }


def measure(fixture: str, draft: str, benchmark: str) -> Comparison:
    """The deterministic half: shape and names. No model involved."""
    result = Comparison(fixture=fixture)
    result.draft_words = len(words(draft))
    result.benchmark_words = len(words(benchmark))
    result.draft_sentences = len(sentences(draft))
    result.benchmark_sentences = len(sentences(benchmark))
    result.draft_blocks = len([p for p in (draft or "").split("\n\n") if p.strip()])
    result.benchmark_blocks = len([p for p in (benchmark or "").split("\n\n") if p.strip()])

    in_draft = {name.lower() for name in proper_nouns(draft)}
    names = proper_nouns(benchmark)
    result.benchmark_names = sorted(names)
    result.shared_names = sorted(n for n in names if n.lower() in in_draft)
    result.missing_names = sorted(n for n in names if n.lower() not in in_draft)
    result.extra_names = sorted(proper_nouns(draft) - names)
    return result


QUALITY_SYSTEM_PROMPT = """You are the editor of a tabletop RPG campaign wiki.

You receive the summary a HUMAN wrote for one session (the BENCHMARK - the
standard this campaign publishes) and a DRAFT an automatic pipeline produced from
the SAME recording.

Score the DRAFT against the benchmark on four axes, 1 to 5, where 5 means "as good
as the benchmark" and 3 means "usable but clearly worse":

* "coverage" - does it tell what matters, in proportion? A summary that buries the
  session's pivotal moments in minor ones scores low even if it mentions them.
* "accuracy" - is it right about WHO did what, and about what happened? A fluent
  sentence that gives a real fact to the wrong person is the failure that matters.
* "narrative" - is it a text a reader follows, or a list of statements? The
  benchmark is the register to judge by, not "more detail is better".
* "density" - does every sentence carry something, or is there padding, repetition
  and connective filler?

Be a hard judge. The benchmark is the standard, and a draft twice its length that
says the same things is not its equal. Score what you read, not what a summariser
could have done.

Respond with a single JSON object:

{"coverage": 4, "accuracy": 3, "narrative": 4, "density": 2, "overall": 3,
 "worst": "one line naming the draft's weakest point",
 "reason": "one sentence comparing it with the benchmark"}
"""


def parse_quality(payload: Any) -> dict[str, Any]:
    """The four axes + overall as numbers, clamped to 1-5; {} when unreadable."""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for axis in ("coverage", "accuracy", "narrative", "density", "overall"):
        value = payload.get(axis)
        try:
            out[axis] = max(1, min(5, round(float(value))))
        except (TypeError, ValueError):
            continue
    for text_key in ("worst", "reason"):
        text = str(payload.get(text_key) or "").strip()
        if text:
            out[text_key] = text
    return out


async def judge_quality(
    llm: Any, draft: str, benchmark: str, truth: str = ""
) -> tuple[dict[str, Any], str]:
    """(scores, failure) - one call, never fatal."""
    message = (
        f"BENCHMARK (the summary this campaign published, the standard):\n{benchmark.strip()}\n\n"
        + (
            f"GROUND TRUTH (a longer human account of the same session):\n{truth.strip()}\n\n"
            if truth
            else ""
        )
        + f"DRAFT (the pipeline's summary of the same recording):\n{draft.strip()}\n\n"
        "Score the draft against the benchmark."
    )
    try:
        payload = await llm._complete_json(
            QUALITY_SYSTEM_PROMPT, message, "the draft quality", extraction=False
        )
    except Exception as exc:  # noqa: BLE001 - a failed comparison is not a failed run
        return {}, f"{type(exc).__name__}: {exc}"
    return parse_quality(payload), ""


COVERAGE_SYSTEM_PROMPT = """You compare two accounts of the same tabletop RPG session.

BENCHMARK: the summary a human wrote for the recording.
DRAFT: the summary an automatic pipeline produced for the SAME recording.

You are not grading style. You are finding what the DRAFT leaves out or gets
wrong, judged ONLY against the benchmark and the ground-truth account you are
given. A fact the benchmark states and the draft omits is a MISSING item. A fact
the draft states that the ground truth contradicts is a CONTRADICTION.

YOU DECIDE ONE BENCHMARK ITEM AT A TIME, in the order given, and you decide each
one on its own. This is not a preference: an earlier version of this prompt asked
for "what is missing" as a free list, and on a draft that plainly contained Miles
Falco, the mirror, the indigo paintbrush and the explosion it reported all four as
missing - a measurement that says a fact is absent when it is present is worse
than no measurement at all. A per-item verdict forces the answer for each one.

Reworded IS covered: if the draft says the same thing in other words, or states
it as part of a longer sentence, the item is COVERED. An item is NOT covered when
the draft never says it, or says something weaker that leaves the fact out (a
draft that mentions "un pennello" but never the indigo dye has not covered the
indigo paintbrush).

Respond with a single JSON object:

{"items": [{"n": 1, "covered": true, "draft_quote": "", "note": ""}],
 "contradicted": ["..."], "reason": "one sentence"}

* "items": one entry for EVERY number you were given, in order. "draft_quote"
  is the draft's own words that cover the item ("" when not covered); "note" is
  a short English phrase naming what is missing, when it is.
* "contradicted": the draft's statements the ground truth says are FALSE - it
  states the other thing. Quote the draft. Empty when there are none.
* "unsupported": the draft's statements the ground truth does not mention AT ALL.
  Quote the draft. This is a different thing from a contradiction and must not be
  merged with it: an ambiguity the recording carries and the human summary skipped
  lands here, and calling that a falsehood is how a run gets blamed for being
  faithful. On a real session the draft read "il paziente dice di chiamarsi
  Galgith, ma il gruppo conosce un altro Galgith" - both halves of a contested
  moment the recording really contains - and the human account simply does not
  mention the moment.
* "reason": one sentence summarising how complete the draft is.
"""


def benchmark_items(benchmark: str, *, min_chars: int = 15) -> list[str]:
    """The benchmark split into the statements a draft can be checked against.

    One item per sentence. The benchmark is a single dense paragraph, so its
    sentences ARE its claims: this is what makes the coverage verdict auditable -
    the judge answers per item, and the report can show which sentence of the
    human summary the draft failed to carry.
    """
    return [s for s in sentences(benchmark) if len(s) >= min_chars]


async def judge_coverage(
    llm: Any, draft: str, benchmark: str, truth: str = ""
) -> tuple[list[str], list[str], list[str], str, str]:
    """(missing, contradicted, unsupported, reason, failure) - one call, never fatal."""
    items = benchmark_items(benchmark)
    numbered = "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))
    message = (
        f"BENCHMARK ITEMS (the human summary, one claim per line):\n{numbered}\n\n"
        + (
            f"GROUND TRUTH (a longer human account of the same session):\n{truth.strip()}\n\n"
            if truth
            else ""
        )
        + f"DRAFT (the pipeline's summary):\n{draft.strip()}\n\n"
        f"Decide all {len(items)} items."
    )
    try:
        payload = await llm._complete_json(
            COVERAGE_SYSTEM_PROMPT, message, "the benchmark coverage", extraction=False
        )
    except Exception as exc:  # noqa: BLE001 - a failed comparison is not a failed run
        return [], [], [], "", f"{type(exc).__name__}: {exc}"
    # A verdict the judge did not return is "not covered": the draft has to have
    # been shown to carry an item, not assumed to.
    verdicts = {
        int(entry.get("n")): bool(entry.get("covered"))
        for entry in (payload.get("items") or [])
        if str(entry.get("n") or "").strip().isdigit()
    }
    missing = [
        item for index, item in enumerate(items, 1) if not verdicts.get(index, False)
    ]
    contradicted = [
        str(item).strip() for item in (payload.get("contradicted") or []) if str(item).strip()
    ]
    unsupported = [
        str(item).strip() for item in (payload.get("unsupported") or []) if str(item).strip()
    ]
    return (
        missing,
        contradicted,
        unsupported,
        str(payload.get("reason") or "").strip(),
        "",
    )


async def compare(
    llm: Any, fixture: str, draft: str, benchmark: str, truth: str = "", *, judge: bool = True
) -> Comparison:
    """Everything this module knows about one draft against one benchmark."""
    result = measure(fixture, draft, benchmark)
    result.benchmark_items = len(benchmark_items(benchmark))
    if judge and benchmark.strip() and draft.strip():
        (
            result.missing_content,
            result.contradicted,
            result.unsupported,
            result.judge_reason,
            result.judge_failed,
        ) = await judge_coverage(llm, draft, benchmark, truth)
    if judge and benchmark.strip() and draft.strip():
        result.quality, quality_failure = await judge_quality(llm, draft, benchmark, truth)
        if quality_failure and not result.judge_failed:
            result.judge_failed = f"quality judge: {quality_failure}"
    return result


def render(result: Comparison) -> str:
    """The comparison as the markdown a run report carries."""
    lines = [
        "## benchmark comparison",
        "",
    ]
    if result.quality:
        scores = " | ".join(
            f"{axis} {result.quality.get(axis, '?')}"
            for axis in ("overall", "coverage", "accuracy", "narrative", "density")
        )
        lines += [f"**quality vs the benchmark (5 = as good):** {scores}", ""]
        if result.quality.get("worst"):
            lines += [f"_weakest: {result.quality['worst']}_", ""]
    lines += [
        "| | draft | benchmark |",
        "| --- | --- | --- |",
        f"| words | {result.draft_words} | {result.benchmark_words} |",
        f"| sentences | {result.draft_sentences} | {result.benchmark_sentences} |",
        f"| blocks | {result.draft_blocks} | {result.benchmark_blocks} |",
        (
            f"| name coverage | {result.name_coverage:.0%} "
            f"({len(result.shared_names)}/{len(result.benchmark_names)}) | - |"
        ),
        (
            f"| content coverage | {result.content_coverage:.0%} "
            f"({result.benchmark_items - len(result.missing_content)}"
            f"/{result.benchmark_items} benchmark sentences carried) | - |"
        ),
        "",
    ]
    if result.missing_names:
        lines += [
            "**names the benchmark uses and the draft never mentions:** "
            + ", ".join(result.missing_names),
            "",
        ]
    if result.judge_failed:
        lines += [f"_coverage judge failed: {result.judge_failed}_", ""]
    if result.missing_content:
        lines += ["**what the benchmark covers and the draft does not:**", ""]
        lines += [f"{index}. {item}" for index, item in enumerate(result.missing_content, 1)]
        lines.append("")
    if result.contradicted:
        lines += ["**statements the ground truth says are false:**", ""]
        lines += [f"- {item}" for item in result.contradicted]
        lines.append("")
    if result.unsupported:
        lines += [
            "**statements the ground truth does not mention** (not the same as false:",
            "the human account is a summary too):",
            "",
        ]
        lines += [f"- {item}" for item in result.unsupported[:8]]
        lines.append("")
    if result.judge_reason:
        lines += [f"_{result.judge_reason}_", ""]
    return "\n".join(lines)
