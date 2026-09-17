// /system — read-only status of the AI/ML pipeline endpoints.

"use client";

import { Alert, Badge, Card, EmptyState } from "@/components/ui";
import { AuthGate, useAuth } from "@/lib/auth";
import { systemApi } from "@/lib/api";
import { useAsyncData } from "@/lib/use-async";

export default function SystemPage() {
  const { token } = useAuth();
  const { data: models } = useAsyncData((t) => systemApi.transcriptionModels(t));
  const { data: status } = useAsyncData((t) => systemApi.transcriptionStatus(t));
  const { data: speaker } = useAsyncData((t) => systemApi.speakerModel(t));

  return (
    <AuthGate>
    <div className="space-y-8">
      <div>
        <h1 className="rl-title text-3xl">System</h1>
        <p className="mt-1 text-sm text-[color:var(--rl-text-on-parchment-muted)]">Configured AI/ML pipeline (read-only).</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">Transcription</h2>
          {models ? (
            <dl className="space-y-2 text-sm">
              <div><dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">ASR model</dt><dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{models.asr_model}</dd></div>
              <div><dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Diarization</dt><dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{models.diarization_model}</dd></div>
              <div><dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Compute</dt><dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{models.compute_type}</dd></div>
            </dl>
          ) : (
            <EmptyState>transcription-service unreachable</EmptyState>
          )}
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">Runtime</h2>
          {status ? (
            <dl className="space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Device</dt>
                <dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{status.device ?? "auto"}</dd>
              </div>
              <div className="flex items-center justify-between">
                <dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">ASR loaded</dt>
                <dd>{status.asr_loaded ? <Badge tone="green">ready</Badge> : <Badge tone="amber">cold</Badge>}</dd>
              </div>
              <div className="flex items-center justify-between">
                <dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Diarizer loaded</dt>
                <dd>{status.diarizer_loaded ? <Badge tone="green">ready</Badge> : <Badge tone="amber">cold</Badge>}</dd>
              </div>
            </dl>
          ) : (
            <EmptyState>transcription-service unreachable</EmptyState>
          )}
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-[color:var(--rl-text-on-parchment-muted)]">Speaker identification</h2>
          {speaker ? (
            <dl className="space-y-2 text-sm">
              <div><dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Embedding model</dt><dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{speaker.model}</dd></div>
              <div><dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Match threshold</dt><dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{speaker.threshold}</dd></div>
              <div><dt className="text-xs text-[color:var(--rl-text-on-parchment-muted)]">Collection</dt><dd className="font-mono text-xs text-[color:var(--rl-text-on-parchment-primary)]">{speaker.collection}</dd></div>
            </dl>
          ) : (
            <EmptyState>speaker-service unreachable</EmptyState>
          )}
        </Card>
      </div>

      <Alert tone="info">
        If a service shows &ldquo;unreachable&rdquo;, make sure the docker-compose stack is up and that the{" "}
        <code className="rounded bg-[color:var(--rl-bg-parchment-sunk)] px-1">gateway</code> (Traefik) container is routing requests. No hosts
        file entries are needed: on Windows <code className="rounded bg-[color:var(--rl-bg-parchment-sunk)] px-1">*.localhost</code> resolves
        automatically, and the app reaches the gateway over the Docker network.
      </Alert>
    </div>
    </AuthGate>
  );
}
