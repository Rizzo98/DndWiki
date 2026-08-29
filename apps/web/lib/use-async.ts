// useAsyncData: small data-fetching hook that reads the auth token, runs a
// loader, and exposes { data, error, loading, reload }.

"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "./auth";
import { ApiError } from "./api";

export function errMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { detail?: unknown } | string | null | undefined;
    if (typeof d === "string") return d;
    if (d && typeof d === "object" && "detail" in d && typeof d.detail === "string") return d.detail;
    // FastAPI validation errors arrive as an array of {type, loc, msg, ...};
    // render them readably ("members.0.player_name: String should have at least 1 character")
    // instead of the opaque "422 Unprocessable Entity".
    if (Array.isArray(d)) {
      const parts = d
        .filter((item): item is { loc?: unknown[]; msg?: unknown } => typeof item === "object" && item !== null)
        .map((item) => {
          const loc = (item.loc ?? []).filter((p): p is string => typeof p === "string" && p !== "body").join(".");
          const msg = typeof item.msg === "string" ? item.msg : "Invalid value";
          return loc ? `${loc}: ${msg}` : msg;
        });
      if (parts.length > 0) return parts.join("; ");
    }
    return `${err.status} ${err.message}`;
  }
  return err instanceof Error ? err.message : String(err);
}

export function useAsyncData<T>(load: (token: string) => Promise<T>, deps: unknown[] = []) {
  const { token } = useAuth();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    // Only the first load (no data yet) may flip the UI to a loading/error
    // state. Background reloads (polling) keep showing the previous data so
    // the page does not flicker "Loading…" on every poll.
    const firstLoad = data === null;
    if (firstLoad) {
      setLoading(true);
      setError(null);
    }
    load(token)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          if (firstLoad) setError(errMessage(e));
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, nonce, ...deps]);

  return { data, error, loading, reload };
}