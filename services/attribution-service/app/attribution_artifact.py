"""The attributed transcript artifact (docs/attribution-model.md S14.1).

This is what content-service reads INSTEAD of a label map. It carries a status
per utterance, and the status is what the wiki gate keys on: nothing reaches a
character page unless its source_refs resolve to a confident status.

The artifact deliberately carries no raw diarization label and no player's real
name in a speaker field. The renderer decides how to show an uncertain or
unresolved line; the artifact only reports what the engine believes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.statuses import CONFIDENT_STATUSES

#: Rendered inline before an utterance in the content view, per status (S14.2).
VIEW_PREFIX = {
    "user_confirmed": "",
    "auto_high": "",
    "propagated": "",
    "auto_low": "?",
    "unresolved": None,  # no name at all: "(unattributed)"
}


def build_artifact(plan: Any, *, revision: int = 1) -> dict[str, Any]:
    """The JSON content-service consumes (and the audit trail the SDK shows)."""
    roster = {entry["member_id"]: entry for entry in plan.roster}
    from app.inference import best_candidate
    utterances: list[dict[str, Any]] = []
    for utterance in plan.utterances:
        verdict = plan.verdicts.get(utterance.ref)
        posterior = plan.inference.posteriors.get(utterance.ref, {})
        candidate, confidence = best_candidate(posterior)
        speaker: dict[str, Any] | None = None
        if candidate and candidate != "unknown":
            member_id = (
                candidate.split(":", 1)[1] if candidate.startswith("member:") else None
            )
            entry = roster.get(member_id or "", {})
            speaker = {
                "member_id": member_id,
                "character_name": entry.get("character_name"),
                "player_name": entry.get("player_name"),
                "role": entry.get("role") or "player",
                "mode": _mode_of(plan, utterance.ref),
                "label": entry.get("label"),
            }
        utterances.append(
            {
                "id": utterance.ref,
                "start": round(utterance.start, 3),
                "end": round(utterance.end, 3),
                "text": utterance.text,
                "speaker": speaker,
                "status": verdict.status if verdict else "unresolved",
                "confidence": (
                    round(confidence, 4) if confidence is not None else None
                ),
                "kind": _item_of(plan, utterance.ref).kind,
                "stakes": _item_of(plan, utterance.ref).stakes,
                "decided_by": verdict.decided_by if verdict else None,
                "voice": plan.belief.effective_voice_of(utterance.ref),
            }
        )
    return {
        "session_id": plan.session_id,
        "campaign_id": plan.campaign_id,
        "language": plan.language,
        "attribution_revision": revision,
        "coverage": round(plan.coverage, 4),
        "unresolved_stakes": round(plan.unresolved, 4),
        "engine": {
            "version": getattr(plan, "engine_version", "attr-1"),
            "converged": plan.converged,
            "method": plan.method,
            "prompt_version": getattr(plan.evidence, "prompt_version", None),
        },
        "roster": plan.roster,
        "utterances": utterances,
    }


def _item_of(plan: Any, ref: str) -> Any:
    """The evidence item for one utterance (an empty one when the pass was thin)."""
    from app.evidence import EvidenceItem

    evidence = getattr(plan, "evidence", None)
    if evidence is None:
        return EvidenceItem(ref=ref)
    return evidence.item(ref)


def _mode_of(plan: Any, ref: str) -> str | None:
    return _item_of(plan, ref).voice_mode


def confident_refs(artifact: Mapping[str, Any]) -> set[str]:
    """The utterances the wiki gate will accept as a source for a fact."""
    return {
        str(utterance["id"])
        for utterance in artifact.get("utterances") or []
        if utterance.get("status") in CONFIDENT_STATUSES
    }


def status_of(artifact: Mapping[str, Any], ref: str) -> str:
    for utterance in artifact.get("utterances") or []:
        if str(utterance.get("id")) == ref:
            return str(utterance.get("status") or "unresolved")
    return "unresolved"


def render_line(artifact: Mapping[str, Any], ref: str) -> str:
    """One line of the content-pass view, per S14.2.

    Confident:      [u_00412 00:41:15] Aramil: I cast fireball on the three goblins
    auto_low:       [u_00413 00:41:19] Aramil?: wait, do I still get my attack?
    unresolved:     [u_00414 00:41:24] (unattributed): ok so I move up to the door
    """
    for utterance in artifact.get("utterances") or []:
        if str(utterance.get("id")) != ref:
            continue
        status = str(utterance.get("status") or "unresolved")
        speaker = utterance.get("speaker") or {}
        name = speaker.get("character_name") or speaker.get("label") or ""
        if status == "unresolved" or not name:
            head = "(unattributed)"
        elif status == "auto_low":
            head = f"{name}?"
        else:
            head = name
        stamp = _timestamp(float(utterance.get("start") or 0.0))
        return f"[{ref} {stamp}] {head}: {utterance.get('text', '')}"
    return ""


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"
