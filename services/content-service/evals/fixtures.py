"""Fixture discovery and loading.

**A FIXTURE IS A DIRECTORY, AND THE DIRECTORY IS THE WHOLE FIXTURE.** Everything
it needs lives inside it, so deleting the directory IS the removal procedure.
Nothing outside this package holds a list of fixture names - available() scans
the filesystem instead of enumerating a registry - which is what makes that true:
there is no second place to edit, no import to drop and no test to fix up.

    evals/fixtures/<name>/
        fixture.yaml       provenance: where the recording came from, written by capture.py
        expected.yaml      the checks; hand-written, evaluated by checks.py
        attributed.json    the frozen attributed transcript - the pipeline's INPUT
        baseline.json      the draft that actually shipped, for the offline self-test

Why this is not under tests/: a fixture is a recording of a real session
(names, the table's improvisation, hundreds of kilobytes of transcript). It is
kept only as long as it earns its keep, and the test suite must stay green and
complete once it is gone - see tests/test_evals_fixtures.py, which exercises
this module against a synthetic fixture built in tmp_path.

This module is PURE: no app imports, no network, no LLM. It is importable by the
unit tests without the service's runtime dependencies.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Where fixtures live. Overridable so tests can point at a tmp_path tree.
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"

#: The files a fixture directory carries.
ATTRIBUTED_NAME = "attributed.json"
TRANSCRIPT_NAME = "transcript.json"
BASELINE_NAME = "baseline.json"
META_NAME = "fixture.yaml"
EXPECTED_NAME = "expected.yaml"
README_NAME = "README.md"
#: The target the draft is measured against, when one is known: the session
#: summary a human wrote for the same recording. It is a BENCHMARK, not a
#: baseline - baseline.json is what the pipeline actually produced, this is what
#: it should have produced, and the gap between them is the thing being worked on.
BENCHMARK_NAME = "benchmark.txt"
#: A denser account of the same session (also written by a human), used as the
#: ground truth when judging whether a statement in the draft is true at all.
TRUTH_NAME = "truth.txt"

#: The two input shapes a fixture may carry, in the order the pipeline prefers
#: them: the ATTRIBUTED transcript (the redesign's input, per-utterance status and
#: a [u_XXXXX] ref) or the DIARIZED transcript (the worker's input when the
#: attribution engine did not run - speaker labels, no ids, no status).
INPUT_ATTRIBUTED = "attributed"
INPUT_TRANSCRIPT = "transcript"


class FixtureError(RuntimeError):
    """A fixture that exists but cannot be used (missing or malformed parts)."""


@dataclass
class Fixture:
    """One loaded fixture: its provenance, its input, its expectations."""

    name: str
    path: Path
    meta: dict[str, Any]
    expected: dict[str, Any]
    artifact: dict[str, Any]
    baseline: dict[str, Any] = field(default_factory=dict)
    transcript: dict[str, Any] = field(default_factory=dict)
    #: The human-written summary of the same recording: what the draft should
    #: read like. Empty when the fixture only asserts defects.
    benchmark: str = ""
    #: A denser human account of the session, for judging whether what the draft
    #: says is TRUE (the benchmark shows the level; this shows the facts).
    truth: str = ""

    # -- convenience over the input ---------------------------------------

    @property
    def input_kind(self) -> str:
        """Which of the pipeline's two inputs this fixture carries.

        Both are real inputs of the deployed worker: the attributed artifact when
        the attribution engine ran (ATTRIBUTION_ENABLED), the diarized transcript
        and a speaker map when it did not.
        """
        return INPUT_ATTRIBUTED if self.artifact else INPUT_TRANSCRIPT

    @property
    def session_id(self) -> str:
        return str(
            self.meta.get("session_id")
            or self.artifact.get("session_id")
            or self.transcript.get("session_id")
            or ""
        )

    @property
    def speakers(self) -> dict[str, str]:
        """Speaker label -> character name, when the fixture was given one.

        The deployed worker gets this from the speakers.identified event (voice
        print matching + the DM naming strangers). A fixture that carries no map
        runs the way a session runs when nobody has been identified yet: the raw
        diarization label is what the view shows.
        """
        raw = self.meta.get("speakers") or {}
        return {str(k): str(v) for k, v in raw.items() if v}

    @property
    def roster_members(self) -> list[dict[str, Any]]:
        """The campaign roster in campaign-service's own shape.

        A fixture declares it as a mapping in fixture.yaml:

            roster:
              dm: [Matt]
              players:
                Gianandrea: Galgith

        and it comes back as the member rows GET /internal/campaigns/{id}/members
        returns, so the run builds its CampaignRoster through the SAME function the
        worker uses (app/roster.roster_from_members) instead of a fixture-shaped
        shortcut. A fixture with no roster runs the way a session runs when the
        campaign cannot be reached: without it.
        """
        raw = self.meta.get("roster") or {}
        members: list[dict[str, Any]] = [
            {"role": "dm", "player_name": str(name)}
            for name in (raw.get("dm") or [])
            if str(name).strip()
        ]
        players = raw.get("players") or {}
        appearances = raw.get("appearance") or {}
        members += [
            {
                "role": "player",
                "player_name": str(player),
                "character_name": str(character),
                "character_description": str(appearances.get(character) or ""),
            }
            for player, character in players.items()
            if str(player).strip() and str(character).strip()
        ]
        return members

    def utterances(self) -> list[dict[str, Any]]:
        """Utterance-shaped rows for EITHER input, so evidence refs resolve.

        An attributed artifact carries them already. A diarized transcript has
        segments instead, which have no id: one is synthesized from the line
        position ('s_00001'), stable for as long as the fixture file is, so a
        check can name the line it is about whichever input the fixture has.
        """
        if self.artifact:
            return list(self.artifact.get("utterances") or [])
        rows: list[dict[str, Any]] = []
        for index, segment in enumerate(self.transcript.get("segments") or [], 1):
            if not isinstance(segment, dict):
                continue
            rows.append(
                {
                    "id": f"s_{index:05d}",
                    "start": segment.get("start"),
                    "text": segment.get("text"),
                    "speaker": {
                        "label": segment.get("speaker"),
                        "character_name": None,
                    },
                    "status": "unresolved",
                }
            )
        return rows

    def evidence(self, refs: list[str]) -> list[str]:
        """The transcript lines behind a check's 'evidence' list.

        A judge check is only as good as the ground truth it is shown: these are
        the lines the criterion is about, rendered the way the pipeline's own
        view renders them ([ref time] speaker: text), so the judge reads the same
        certainty markers the extractor read.
        """
        wanted = {str(ref) for ref in refs}
        out: list[str] = []
        for utterance in self.utterances():
            if str(utterance.get("id")) not in wanted:
                continue
            speaker = utterance.get("speaker") or {}
            name = (speaker.get("character_name") or speaker.get("label") or "?").strip()
            out.append(
                f'[{utterance.get("id")} {_stamp(utterance.get("start"))}] {name}: '
                f'{utterance.get("text")}'
            )
        return out

    def checks(self) -> list[dict[str, Any]]:
        return list(self.expected.get("checks") or [])

    def known_baseline_failures(self) -> list[str]:
        section = self.expected.get("baseline") or {}
        return [str(item) for item in (section.get("known_failures") or [])]


def _stamp(seconds: Any) -> str:
    total = max(0, int(float(seconds or 0)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def available(root: Path | None = None) -> list[str]:
    """Every fixture installed right now, sorted.

    A directory counts when it carries a fixture.yaml - the marker capture.py
    writes - so a half-deleted or scratch directory is ignored rather than
    crashing the run. An EMPTY LIST IS A LEGITIMATE STATE, not an error: it is
    the state the repository returns to once the fixtures are removed.
    """
    base = Path(root) if root is not None else FIXTURE_ROOT
    if not base.is_dir():
        return []
    return sorted(
        entry.name
        for entry in base.iterdir()
        if entry.is_dir() and (entry / META_NAME).is_file()
    )


def load(name: str, root: Path | None = None) -> Fixture:
    """Load one fixture, or raise FixtureError explaining what is missing."""
    base = Path(root) if root is not None else FIXTURE_ROOT
    path = base / name
    if not path.is_dir():
        known = ", ".join(available(base)) or "none installed"
        raise FixtureError(f"fixture {name!r} is not installed (installed: {known})")
    meta = _read_yaml(path / META_NAME, name)
    expected = _read_yaml(path / EXPECTED_NAME, name)
    # One of the two inputs is required; attributed.json wins when both are here,
    # because that is the shape the worker prefers too.
    artifact = _read_json(path / ATTRIBUTED_NAME, name) if (path / ATTRIBUTED_NAME).is_file() else {}
    transcript = (
        _read_json(path / TRANSCRIPT_NAME, name)
        if (path / TRANSCRIPT_NAME).is_file()
        else {}
    )
    if not artifact and not transcript:
        raise FixtureError(
            f"fixture {name!r}: no {ATTRIBUTED_NAME} and no {TRANSCRIPT_NAME} - "
            "a fixture needs the recording it is about"
        )
    baseline_path = path / BASELINE_NAME
    baseline = _read_json(baseline_path, name) if baseline_path.is_file() else {}
    return Fixture(
        name=name,
        path=path,
        meta=meta,
        expected=expected,
        artifact=artifact,
        baseline=baseline,
        transcript=transcript,
        benchmark=_read_text(path / BENCHMARK_NAME),
        truth=_read_text(path / TRUTH_NAME),
    )


def _read_text(path: Path) -> str:
    """Optional free text (a benchmark, a ground-truth account): "" when absent."""
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def drop(name: str, root: Path | None = None) -> Path:
    """Delete a fixture directory. The one supported removal path.

    Destructive by design and guarded at the CLI (--drop NAME --yes), because
    "easy to remove" is the entire point of keeping fixtures in one directory:
    the operation must be obvious, complete and impossible to half-do.
    """
    base = Path(root) if root is not None else FIXTURE_ROOT
    path = base / name
    if not path.is_dir():
        raise FixtureError(f"fixture {name!r} is not installed")
    if not (path / META_NAME).is_file():
        raise FixtureError(
            f"{path} has no {META_NAME}: refusing to delete a directory that is "
            "not a fixture"
        )
    shutil.rmtree(path)
    return path


def size_mb(name: str, root: Path | None = None) -> float:
    """On-disk size of a fixture, for --list (these are not small files)."""
    base = Path(root) if root is not None else FIXTURE_ROOT
    path = base / name
    if not path.is_dir():
        return 0.0
    return round(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6, 2)


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------


def _read_yaml(path: Path, name: str) -> dict[str, Any]:
    if not path.is_file():
        raise FixtureError(f"fixture {name!r}: {path.name} is missing")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FixtureError(f"fixture {name!r}: {path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise FixtureError(f"fixture {name!r}: {path.name} must be a mapping")
    return data


def _read_json(path: Path, name: str) -> dict[str, Any]:
    if not path.is_file():
        raise FixtureError(f"fixture {name!r}: {path.name} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FixtureError(f"fixture {name!r}: {path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FixtureError(f"fixture {name!r}: {path.name} must be a JSON object")
    return data
