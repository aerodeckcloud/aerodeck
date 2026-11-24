import { connection, NextResponse } from "next/server";

import { ControlPlaneError, readControlPlaneConfig, readControlPlaneSecurity } from "@/lib/control-plane";
import { jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Deployments stay on the mock API.", "not_found");
  }
  const { id } = await context.params;
  try {
    const report = await readControlPlaneSecurity(config, id, { actorUserId: user.id });
    return NextResponse.json(report);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 404) {
      return jsonError(404, "Draft was not found.", "not_found");
    }
    if (cause instanceof ControlPlaneError && cause.status === 422) {
      return jsonError(400, "The dependency scan did not complete.", "invalid_draft");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "request failed";
    console.error("event=console_security_scan_failed detail=%s", detail);
    return jsonError(502, "The dependency scan did not complete.", "control_plane_unavailable");
  }
}
