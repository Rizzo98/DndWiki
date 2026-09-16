"""Scenes: where the session happens, and who the transcript shows is there.

A session is not one conversation in one place. The party moves, the scene cuts,
a player leaves the table for twenty minutes. Every one of those is a fact about
WHO COULD BE SPEAKING, and the engine had no representation of it: an
identity_note ("Keth isn't here tonight") was the only absence it could express,
and only when somebody said it out loud.

This asks the model for that structure ONCE per session, over the session's own
text (docs/attribution-model.md S12.6). It is a separate call rather than another
field of the per-utterance pass for one structural reason: that pass is CHUNKED
and its chunks run CONCURRENTLY, so no chunk knows where the previous one ended.
A scene key emitted per chunk would be named inconsistently ("tavern" / "the
tavern" / "la locanda") and a stretch straddling a boundary would be split in two.
Stretch identity belongs to the whole session, so it is read from the whole
session.

It reads the TEXT and not the gists the evidence pass produced, and that is
measured rather than assumed: the gists are subject-less by design ("casts
Fireball on the three goblins"), so a reading built on them put every character
in every scene - the words that say who is in the room, and who has just walked
out of it, are exactly what the gist pass removes. The whole session's text is
one call, because this pass is not chunked per stretch.

What is asked for, and what is NOT:

* a "scene" is a stretch in which THE SAME PEOPLE are together in the same place -
  so a new one starts when somebody JOINS OR LEAVES, not only when the party moves.
  This is the part that carries the signal: on a real session it restricted the
  candidate set for 227 of 404 moments, because the party splits and a summary
  never mentions it.
* "absent" is what the engine acts on: the characters the record puts SOMEWHERE
  ELSE, with the event that took them there as the stretch's reason. The engine
  keeps this conservative - only a stated absence is evidence, and an absence
  nobody stated is not an absence (that is why "present" alone is not used as a
  penalty: a list that is merely incomplete would silence the quiet player).
* The Dungeon Master narrates every stretch and is never absent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

#: Bumped when the scene prompt changes shape; stored so a re-run can tell which
#: structure a session was read with.
SCENE_PROMPT_VERSION = "attr-scenes-v1"

SCENES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "first": {"type": "string"},
                    "last": {"type": "string"},
                    "present": {"type": "array", "items": {"type": "string"}},
                    "absent": {"type": "array", "items": {"type": "string"}},
                    "npcs": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["first", "last"],
            },
        },
    },
    "required": ["scenes"],
}

SCENE_SYSTEM_PROMPT = '''You are reading the record of a tabletop RPG (Dungeons & Dragons) session, one line per moment.

Your job is NOT to summarise the session and NOT to decide who was speaking. It is to find the stretches in which THE SAME PEOPLE ARE TOGETHER IN THE SAME PLACE, and to say who is there.

This is the single most useful thing you can tell us, because a character who is somewhere else CANNOT be the one speaking. So the party splitting up is the most important thing to find, and it is the thing a summary never mentions.

You will receive the session as numbered moments:

  [u_00412 00:41:15 narration] pushes the door open onto a ruin

The reference identifies the moment: ALWAYS echo it back exactly.

For every stretch, in order, covering the whole session with no gaps:

* "location": where it happens, in a few words ("the tavern in Helm's Reach",
  "the road north", "the crypt, lower level"). Use "unclear" when the record does
  not say.
* "first" / "last": the reference of the first and the last moment of the stretch.
  A NEW STRETCH STARTS when the place changes, when there is a hard cut ("the next
  morning", "meanwhile"), AND - this is the one that matters - WHEN SOMEBODY
  JOINS OR LEAVES THE GROUP ("Letho goes back to the inn", "Dalia and Shiran
  reach the cart while the others follow the guards"). Two moments in the same
  place with different people in it are TWO stretches, not one.
* "present": the characters who are in that place during that stretch. Use the
  roster's character names exactly as the roster writes them. A character who is
  quiet is still present when the record places them there.
* "absent": the characters who are NOT in that place during that stretch, when
  the record shows where they are instead ("Letho went back to the inn", "the
  others stayed with the guards"). This is what tells the engine who could have
  spoken, so state it whenever the record supports it.
* "npcs": non-player characters in play in the stretch ("the innkeeper", "Dolan").
* "reason": the event that starts this stretch ("Letho leaves the group"), or
  "start" for the very first one.

RULES YOU MUST FOLLOW:

* Never invent a character who is not in the roster.
* "present" and "absent" are about where people ARE, not about who is talking.
  A character who says nothing for ten minutes is present if the record puts them
  in the room.
* The Dungeon Master narrates every stretch and is therefore never absent.
* Cover the session: the last stretch's "last" is the final moment given to you.
* When you cannot tell whether somebody is there, leave them out of "absent" -
  say nothing rather than guessing that they are gone.
* If the record never says that anybody left, then ONE stretch covering everything
  is the right answer.

Respond with a single JSON object matching EXACTLY this schema (no markdown, no commentary outside the JSON):

{schema}
'''


def scene_system_message() -> str:
    """The system prompt with the schema rendered in."""
    return SCENE_SYSTEM_PROMPT.format(schema=json.dumps(SCENES_SCHEMA))

@dataclass(frozen=True)
class Scene:
    """One stretch of the session in one place, with who is SHOWN to be there."""

    index: int
    start_ordinal: int
    end_ordinal: int
    first_ref: str
    last_ref: str
    location: str = "unclear"
    present: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    npcs: tuple[str, ...] = ()
    reason: str = ""

    @property
    def moment_count(self) -> int:
        return self.end_ordinal - self.start_ordinal + 1

    def as_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "location": self.location,
            "first_ref": self.first_ref,
            "last_ref": self.last_ref,
            "moments": self.moment_count,
            "present": list(self.present),
            "absent": list(self.absent),
            "npcs": list(self.npcs),
            "reason": self.reason,
        }


@dataclass
class SceneMap:
    """The session's scenes, with the lookup the engine needs.

    Every moment belongs to exactly one scene: a session read as ONE scene
    covering everything is valid, and so is a partial reading, which is stretched
    to the ends of the session rather than leaving moments with no scene at all.
    """

    scenes: tuple[Scene, ...] = ()
    _by_ordinal: dict[int, int] = field(default_factory=dict, repr=False)

    def __bool__(self) -> bool:
        return bool(self.scenes)

    def __len__(self) -> int:
        return len(self.scenes)

    @classmethod
    def build(cls, scenes: Sequence[Scene]) -> SceneMap:
        """Index the scenes by ordinal, overlapping readings resolved in order."""
        ordered = tuple(sorted(scenes, key=lambda scene: (scene.start_ordinal, scene.index)))
        lookup: dict[int, int] = {}
        for position, scene in enumerate(ordered):
            for ordinal in range(scene.start_ordinal, scene.end_ordinal + 1):
                # First scene wins: overlapping ranges are a model error, and the
                # earlier boundary is the one the previous scene already agreed to.
                lookup.setdefault(ordinal, position)
        return cls(scenes=ordered, _by_ordinal=lookup)

    def for_ordinal(self, ordinal: int) -> Scene | None:
        position = self._by_ordinal.get(ordinal)
        if position is None:
            # Outside the read range: the nearest scene is the honest answer for a
            # session whose ends the model never described.
            if not self.scenes:
                return None
            return self.scenes[0] if ordinal < self.scenes[0].start_ordinal else self.scenes[-1]
        return self.scenes[position]

    def for_ref(self, ref: str, ordinals: Mapping[str, int]) -> Scene | None:
        ordinal = ordinals.get(ref)
        return None if ordinal is None else self.for_ordinal(ordinal)

    def as_payload(self) -> list[dict[str, Any]]:
        return [scene.as_payload() for scene in self.scenes]


def render_moment_view(utterances: Iterable[Any]) -> list[str]:
    """One line per moment: reference, time, and what was actually said.

    The full text, not the gists: see the module docstring - a reading built on
    subject-less gists put every character in every scene, because the words that
    place somebody in a room are the words the gist pass takes out.
    """
    lines: list[str] = []
    for utterance in utterances:
        ref = str(getattr(utterance, "ref", ""))
        text = " ".join(str(getattr(utterance, "text", "") or "").split())[:400]
        if not text:
            continue
        stamp = _timestamp(float(getattr(utterance, "start", 0.0)))
        lines.append(f"[{ref} {stamp}] {text}")
    return lines


def build_scene_message(
    lines: Sequence[str],
    *,
    header: str | None = None,
    window: int = 0,
    total: int = 1,
) -> str:
    """One request: the roster, then the moments to segment."""
    parts = [f"Session record, part {window + 1}/{total}."]
    if header:
        parts += ["", header]
    parts += ["", *lines, ""]
    parts.append(
        "Return the JSON object described in your instructions: the scenes of "
        "these moments, in order, covering all of them. Echo the first and the "
        "last reference of each scene exactly."
    )
    return "\n".join(parts)


def parse_scenes(
    raw: Any,
    *,
    ordinals: Mapping[str, int],
    roster_names: Mapping[str, str],
) -> tuple[Scene, ...]:
    """Normalise one response into scenes that cover the session, in order.

    Lenient in the same way the evidence pass is: an unusable scene is dropped, an
    unknown reference or an unknown character is dropped, and the survivors are
    made contiguous by construction - the engine reads a scene per MOMENT, so a
    gap in the reading would be a moment with no place at all.

    'ordinals' maps ref -> ordinal (the moment's position in the session) and
    'roster_names' maps a lowercased name to the roster's own spelling: a name the
    model invented is dropped rather than trusted, because the engine keys
    candidates by member, and a name that matches nobody is worse than no name.
    """
    if not isinstance(raw, dict):
        return ()
    known = sorted(ordinals.items(), key=lambda item: item[1])
    if not known:
        return ()
    last_ordinal = known[-1][1]
    ref_of = {ordinal: ref for ref, ordinal in ordinals.items()}

    seen: list[tuple[int, int, dict[str, Any]]] = []
    for entry in raw.get("scenes") or []:
        if not isinstance(entry, dict):
            continue
        start = ordinals.get(str(entry.get("first") or "").strip())
        end = ordinals.get(str(entry.get("last") or "").strip())
        if start is None:
            continue
        if end is None or end < start:
            end = start
        seen.append((start, end, entry))
    if not seen:
        return ()
    seen.sort(key=lambda item: (item[0], item[1]))

    out: list[Scene] = []
    for position, (start, end, entry) in enumerate(seen):
        if position + 1 < len(seen):
            end = min(end, max(start, seen[position + 1][0] - 1))
        if position == len(seen) - 1:
            end = max(end, last_ordinal)
        out.append(
            Scene(
                index=position + 1,
                start_ordinal=start,
                end_ordinal=end,
                first_ref=ref_of.get(start, ""),
                last_ref=ref_of.get(end, ""),
                location=_clean(entry.get("location")) or "unclear",
                present=_names(entry.get("present"), roster_names),
                absent=_names(entry.get("absent"), roster_names),
                npcs=_strings(entry.get("npcs")),
                reason=_clean(entry.get("reason")) or "",
            )
        )
    if out and out[0].start_ordinal > known[0][1]:
        # A leading stretch the model left out belongs to the first scene it did
        # describe, rather than to no scene at all.
        first = out[0]
        out[0] = Scene(
            index=first.index,
            start_ordinal=known[0][1],
            end_ordinal=first.end_ordinal,
            first_ref=ref_of.get(known[0][1], first.first_ref),
            last_ref=first.last_ref,
            location=first.location,
            present=first.present,
            absent=first.absent,
            npcs=first.npcs,
            reason=first.reason,
        )
    return tuple(out)


def merge_scene_parts(parts: Sequence[Sequence[Scene]]) -> tuple[Scene, ...]:
    """Stitch a session read in several requests back into one scene list.

    The model was never asked to look outside its own part, so a stretch that
    continues across a boundary comes back as two. They are joined when they are
    adjacent, name the same place and the later one does not claim an event
    started it; the result is renumbered so the index a question quotes is the
    index of the session.

    The first stretch of every part after the first has its "absent" list
    DROPPED. That list is the only thing the engine acts on, and a model that
    cannot see the moments before its part has no way to know who was already
    gone: claiming an absence from there would silence a member on evidence
    nobody read. The stretch keeps its place and its cast; it just cannot exclude
    anybody.
    """
    parts = [
        (
            part
            if index == 0 or not part
            else (replace(part[0], absent=()), *part[1:])
        )
        for index, part in enumerate(parts)
    ]
    flat: list[Scene] = [scene for part in parts for scene in part]
    if not flat:
        return ()
    flat.sort(key=lambda scene: (scene.start_ordinal, scene.index))
    merged: list[Scene] = [flat[0]]
    for scene in flat[1:]:
        previous = merged[-1]
        place = previous.location.strip().lower()
        continues = (
            scene.start_ordinal <= previous.end_ordinal + 1
            and place not in {"", "unclear"}
            and scene.location.strip().lower() == place
            and not scene.reason
        )
        if continues:
            merged[-1] = Scene(
                index=previous.index,
                start_ordinal=previous.start_ordinal,
                end_ordinal=max(previous.end_ordinal, scene.end_ordinal),
                first_ref=previous.first_ref,
                last_ref=scene.last_ref,
                location=previous.location,
                present=_union(previous.present, scene.present),
                absent=_union(previous.absent, scene.absent),
                npcs=_union(previous.npcs, scene.npcs),
                reason=previous.reason,
            )
            continue
        merged.append(scene)
    return tuple(
        Scene(
            index=position + 1,
            start_ordinal=scene.start_ordinal,
            end_ordinal=scene.end_ordinal,
            first_ref=scene.first_ref,
            last_ref=scene.last_ref,
            location=scene.location,
            present=scene.present,
            absent=scene.absent,
            npcs=scene.npcs,
            reason=scene.reason,
        )
        for position, scene in enumerate(merged)
    )


def _union(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    out = list(left)
    for value in right:
        if value not in out:
            out.append(value)
    return tuple(out)


def _names(value: Any, roster_names: Mapping[str, str]) -> tuple[str, ...]:
    """Roster spellings of the names given; unknown names dropped."""
    out: list[str] = []
    for entry in _strings(value):
        key = roster_names.get(entry.strip().lower())
        if key and key not in out:
            out.append(key)
    return tuple(out)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

