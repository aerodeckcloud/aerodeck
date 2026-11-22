import { connection, NextResponse } from "next/server";

import { readControlPlaneConfig } from "@/lib/control-plane";
import { jsonError } from "@/lib/mock-http";

const UPSTREAM_TIMEOUT_MS = 15_000;

/**
 * Public GitHub webhook proxy (#249).
 * Streams the raw request body and GitHub signature headers directly to the
 * control-plane `/api/v1/webhooks/github` endpoint with an explicit upstream timeout.
 */
export async function POST(request: Request) {
  await connection();
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Webhooks require the control plane.", "not_found");
  }

  const headers: Record<string, string> = {
    "content-type": request.headers.get("content-type") ?? "application/json",
  };
  for (const name of ["x-github-event", "x-hub-signature-256", "x-github-delivery"] as const) {
    const val = request.headers.get(name);
    if (val) {
      headers[name] = val;
    }
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
  try {
    const init: RequestInit & { duplex?: "half" } = {
      method: "POST",
      headers,
      body: request.body,
      duplex: "half",
      signal: controller.signal,
      cache: "no-store",
    };
    const upstream = await fetch(`${config.baseUrl}/api/v1/webhooks/github`, init);
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (cause) {
    if (cause instanceof Error && cause.name === "AbortError") {
      return jsonError(504, "Control plane webhook receiver timed out.", "webhook_proxy_timeout");
    }
    return jsonError(502, "Could not reach control plane webhook receiver.", "webhook_proxy_failed");
  } finally {
    clearTimeout(timer);
  }
}
