"""Beats that look like one moment the session read twice.

The transcript is cut into fixed-size pieces so it can be read, and the pieces
OVERLAP so that an entity near a boundary is seen by both. For entities that is
exactly right. For the session's STORY it is the wrong thing, because two
readings that cannot see each other describe one scene twice in different words,
and the merger - which de-duplicates on identical text - keeps both:

    "Fuori dall'ospedale il gruppo incontra una ragazza che porta il pranzo a
     suo zio"            (one part)
    "Una nana porta il pranzo a suo zio, il primario dell'ospedale"   (the next)

The composer is handed both and writes them as two people. Measured, twice: a
rule in its prompt does not stop it, and neither does telling it which part of
the session each beat came from. The composer is the wrong place to fix a
contradiction - it can smooth, it cannot decide.

So this module does not try. It finds the pairs and says so, and the flag it
produces is shown to the Dungeon Master next to the draft: no question, no
roster pick, nothing to answer. That restraint is measured too. Asking was
tested (evals/probe_conflicts.py) and the precise detector here is BLIND to the
case above - "una cesta di vini" and "una nana porta il pranzo a suo zio" share
too few content words - while the detector that finds more is wrong about
two-thirds of the time. Nine unusable questions to surface one real conflict is
how the retired presence questions crowded out the ones that would have landed
(docs/attribution-ux.md S4.2). A flag costs a glance; a question costs an answer.

WHY THIS IS WORTH SHOWING EVEN AT LOW RECALL. It is precise: on every run
measured, a pair it reported was a real duplication, and it found the
room-entrance one in two runs out of three. The case it misses stays missed, but
nothing is lost by saying nothing about it. A flag nobody has to act on is
allowed to be incomplete; that is the whole difference from a question.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

#: How similar two beats must be before the pipeline says anything. Calibrated
#: on ONE session (evals/fixtures/bugie_inutili): the real duplication there
#: scored 0.38-0.45 and the worst innocent pair 0.16-0.31. One session is thin
#: evidence, which is why this is a flag and not a gate, and why the fixture
#: keeps the number honest.
DEFAULT_THRESHOLD = 0.35

#: Most conflicts stored for one session. A flag list longer than this is not a
#: flag any more, and a pathological run must not be able to fill the page.
MAX_CONFLICTS = 10

#: Words that carry no identity and would make every pair of beats look alike.
#: Italian and English, plus the words this pipeline's own prose over-uses.
_STOPWORD_TEXT = """
il lo la i gli le un uno una del della dei delle degli al alla ai alle nel nella
nei nelle dal dalla sul sulla per con fra tra che chi cui non piu meno come dove
quando mentre anche ancora dopo prima poi gia solo sono essere avere hanno ha ho
lui lei loro questo questa quello quella questi queste suo sua suoi sue
the a an of in on at to for with and or but is are is was were that this these those
his her their its from into over under then than when while after before again
party group gruppo poi cosi essere vengono viene fatto fare
dall dell nell sull coll quest
"""

_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def content_words(text: str) -> frozenset[str]:
    """Identity-bearing words of a sentence, for cheap similarity.

    Apostrophes split rather than join: in Italian the elided article is glued to
    its noun ("dall'ospedale"), and keeping it whole would hide the noun that two
    beats about the same place must have in common.
    """
    words = re.findall(r"\w+", (text or "").lower())
    return frozenset(w for w in words if len(w) > 3 and w not in _STOPWORDS)


def similarity(text_a: str, text_b: str) -> float:
    """Jaccard over the identity-bearing words of two beats."""
    words_a, words_b = content_words(text_a), content_words(text_b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


@dataclass(frozen=True)
class Conflict:
    """Two beats that look like one moment, each with the part it came from."""

    first_text: str
    first_from: str
    first_to: str
    second_text: str
    second_from: str
    second_to: str
    score: float

    def as_payload(self) -> dict[str, Any]:
        """The shape stored on the summary row and rendered on the session page."""
        return {
            "score": round(self.score, 2),
            "first": {
                "text": self.first_text,
                "from": self.first_from,
                "to": self.first_to,
            },
            "second": {
                "text": self.second_text,
                "from": self.second_from,
                "to": self.second_to,
            },
        }


def find_conflicts(
    beats: Sequence[dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = MAX_CONFLICTS,
) -> list[Conflict]:
    """The pairs of beats that look like one moment the session recorded twice.

    Only beats from DIFFERENT parts are compared. Two beats carrying the same
    span were written together, by one reading of one stretch: they are the
    normal shape of a summary and never this defect. The owned spans of two
    chunks never touch (chunking.owned_ranges), so "different parts" and "could
    not see each other" are the same condition here.

    A beat appears in at most ONE conflict. Two flags quoting the same sentence
    read as one flag repeated, and the real duplication on the fixture session
    produces exactly that: two pairs sharing a beat. The strongest pair wins.
    """
    scored: list[tuple[float, int, int]] = []
    for (index_a, beat_a), (index_b, beat_b) in combinations(enumerate(beats), 2):
        if _span(beat_a) == _span(beat_b):
            continue
        score = similarity(
            str(beat_a.get("text") or ""), str(beat_b.get("text") or "")
        )
        if score >= threshold:
            scored.append((score, index_a, index_b))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    claimed: set[int] = set()
    found: list[Conflict] = []
    for score, index_a, index_b in scored:
        if index_a in claimed or index_b in claimed:
            continue
        claimed.update({index_a, index_b})
        beat_a, beat_b = beats[index_a], beats[index_b]
        found.append(
            Conflict(
                first_text=str(beat_a.get("text") or ""),
                first_from=str(beat_a.get("from") or ""),
                first_to=str(beat_a.get("to") or ""),
                second_text=str(beat_b.get("text") or ""),
                second_from=str(beat_b.get("from") or ""),
                second_to=str(beat_b.get("to") or ""),
                score=score,
            )
        )
        if len(found) >= limit:
            break
    return found


def conflicts_payload(conflicts: Iterable[Conflict]) -> list[dict[str, Any]]:
    """The stored form of a conflict list (JSONB-friendly)."""
    return [conflict.as_payload() for conflict in conflicts]


def _span(beat: dict[str, Any]) -> tuple[str, str]:
    return (str(beat.get("from") or ""), str(beat.get("to") or ""))
