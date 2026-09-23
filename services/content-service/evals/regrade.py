"""Re-measure every STORED run with the comparison module as it is now.

The instrument changes too, and a measurement made by an older instrument is not
comparable with a new one. When the coverage judge was rewritten to decide one
benchmark item at a time, every comparison.json on disk had been written by the
version that reported facts as missing while they were present in the draft;
comparing a fresh run against those numbers would have measured the judge rather
than the pipeline.

    python -m evals.regrade                  # every fixture with stored runs
    python -m evals.regrade s1e2_bugie_inutili

It re-grades the narratives already on disk and rewrites their comparison.json.
It does NOT re-run the pipeline: no extraction, no compose, one judge call per
stored run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import get_settings
from app.extraction import LLMClient
from evals import compare
from evals import fixtures as store

OUT_ROOT = Path(__file__).resolve().parent / "out"


async def regrade(name: str, llm: LLMClient) -> int:
    """Re-measure every stored run of one fixture. Returns how many were updated."""
    try:
        fixture = store.load(name)
    except store.FixtureError as exc:
        print(f"{name}: {exc}")
        return 0
    root = OUT_ROOT / name
    if not root.is_dir():
        return 0
    updated = 0
    for directory in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        narrative = directory / "narrative.run.md"
        if not narrative.is_file():
            continue
        result = await compare.compare(
            llm,
            name,
            narrative.read_text(encoding="utf-8"),
            fixture.benchmark,
            fixture.truth,
            judge=True,
        )
        (directory / "comparison.json").write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(
            f"{name}/{directory.name}: words={result.draft_words} "
            f"coverage={result.content_coverage:.0%} "
            f"missing={len(result.missing_content)} "
            f"contradicted={len(result.contradicted)}"
        )
        updated += 1
    return updated


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.regrade",
        description="Re-measure stored runs with the current comparison module.",
    )
    parser.add_argument("fixtures", nargs="*", help="fixture names (default: all with runs)")
    args = parser.parse_args(argv)
    names = args.fixtures or (
        sorted(p.name for p in OUT_ROOT.iterdir() if p.is_dir()) if OUT_ROOT.is_dir() else []
    )
    llm = LLMClient(get_settings())
    total = 0
    for name in names:
        total += await regrade(name, llm)
    print(f"re-graded {total} stored run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
