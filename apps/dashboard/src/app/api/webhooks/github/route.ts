import { connection, NextResponse } from "next/server";

import { readControlPlaneConfig } from "@/lib/control-plane";
import { jsonError } from "@/lib/mock-http";

/**
 * Public GitHub webhook proxy (#249).
 * Forwards the raw body bytes and GitHub signature headers directly to the
 * control-plane `/api/v1/webhooks/github` endpoint without modifying bytes.
 */
export async function POST(request: Request) {
  await connection();
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Webhooks require the control plane.", "not_found");
  }

  const rawBody = await request.arrayBuffer();
  const headers: Record<string, string> = {
    "content-type": request.headers.get("content-type") ?? "application/json",
  };
  for (const name of ["x-github-event", "x-hub-signature-256", "x-github-delivery"] as const) {
    const val = request.headers.get(name);
    if (val) {
      headers[name] = val;
    }
  }

  try {
    const upstream = await fetch(`${config.baseUrl}/api/v1/webhooks/github`, {
      method: "POST",
      headers,
      body: rawBody,
      cache: "no-store",
    });
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return jsonError(502, "Could not reach control plane webhook receiver.", "webhook_proxy_failed");
  }
}
