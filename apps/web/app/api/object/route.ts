// Object/media proxy: forwards requests to presigned MinIO object URLs.
//
// Services generate presigned URLs with the docker-internal host
// (http://minio:9000/...), which the browser cannot resolve. The Next.js
// server is on the same Docker network, so audio/transcripts/avatars are
// proxied server-side. Range requests are forwarded so the <audio> player
// can seek. Only MinIO-ish hosts are allowed (presigned URLs are signed for
// their exact host, so the host cannot be rewritten).

export const dynamic = "force-dynamic";

const ALLOWED_HOSTS = new Set(["minio", "localhost", "127.0.0.1"]);

export async function GET(req: Request) {
  const json = (status: number, body: Record<string, unknown>) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });

  const url = new URL(req.url).searchParams.get("url");
  if (!url) return json(400, { error: "missing url parameter" });

  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return json(400, { error: "url is not a valid URL" });
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return json(400, { error: "url must be http(s)" });
  }
  if (!ALLOWED_HOSTS.has(parsed.hostname)) {
    return json(400, { error: `host not allowed: ${parsed.hostname}` });
  }

  const headers: Record<string, string> = {};
  const range = req.headers.get("range");
  if (range) headers["range"] = range;

  let upstream: Response;
  try {
    upstream = await fetch(url, { headers, signal: AbortSignal.timeout(120_000) });
  } catch (err) {
    return json(502, {
      error: "object fetch failed",
      detail: err instanceof Error ? err.message : String(err),
    });
  }

  const body = await upstream.arrayBuffer();
  const outHeaders: Record<string, string> = {
    "content-type": upstream.headers.get("content-type") ?? "application/octet-stream",
    "content-length": String(body.byteLength),
    "cache-control": "private, max-age=0",
  };
  if (upstream.status === 206) {
    const contentRange = upstream.headers.get("content-range");
    if (contentRange) outHeaders["content-range"] = contentRange;
    outHeaders["accept-ranges"] = "bytes";
  }
  return new Response(body, { status: upstream.status, headers: outHeaders });
}
