"""Business logic for content-service (generation jobs + review layers).

The two DM-toolkit modules - app.services.notes (the DM's planning notes) and
app.services.note_plans (the proposed changes built from a selection of them) -
are deliberately NOT flattened in here. They share the review layer's vocabulary
on purpose (note_plans.get_plan / confirm_plan / replace_reviewable / mark_draft
mean for a note plan exactly what plans.* mean for a session's change set), so
re-exporting both into one namespace would be ambiguous, and a reader could not
tell which review layer a call writes. Import them qualified:

    from app.services import note_plans as note_plan_services

which is also what makes the call site say WHICH set is being modified.
"""

from app.services.jobs import (
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    complete_job,
    create_job,
    fail_job,
    latest_job_for_session,
)
from app.services.plans import (
    PlanEditError,
    confirm_plan,
    confirmed_summary,
    get_plan,
    mark_applied,
    mark_applying,
    mark_draft,
    replace_reviewable,
    save_plan,
)
from app.services.summaries import (
    apply_revision,
    confirm_summary,
    latest_summary_for_session,
    save_summary,
    summary_lines,
    summary_to_merged,
)

__all__ = [
    "DONE",
    "FAILED",
    "QUEUED",
    "RUNNING",
    "PlanEditError",
    "apply_revision",
    "complete_job",
    "confirm_plan",
    "confirm_summary",
    "confirmed_summary",
    "create_job",
    "fail_job",
    "get_plan",
    "latest_job_for_session",
    "latest_summary_for_session",
    "mark_applied",
    "mark_applying",
    "mark_draft",
    "replace_reviewable",
    "save_plan",
    "save_summary",
    "summary_lines",
    "summary_to_merged",
]
