import { connection, NextResponse } from "next/server";

import {
  acknowledgeControlPlaneSecurity,
  ControlPlaneError,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Deployments stay on the mock API.", "not_found");
  }
  let payload: { acknowledgement?: unknown };
  try {
    payload = (await request.json()) as { acknowledgement?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (payload.acknowledgement !== "findings" && payload.acknowledgement !== "incomplete") {
    return jsonError(400, "The acknowledgement was not recognized.", "invalid_draft");
  }
  const { id } = await context.params;
  try {
    const report = await acknowledgeControlPlaneSecurity(config, id, payload.acknowledgement, {
      actorUserId: user.id,
    });
    return NextResponse.json(report);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 404) {
      return jsonError(404, "Draft was not found.", "not_found");
    }
    if (cause instanceof ControlPlaneError && cause.status === 422) {
      return jsonError(400, "The dependency scan could not be acknowledged.", "invalid_draft");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "request failed";
    console.error("event=console_security_ack_failed detail=%s", detail);
    return jsonError(502, "The dependency scan did not complete.", "control_plane_unavailable");
  }
}
