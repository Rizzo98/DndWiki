"""Capture a session as a fixture: the permanent half of this package.

The FIXTURES are temporary - they are recordings of a table's evening and they get
deleted when they stop earning their keep. THIS is what stays, and it is what makes
that safe: the fixture is generated from the same sources the pipeline reads, so
it can be regenerated, replaced or removed without losing the ability to make one.

What it reads (nothing else, and nothing is inferred):

    MinIO    transcripts/<session_id>/attributed.json   the pipeline's actual input
    Postgres dnd_content.session_summaries              the draft that actually shipped

What it writes into the fixture directory:

    fixture.yaml      provenance, so a fixture can be dated and invalidated
    attributed.json   the frozen input
    baseline.json     the shipped draft (summary text, blocks, entities, timeline)
    expected.yaml     a STARTER set of checks - only if the file is absent
    README.md         what this is and how to delete it - only if the file is absent

expected.yaml and README.md are never overwritten: the checks are the part a human
writes, and capture must not be able to destroy them.

The baseline is the reason a fixture is worth its kilobytes. It is the output the
pipeline really produced on that session, so the checks can be run against it
OFFLINE, with no model and no API key, and prove they detect what they claim to.
A check that cannot fail on a known-bad draft is decoration.

Run it from the service directory (defaults point at the local stack):

    python -m evals.capture --session <uuid> --name my-session
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from dnd_common.db import session_factory
from sqlalchemy import select

from app.core.config import ServiceSettings, get_settings
from app.models import SessionSummary
from app.storage import ObjectStorage
from evals import fixtures as store

#: Written only when the fixture has no expected.yaml yet. Deliberately thin: the
#: generic shape checks are free and always apply, while the checks that actually
#: catch a wrong sentence are about ONE session and have to be written by someone
#: who read it. A stub that pretended otherwise would be worse than no stub.
EXPECTED_TEMPLATE = """# The checks this fixture asserts. Evaluated by evals/checks.py.
#
# Two kinds, on purpose:
#   judge  - asks a question the way a reader would. Needs a model, and it is the
#            only kind that can catch a real fact stated about the wrong person.
#   others - deterministic proxies over shape. Cheap, reproducible, cannot be
#            argued with, and blind to meaning.
#
# `baseline.known_failures` lists the checks the STORED draft fails. That list is
# the eval's own self-test: if one of them starts passing, the check has stopped
# detecting the defect it was written for (tests/test_evals_fixtures.py enforces it).

baseline:
  known_failures: []

checks:
  - id: beat-budget
    kind: min_beats
    applies_to: run
    blocking: false
    value: 25
    note: "1-3 beats per chunk over 5 chunks is ~15 beats for a 52 minute session"

  - id: no-duplicated-moments
    kind: max_duplicate_similarity
    applies_to: run
    blocking: false
    value: 0.35
    min_words: 5
    note: "a proxy: the 10% chunk overlap writes the same moment twice, in different words"

  - id: labels-come-from-the-extraction
    kind: labels_from_places
    applies_to: text
    blocking: true
    why: >
      block labels must be place names the extraction produced, in the table's own
      language - not the attribution engine's English reading of where the scene is
"""

README_TEMPLATE = """# Fixture: {name}

A frozen recording of one real session, kept as a regression test for the
summariser. See ../README.md for what fixtures are and how the checks work.

| | |
|---|---|
| session | `{session_id}` |
| campaign | `{campaign_id}` |
| language | {language} |
| utterances | {utterances} |
| captured | {captured_at} |
| input | `s3://{bucket}/{key}` |
| draft | revision {revision}, prompt {prompt_version}, model {model}, confidence {confidence} |

## The files

- `fixture.yaml` - the table above, machine readable.
- `attributed.json` - the attributed transcript the pipeline was given.
- `baseline.json` - the draft that actually shipped from it.
- `expected.yaml` - the checks; `baseline.known_failures` is the self-test list.

## Removing this fixture

    python -m evals --drop {name} --yes

or simply delete this directory - nothing outside it refers to it by name, and
the test suite skips the checks that need it. Regenerate it at any time with:

    python -m evals.capture --session {session_id} --name {name}
"""


class CaptureError(RuntimeError):
    """The session cannot be captured (missing input or missing draft)."""


async def capture(
    session_id: str,
    name: str,
    *,
    root: Path | None = None,
    force: bool = False,
) -> Path:
    """Freeze one session into a fixture directory and return its path."""
    settings = get_settings()
    base = Path(root) if root is not None else store.FIXTURE_ROOT
    path = base / name

    if path.exists() and any(path.iterdir()) and not force:
        raise CaptureError(
            f"{path} already exists and is not empty; pass --force to overwrite "
            "its data files (expected.yaml and README.md are never touched)"
        )

    artifact, source = await _read_artifact(settings, session_id)
    baseline = await _read_baseline(settings, session_id)

    path.mkdir(parents=True, exist_ok=True)
    _write_json(path / store.ATTRIBUTED_NAME, artifact)
    _write_json(path / store.BASELINE_NAME, baseline)

    meta = {
        "name": name,
        "session_id": session_id,
        "campaign_id": str(artifact.get("campaign_id") or ""),
        "language": artifact.get("language"),
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "utterances": len(artifact.get("utterances") or []),
        "baseline": {
            key: baseline.get(key)
            for key in (
                "summary_id",
                "revision",
                "prompt_version",
                "llm_provider",
                "llm_model",
                "confidence",
                "language",
                "review_status",
            )
        },
    }
    _write_yaml(path / store.META_NAME, meta)

    # The two files a human owns. Written once, never overwritten.
    _write_if_absent(path / store.EXPECTED_NAME, EXPECTED_TEMPLATE)
    _write_if_absent(
        path / store.README_NAME,
        README_TEMPLATE.format(
            name=name,
            session_id=session_id,
            campaign_id=meta["campaign_id"],
            language=meta["language"] or "?",
            utterances=meta["utterances"],
            captured_at=meta["captured_at"],
            bucket=source["bucket"],
            key=source["key"],
            revision=baseline.get("revision", "?"),
            prompt_version=baseline.get("prompt_version") or "?",
            model=baseline.get("llm_model") or "?",
            confidence=baseline.get("confidence", "?"),
        ),
    )
    return path


async def _read_artifact(
    settings: ServiceSettings, session_id: str
) -> tuple[dict[str, Any], dict[str, str]]:
    bucket = settings.minio_transcripts_bucket
    key = f"transcripts/{session_id}/attributed.json"
    try:
        artifact = await ObjectStorage(settings).read_json(bucket, key)
    except Exception as exc:
        raise CaptureError(
            f"could not read s3://{bucket}/{key}: {type(exc).__name__}: {exc}. "
            "The fixture needs the ATTRIBUTED transcript; a session transcribed "
            "before the attribution engine ran has no such object."
        ) from exc
    if not artifact.get("utterances"):
        raise CaptureError(f"s3://{bucket}/{key} carries no utterances")
    return artifact, {"bucket": bucket, "key": key}


async def _read_baseline(settings: ServiceSettings, session_id: str) -> dict[str, Any]:
    """The stored draft: everything the checks may need to grade it offline."""
    async with session_factory(settings)() as db:
        row = (
            await db.execute(
                select(SessionSummary).where(SessionSummary.session_id == UUID(session_id))
            )
        ).scalar_one_or_none()
    if row is None:
        raise CaptureError(
            f"session {session_id} has no row in session_summaries: there is no "
            "draft to compare a run against, and without one the checks cannot be "
            "shown to detect anything"
        )
    return {
        "summary_id": str(row.id),
        "summary": row.summary or "",
        "summary_blocks": row.summary_blocks or [],
        "characters": row.characters or [],
        "locations": row.locations or [],
        "events": row.events or [],
        "timeline_entries": row.timeline_entries or [],
        "party_characters": row.party_characters or [],
        "language": row.language,
        "revision": row.revision,
        "review_status": row.review_status,
        "confidence": float(row.confidence) if row.confidence is not None else None,
        "llm_provider": row.llm_provider,
        "llm_model": row.llm_model,
        "prompt_version": row.prompt_version,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def _write_yaml(path: Path, payload: Any) -> None:
    import yaml

    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _write_if_absent(path: Path, text: str) -> None:
    if not path.exists():
        path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.capture",
        description="Freeze a session into a fixture under evals/fixtures/.",
    )
    parser.add_argument("--session", required=True, help="session uuid to capture")
    parser.add_argument("--name", required=True, help="fixture directory name")
    parser.add_argument("--force", action="store_true", help="overwrite the data files")
    args = parser.parse_args(argv)

    try:
        path = asyncio.run(capture(args.session, args.name, force=args.force))
    except CaptureError as exc:
        print(f"capture failed: {exc}")
        return 2
    print(f"captured {args.name} -> {path}")
    for entry in sorted(path.iterdir()):
        print(f"  {entry.name}  ({entry.stat().st_size / 1000:.0f} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
