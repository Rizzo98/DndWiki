"""Every run of one fixture, side by side: the measurement across iterations.

A single run is a sample (llm_temperature is 0.2), so the question "did that
change help?" is only answerable across runs. The harness already writes every
artefact of every run under out/<fixture>/<stamp>/; this reads them back and puts
the numbers in one table, oldest first.

    python -m evals.history                       # every fixture with runs
    python -m evals.history s1e2_bugie_inutili    # one fixture

The columns are the ones a change should move: how long the draft is against the
benchmark's length, how many of the benchmark's names it uses, how much of its
content the coverage judge found missing, and how many statements the ground
truth contradicts. Shape alone never decides - a shorter draft that invents less
and covers more is a better summary.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT_ROOT = Path(__file__).resolve().parent / "out"


def runs(fixture: str, out_root: Path | None = None) -> list[tuple[str, dict]]:
    """(stamp, comparison json) for every run of a fixture, oldest first."""
    root = (out_root or OUT_ROOT) / fixture
    if not root.is_dir():
        return []
    found: list[tuple[str, dict]] = []
    for directory in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        path = directory / "comparison.json"
        if path.is_file():
            found.append((directory.name, json.loads(path.read_text(encoding="utf-8"))))
    return found


def render(fixture: str, out_root: Path | None = None) -> str:
    rows = runs(fixture, out_root)
    if not rows:
        root = (out_root or OUT_ROOT) / fixture
        return f"{fixture}: no runs with a benchmark comparison under {root}"
    lines = [
        f"# {fixture}: {len(rows)} run(s)",
        "",
        (
            "| run | words (bench) | names | missing | contradicted | unsupported "
            "| quality (1-5) |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    scores: list[int] = []
    for stamp, data in rows:
        words = data.get("words") or {}
        names = data.get("names") or {}
        quality = data.get("quality") or {}
        overall = quality.get("overall")
        if isinstance(overall, int):
            scores.append(overall)
        axes = " ".join(
            f"{axis[:3]}{quality[axis]}"
            for axis in ("coverage", "accuracy", "narrative", "density")
            if axis in quality
        )
        lines.append(
            f"| {stamp} | {words.get('draft')} ({words.get('benchmark')}) "
            f"| {names.get('coverage', 0):.0%} "
            f"| {len(data.get('missing_content') or [])} "
            f"| {len(data.get('contradicted') or [])} "
            f"| {len(data.get('unsupported') or [])} "
            f"| {(str(overall) + ' (' + axes + ')') if overall else '-'} |"
        )
    lines.append("")
    if scores:
        lines += [
            (
                f"**quality against the benchmark: mean {sum(scores) / len(scores):.2f} "
                f"over {len(scores)} judged run(s)** (5 = as good as the human summary, "
                "3 = usable but clearly worse)"
            ),
            "",
        ]
    # What the runs agree on: a failure class present in EVERY run is a property
    # of the pipeline, one present in a single run is a sample.
    counts: dict[str, int] = {}
    for _, data in rows:
        for item in data.get("contradicted") or []:
            counts[item] = counts.get(item, 0) + 1
    if counts:
        lines += ["## statements the ground truth contradicts, by how many runs", ""]
        for item, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {count}/{len(rows)}: {item}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.history",
        description="Every run of a fixture side by side, oldest first.",
    )
    parser.add_argument("fixtures", nargs="*", help="fixture names (default: all with runs)")
    args = parser.parse_args(argv)
    names = args.fixtures or (
        sorted(p.name for p in OUT_ROOT.iterdir() if p.is_dir()) if OUT_ROOT.is_dir() else []
    )
    for name in names:
        print(render(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
