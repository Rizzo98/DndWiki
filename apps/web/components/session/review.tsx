// The session review: the DM answers a handful of questions about MOMENTS.
//
// This replaces "confirm every SPEAKER_XX label" (docs/attribution-ux.md S3-S4).
// Three properties matter more than the styling:
//
// 1. Every question is answerable from memory of the session: it quotes a line
//    and offers one-click playback of the audio span (S8.2). A question the DM
//    cannot answer from memory is a bug in the generator, not a hard question.
// 2. "I don't know" is ALWAYS an option and never penalised (S8.3). An answer of
//    "I don't know" changes nothing except the record - the engine must be able
//    to accept it without inventing a label.
// 3. The reward for answering is visible: "this also resolved 12 other moments".
//    That message is not decoration; it is the measured propagation (S10.6).

"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, Badge, Button, Card, EmptyState, Spinner, fmtDuration } from "@/components/ui";
import { AttributionBuckets, CoverageBar } from "@/components/session/attribution";
import {
  ApiError,
  sessionsApi,
  type ReviewQuestion,
  type ReviewStatus,
  type ReviewStop,
} from "@/lib/api";
import { errMessage } from "@/lib/use-async";

type Phase = "loading" | "ready" | "asking" | "done" | "error" | "disabled";

/**
 * Why the engine stopped asking, in the DM's words.
 *
 * "Nothing left worth asking" is TRUE for a belief that converged and FALSE for
 * a review that ran out of its question budget - and the second case is the one
 * where the DM most needs to know what is left, because the panel is about to
 * tell them the session is finished while most of it is still unattributed
 * (docs/attribution-model.md S11.2).
 */
const STOP_EXPLANATION: Record<string, string> = {
  default:
    "Nothing left worth asking. Everything we could not settle is described at party level and never attributed to a character.",
  budget:
    "We have asked every question one review asks. What is still unattributed is described at party level and never attributed to a character - enrolling the players' voices is what moves it further.",
  repeated_dont_know:
    "You said you did not know twice in a row, so we stopped asking. What is left is described at party level and never attributed to a character.",
  target_reached:
    "We have settled everything that matters for the wiki. The rest is described at party level and never attributed to a character.",
  gain_floor:
    "Nothing left worth asking: no remaining question would change what the wiki can use. The rest is described at party level.",
  no_candidates:
    "No question we could ask would help. The rest is described at party level and never attributed to a character.",
};

export function SessionReviewCard({
  token,
  sessionId,
  sessionStatus,
  onSeek,
  onFinished,
}: {
  token: string | null;
  sessionId: string;
  /**
   * The session's pipeline status. The review only EXISTS once the engine has
   * run, and the DM is usually sitting on this page while it does - so the card
   * re-asks whenever the status moves, and appears on its own instead of
   * waiting for a manual refresh. It is a dependency, not a condition: the
   * server, not this component, decides whether there is a review to show.
   */
  sessionStatus?: string;
  /** Seek the page's audio player to a span, so the DM can just listen. */
  onSeek?: (seconds: number) => void;
  /** Called when the review ends, so the page can reload the summary. */
  onFinished?: () => void;
}) {
  const [status, setStatus] = useState<ReviewStatus | null>(null);
  const [question, setQuestion] = useState<ReviewQuestion | null>(null);
  //: Why the engine stopped asking. The end of the review is not the same event
  //: as "there is nothing left to ask": a review can end on the question budget
  //: with most of the session still unattributed, and saying otherwise to the DM
  //: is the one thing this panel must not do.
  const [stop, setStop] = useState<ReviewStop | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [finished, setFinished] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      const [review, next] = await Promise.all([
        sessionsApi.review(token, sessionId),
        sessionsApi.nextQuestion(token, sessionId),
      ]);
      setStatus(review);
      setQuestion(next.question);
      setStop(next.stop ?? null);
      // A finished review is a CLOSED review: the DM pressed Finish, the run is
      // complete in the database, and the panel goes away instead of coming back
      // as "in review" on the next page load.
      setFinished(Boolean(review.finished));
      setPhase(next.question ? "ready" : "done");
    } catch (err) {
      // A 404 simply means this session has no attribution YET - the engine is
      // disabled, or the session has not reached it. That is the normal state
      // of a freshly created session, so the card renders nothing at all.
      //
      // Branching on the STATUS, not on the message: a mis-routed call is also
      // a 404, and its body ("Not Found") mentions neither attribution nor a
      // code, so text matching put a red box on every new session.
      if (err instanceof ApiError && err.status === 404) {
        setPhase("disabled");
        return;
      }
      setError(errMessage(err));
      setPhase("error");
    }
    // sessionStatus is a dependency on purpose: the pipeline runs while the DM
    // watches, and the card has to notice when it finishes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, sessionId, sessionStatus]);

  useEffect(() => {
    void load();
  }, [load]);

  const answer = useCallback(
    async (optionKey: string) => {
      if (!token || !question) return;
      setBusy(true);
      setError(null);
      try {
        const result = await sessionsApi.answerQuestion(
          token,
          sessionId,
          question.prompt_text,
          optionKey,
        );
        setNote(
          result.resolved_utterances > 0
            ? `Thanks - that also resolved ${result.resolved_utterances} other moment${result.resolved_utterances === 1 ? "" : "s"} (${Math.round(result.resolved_sec)}s of speech).`
            : "Thanks - recorded.",
        );
        const next = await sessionsApi.nextQuestion(token, sessionId);
        setQuestion(next.question);
        setStop(next.stop ?? null);
        setPhase(next.question ? "ready" : "done");
        const fresh = await sessionsApi.review(token, sessionId);
        setStatus(fresh);
        setFinished(Boolean(fresh.finished));
      } catch (err) {
        setError(errMessage(err));
      } finally {
        setBusy(false);
      }
    },
    [token, sessionId, question],
  );

  const skip = useCallback(async () => {
    if (!token || !question) return;
    setBusy(true);
    try {
      await sessionsApi.skipQuestion(token, sessionId, question.prompt_text);
      const next = await sessionsApi.nextQuestion(token, sessionId);
      setQuestion(next.question);
      setStop(next.stop ?? null);
      setPhase(next.question ? "ready" : "done");
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }, [token, sessionId, question]);

  const finish = useCallback(async () => {
    if (!token) return;
    setBusy(true);
    try {
      await sessionsApi.finishReview(token, sessionId);
      // Gone, immediately and for good: the session moves on to its summary, and
      // the panel does not come back on the next page load.
      setFinished(true);
      setPhase("done");
      onFinished?.();
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }, [token, sessionId, onFinished]);

  if (phase === "disabled") {
    return null; // the engine is off: the page behaves as it did before
  }

  if (finished) {
    // The review is over: the panel's job is done and it leaves the page. The
    // session's own status badge ("summarizing", "summary ready") is what tells
    // the DM where the session is now.
    return null;
  }

  if (phase === "loading") {
    return (
      <Card>
        <div className="flex items-center gap-3 text-sm text-[color:var(--rl-text-on-parchment-muted)]">
          <Spinner /> Checking what we still need to ask…
        </div>
      </Card>
    );
  }

  if (phase === "error") {
    return (
      <Card>
        <Alert>Could not load the review: {error}</Alert>
      </Card>
    );
  }

  return (
    <Card className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="rl-title text-lg text-[color:var(--rl-text-on-parchment-primary)]">Who said what</h2>
          <p className="text-sm text-[color:var(--rl-text-on-parchment-muted)]">
            We work out the speakers from the audio and the conversation. You only need to
            settle what we genuinely cannot.
          </p>
        </div>
        {status ? <Badge tone={status.finished ? "green" : "blue"}>{status.finished ? "review done" : "in review"}</Badge> : null}
      </div>

      {status ? (
        <>
          <CoverageBar
            coverage={status.coverage}
            unresolved={status.unresolved}
            plan={status.plan}
          />
          <AttributionBuckets buckets={status.buckets} />
        </>
      ) : null}

      {note ? <Alert tone="success">{note}</Alert> : null}
      {error ? <Alert>{error}</Alert> : null}

      {phase === "done" ? (
        <div className="space-y-3">
          <EmptyState>{STOP_EXPLANATION[stop?.reason ?? ""] ?? STOP_EXPLANATION.default}</EmptyState>
          <div className="flex flex-wrap gap-2">
            <Button variant="ghost" onClick={() => void load()} disabled={busy}>
              Look again
            </Button>
            <Button variant="primary" onClick={() => void finish()} disabled={busy}>
              Finish review
            </Button>
          </div>
        </div>
      ) : null}

      {phase === "ready" && question ? (
        <QuestionCard
          question={question}
          busy={busy}
          onAnswer={answer}
          onSkip={skip}
          onSeek={onSeek}
        />
      ) : null}

      {phase === "ready" ? (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[color:var(--rl-border-parchment)] pt-3">
          <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">
            You can stop at any point: the session is not blocked on this.
          </p>
          <Button variant="ghost" onClick={() => void finish()} disabled={busy}>
            Finish anyway
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

/**
 * One question.
 *
 * The hook is what makes it answerable in seconds: the quote, and the audio the
 * DM compares without reading anything at all. What to play comes from the hook
 * (samples, or the question's own span) rather than from the question kind.
 */
export function QuestionCard({
  question,
  busy,
  onAnswer,
  onSkip,
  onSeek,
}: {
  question: ReviewQuestion;
  busy: boolean;
  onAnswer: (optionKey: string) => void | Promise<void>;
  onSkip?: () => void | Promise<void>;
  onSeek?: (seconds: number) => void;
}) {
  return (
    <div className="space-y-4 rounded-lg border border-[color:var(--rl-border-parchment)] bg-[color:var(--rl-bg-card)] p-4">
      <div className="space-y-2">
        <p className="text-base font-medium text-[color:var(--rl-text-on-parchment-primary)]">{question.prompt_text}</p>
        {question.hook.quote ? (
          <blockquote className="border-l-2 border-[color:var(--rl-border-parchment)] pl-3 text-sm italic text-[color:var(--rl-text-on-parchment-muted)]">
            “{question.hook.quote}”
          </blockquote>
        ) : null}
        {question.hook.note ? (
          <p className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">{question.hook.note}</p>
        ) : null}
      </div>

      {question.hook.audio ? (
        <Button
          variant="ghost"
          onClick={() => onSeek?.(question.hook.audio!.start)}
          title={`${fmtDuration(question.hook.audio.start)} – ${fmtDuration(question.hook.audio.end)}`}
        >
          ▶ Listen
        </Button>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {question.options.map((option) => (
          <Button
            key={option.key}
            variant={option.key === "idk" ? "ghost" : "primary"}
            disabled={busy}
            onClick={() => void onAnswer(option.key)}
            title={option.why}
          >
            {option.label}
          </Button>
        ))}
      </div>

      {onSkip ? (
        <button
          type="button"
          className="text-xs text-[color:var(--rl-text-on-parchment-muted)] underline-offset-2 hover:underline"
          onClick={() => void onSkip()}
          disabled={busy}
        >
          Never ask me this one
        </button>
      ) : null}
    </div>
  );
}
