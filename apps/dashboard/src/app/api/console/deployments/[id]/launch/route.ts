import { connection, NextResponse } from "next/server";

import { ControlPlaneError, launchControlPlaneDraft, readControlPlaneConfig } from "@/lib/control-plane";
import type { DeployDraftPlan } from "@/lib/deploy-draft";
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
  const { id } = await context.params;
  let payload: { plan?: unknown; project_id?: unknown; environment_id?: unknown };
  try {
    payload = (await request.json()) as { plan?: unknown; project_id?: unknown; environment_id?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  const projectId = typeof payload.project_id === "string" ? payload.project_id : "";
  const environmentId = typeof payload.environment_id === "string" ? payload.environment_id : "";
  if (!payload.plan || projectId.length === 0 || environmentId.length === 0) {
    return jsonError(400, "plan, project_id, and environment_id are required.", "invalid_draft");
  }
  try {
    const deployment = await launchControlPlaneDraft(
      config,
      id,
      payload.plan as DeployDraftPlan,
      projectId,
      environmentId,
      { actorUserId: user.id },
    );
    return NextResponse.json(deployment, { status: 202 });
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status !== undefined && cause.status >= 500) {
      const detail = "Launch did not start. This draft is still open, so you can try again.";
      console.error("event=console_draft_launch_failed detail=%s", cause.message);
      return jsonError(
        cause.status === 503 ? 503 : 502,
        detail,
        cause.status === 503 ? "queue_unavailable" : "control_plane_unavailable",
      );
    }
    if (cause instanceof ControlPlaneError && cause.status === 404) {
      return jsonError(404, "Draft was not found.", "not_found");
    }
    if (cause instanceof ControlPlaneError && cause.status === 422) {
      return jsonError(400, cause.message, "invalid_draft");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "request failed";
    console.error("event=console_draft_launch_failed detail=%s", detail);
    return jsonError(502, "Control plane request failed.", "control_plane_unavailable");
  }
}
