const BIND_HOSTS = new Set(["0.0.0.0", "::"]);

function firstHeader(value: string | null): string | null {
  const first = value?.split(",")[0]?.trim().toLowerCase();
  return first ? first : null;
}

function usableHost(value: string | null): string | null {
  if (!value || value.length > 253 || /[\s/@\\]/.test(value)) {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(`http://${value}`);
  } catch {
    return null;
  }
  if (
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    BIND_HOSTS.has(parsed.hostname)
  ) {
    return null;
  }
  return parsed.host;
}

/**
 * Browser origin for redirects.
 *
 * The dashboard container sets HOSTNAME=0.0.0.0 so Next can bind every
 * interface. That value then shows up as request.url, and a redirect to it
 * sends the browser to 0.0.0.0. Prefer the host the browser actually used.
 */
export function publicOrigin(request: Request): string {
  const url = new URL(request.url);
  const host =
    usableHost(firstHeader(request.headers.get("host"))) ??
    usableHost(firstHeader(request.headers.get("x-forwarded-host"))) ??
    usableHost(url.host);
  const forwardedProto = firstHeader(request.headers.get("x-forwarded-proto"));
  const proto =
    forwardedProto === "https" || forwardedProto === "http"
      ? forwardedProto
      : url.protocol === "https:"
        ? "https"
        : "http";
  if (host) {
    return `${proto}://${host}`;
  }
  const port = url.port || "3000";
  return `${proto}://localhost:${port}`;
}
