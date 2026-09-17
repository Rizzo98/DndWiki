// BFF proxy: forwards every /api/backend/* request to the API gateway
// (Traefik) server-side. The browser sends its Keycloak access token in the
// Authorization header; this handler re-sends it to the backend so services
// can validate the JWT themselves. Proxying server-side avoids CORS entirely
// (services ship without CORS middleware by design).
//
// Multipart bodies (avatar upload, session recording, voiceprint enrollment)
// are forwarded byte-for-byte.

export const dynamic = "force-dynamic";

// The BFF runs inside the alpine (musl) web container, where *.localhost names
// resolve to the container's own loopback (RFC 6761) and can never reach the host.
// Reach Traefik by its compose service name instead; the gateway accepts Host(`gateway`).
const API_BASE = process.env.API_GATEWAY_URL || "http://gateway";

async function handle(req: Request, ctx: { params: { path: string[] } }) {
  const rest = (ctx.params.path ?? []).join("/");
  const url = new URL(req.url);
  const target = `${API_BASE}/${rest}${url.search}`;

  const headers: Record<string, string> = {};
  const auth = req.headers.get("authorization");
  if (auth) headers["authorization"] = auth;
  const contentType = req.headers.get("content-type");
  if (contentType) headers["content-type"] = contentType;

  const method = req.method;
  const body = method === "GET" || method === "HEAD" ? undefined : await req.arrayBuffer();

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method,
      headers,
      body,
      // Wait long enough for large uploads + slow LLM-backed endpoints.
      signal: AbortSignal.timeout(15 * 60 * 1000),
    });
  } catch (err) {
    return new Response(
      JSON.stringify({
        detail: `Gateway unreachable (${API_BASE}): ${err instanceof Error ? err.message : String(err)}`,
      }),
      { status: 502, headers: { "content-type": "application/json" } },
    );
  }

  // 204/205/304 are null-body statuses: the Response constructor throws
  // ("Invalid response status code") when handed a body for them — even an
  // empty one — which turned every "revoke invite"/"remove member"/"delete
  // cover" into a 500 at the BFF.
  const nullBody =
    upstream.status === 204 || upstream.status === 205 || upstream.status === 304;
  const payload = nullBody ? null : await upstream.arrayBuffer();
  const responseHeaders: Record<string, string> = {
    "content-type": upstream.headers.get("content-type") ?? "application/json",
  };
  return new Response(payload, { status: upstream.status, headers: responseHeaders });
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const PATCH = handle;
export const DELETE = handle;
