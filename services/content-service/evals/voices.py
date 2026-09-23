"""What the recording says about its own voices - and whether it agrees with itself.

WHY THIS EXISTS. The residual defects in the drafts are all one class: the wrong
character on an action. Every attempt to fix it inside the summariser failed, and
the reason is in this module's output rather than in a prompt. A diarized
transcript is only usable by name if its LABELS correspond to people, and on the
two benchmark sessions they do not:

    S1E2  SPEAKER_00  "io sono Rendar"           (00:13:03)
          SPEAKER_00  "Il mio nome e Miles Falco" (00:22:01)
          SPEAKER_01  "Shiran, piacere."          (00:13:22)
          SPEAKER_01  "Io sono Dalia"             (00:13:27)
    S1E1  SPEAKER_04  "Shiran, cin!"              (00:08:25)
          SPEAKER_05  "Shiran: ..."               (00:18:40)

One label covers three people; one person speaks under two labels. There is no
function from label to character, so there is nothing correct for a summariser to
be told - which is why handing it a mapping made drafts worse (app/speakers.py,
app/roster.py), and why the pipeline's answer of "describe it at party level" is
the right one.

The platform HAS the fix, upstream of content-service: refiner-service's speaker
pass re-clusters the voices onto CONSISTENT canonical labels, and speaker-service
matches them to voiceprints and names them. The transcript in these fixtures was
captured with that pass disabled (`refiner.speakers_refined: false`), so the
summariser is being asked to name voices the recording never kept straight.

    python -m evals.voices <fixture>       # what this module reports
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from typing import Any

from evals import fixtures as store

#: A line whose TEXT opens with its own character name - "Shiran: non appena
#: sento la parola indaco...". The player announced who was speaking.
SELF_LABEL = re.compile(r"^\s*([A-ZÀ-Ý][\w'à-ÿ]{2,})\s*:")

#: "io sono Rendar", "Io mi chiamo Rendar", "il mio nome è Miles Falco": the line's
#: own speaker states who they are. Both forms are the recording speaking about
#: itself, which is the only evidence a summariser can trust.
#: The keyword is matched case-insensitively and the NAME is not - scoping the
#: flag with (?i:...) is what keeps "Io sono Letho" a name and "Io sono l'unico"
#: out of the report, which a blanket re.IGNORECASE turns into "l'unico".
SELF_INTRO = re.compile(
    r"\b(?i:io sono|io mi chiamo|mi chiamo|il mio nome (?:e|è))\s+([A-ZÀ-Ý][\w'à-ÿ]{2,})"
)

#: "Dalia, piacere.", "Shiran, cin!": an introduction by name. Weaker than the two
#: above (the name could be the person being ADDRESSED), which is why the report
#: shows it separately rather than mixing it into the map.
SELF_PLEASURE = re.compile(
    r"^\s*([A-ZÀ-Ý][\w'à-ÿ]{2,})\s*,\s*(?:piacere|cin|presente|salve)\b", re.IGNORECASE
)

#: Words that are not names, so the report does not drown in them.
_NOT_A_NAME = frozenset(
    {
        "domanda", "allora", "quindi", "perche", "perché", "ancora", "invece",
        "bene", "dai", "ok", "no", "si", "sì", "ma", "e", "il", "la", "un", "una",
        "pero", "però", "comunque", "aspetta", "senti", "guarda", "certo",
    }
)


@dataclass
class VoiceEvidence:
    """One thing the recording says about one label."""

    label: str
    name: str
    kind: str  # self_label | self_intro | pleasure
    at: float
    text: str


@dataclass
class VoiceReport:
    fixture: str
    evidence: list[VoiceEvidence] = field(default_factory=list)
    #: label -> names claimed by that label
    by_label: dict[str, dict[str, list[VoiceEvidence]]] = field(default_factory=dict)
    #: name -> labels claiming it
    by_name: dict[str, set[str]] = field(default_factory=dict)

    @property
    def conflicting_labels(self) -> dict[str, set[str]]:
        """Labels that speak as more than one person: one voice, several characters."""
        return {
            label: {name for name, items in claims.items() if name}
            for label, claims in self.by_label.items()
            if len({name for name, items in claims.items() if name}) > 1
        }

    @property
    def split_names(self) -> dict[str, set[str]]:
        """Names spoken under more than one label: one character, several voices."""
        return {name: labels for name, labels in self.by_name.items() if len(labels) > 1}

    @property
    def usable(self) -> bool:
        """Whether the labels are consistent enough to name voices from at all."""
        return not self.conflicting_labels and not self.split_names


def _stamp(seconds: float) -> str:
    total = max(0, int(float(seconds or 0)))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def collect(transcript: dict[str, Any]) -> list[VoiceEvidence]:
    """Every self-identification the recording states, in order."""
    found: list[VoiceEvidence] = []
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "")
        label = str(segment.get("speaker") or "?")
        at = float(segment.get("start") or 0.0)
        for pattern, kind in (
            (SELF_LABEL, "self_label"),
            (SELF_INTRO, "self_intro"),
            (SELF_PLEASURE, "pleasure"),
        ):
            match = pattern.search(text) if kind == "self_intro" else pattern.match(text)
            if not match:
                continue
            name = match.group(1)
            if name.lower() in _NOT_A_NAME:
                continue
            found.append(VoiceEvidence(label, name, kind, at, text.strip()[:120]))
            break
    return found


def analyse(fixture_name: str) -> VoiceReport:
    fixture = store.load(fixture_name)
    report = VoiceReport(fixture=fixture_name, evidence=collect(fixture.transcript))
    for item in report.evidence:
        report.by_label.setdefault(item.label, {}).setdefault(item.name, []).append(item)
        report.by_name.setdefault(item.name, set()).add(item.label)
    return report


def render(report: VoiceReport) -> str:
    lines = [f"# voices: {report.fixture}", ""]
    if not report.evidence:
        lines += ["The recording never states who any voice is.", ""]
        return "\n".join(lines)
    lines += ["What the recording says about its own voices:", ""]
    for item in report.evidence:
        lines.append(f"- [{_stamp(item.at)}] {item.label} --{item.kind}--> {item.name}")
    lines.append("")
    if report.usable:
        lines += [
            "The labels agree with each other: one voice per person, one person per",
            "voice. A summariser can be told which label is which.",
            "",
        ]
        return "\n".join(lines)
    lines += [
        "**The labels do NOT agree, so no voice can be named from them.**",
        "",
    ]
    for label, names in sorted(report.conflicting_labels.items()):
        lines.append(f"- {label} speaks as: {', '.join(sorted(names))}")
    for name, labels in sorted(report.split_names.items()):
        lines.append(f"- {name} speaks under: {', '.join(sorted(labels))}")
    lines += [
        "",
        (
            "A summariser handed a mapping built on this would apply one character's "
            "name to another's actions - measured, and it made drafts worse "
            "(app/speakers.py, app/roster.py). The pass that fixes it is upstream: "
            "refiner-service's speaker mode re-clusters the voices onto consistent "
            "labels, and speaker-service names them. This transcript was captured "
            "with that pass off."
        ),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.voices",
        description="What a fixture's recording says about its own voices.",
    )
    parser.add_argument("fixtures", nargs="*", help="fixture names (default: all)")
    args = parser.parse_args(argv)
    names = args.fixtures or store.available()
    for name in names:
        try:
            print(render(analyse(name)))
        except store.FixtureError as exc:
            print(f"{name}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
