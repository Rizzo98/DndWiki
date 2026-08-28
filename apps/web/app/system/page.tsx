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
        <h1 className="text-3xl font-bold">System</h1>
        <p className="mt-1 text-sm text-slate-400">Configured AI/ML pipeline (read-only).</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Transcription</h2>
          {models ? (
            <dl className="space-y-2 text-sm">
              <div><dt className="text-xs text-slate-500">ASR model</dt><dd className="font-mono text-xs text-slate-300">{models.asr_model}</dd></div>
              <div><dt className="text-xs text-slate-500">Diarization</dt><dd className="font-mono text-xs text-slate-300">{models.diarization_model}</dd></div>
              <div><dt className="text-xs text-slate-500">Compute</dt><dd className="font-mono text-xs text-slate-300">{models.compute_type}</dd></div>
            </dl>
          ) : (
            <EmptyState>transcription-service unreachable</EmptyState>
          )}
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Runtime</h2>
          {status ? (
            <dl className="space-y-2 text-sm">
              <div className="flex items-center justify-between">
                <dt className="text-xs text-slate-500">Device</dt>
                <dd className="font-mono text-xs text-slate-300">{status.device ?? "auto"}</dd>
              </div>
              <div className="flex items-center justify-between">
                <dt className="text-xs text-slate-500">ASR loaded</dt>
                <dd>{status.asr_loaded ? <Badge tone="green">ready</Badge> : <Badge tone="amber">cold</Badge>}</dd>
              </div>
              <div className="flex items-center justify-between">
                <dt className="text-xs text-slate-500">Diarizer loaded</dt>
                <dd>{status.diarizer_loaded ? <Badge tone="green">ready</Badge> : <Badge tone="amber">cold</Badge>}</dd>
              </div>
            </dl>
          ) : (
            <EmptyState>transcription-service unreachable</EmptyState>
          )}
        </Card>

        <Card>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">Speaker identification</h2>
          {speaker ? (
            <dl className="space-y-2 text-sm">
              <div><dt className="text-xs text-slate-500">Embedding model</dt><dd className="font-mono text-xs text-slate-300">{speaker.model}</dd></div>
              <div><dt className="text-xs text-slate-500">Match threshold</dt><dd className="font-mono text-xs text-slate-300">{speaker.threshold}</dd></div>
              <div><dt className="text-xs text-slate-500">Collection</dt><dd className="font-mono text-xs text-slate-300">{speaker.collection}</dd></div>
            </dl>
          ) : (
            <EmptyState>speaker-service unreachable</EmptyState>
          )}
        </Card>
      </div>

      <Alert tone="info">
        If a service shows &ldquo;unreachable&rdquo;, make sure the docker-compose stack is up and that the{" "}
        <code className="rounded bg-slate-800 px-1">gateway</code> (Traefik) container is routing requests. No hosts
        file entries are needed: on Windows <code className="rounded bg-slate-800 px-1">*.localhost</code> resolves
        automatically, and the app reaches the gateway over the Docker network.
      </Alert>
    </div>
    </AuthGate>
  );
}
