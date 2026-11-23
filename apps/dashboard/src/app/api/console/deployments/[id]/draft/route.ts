import { connection, NextResponse } from "next/server";

import {
  ControlPlaneError,
  readControlPlaneDraft,
  updateControlPlaneDraft,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import type { DeployDraftPlan } from "@/lib/deploy-draft";
import { jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

async function sessionConfig(requestReady: Promise<unknown>) {
  await requestReady;
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Deployments stay on the mock API.", "not_found");
  }
  return { user, config };
}

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const ready = await sessionConfig(connection());
  if (ready instanceof NextResponse) {
    return ready;
  }
  const { id } = await context.params;
  try {
    const saved = await readControlPlaneDraft(ready.config, id, { actorUserId: ready.user.id });
    return NextResponse.json(saved);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 404) {
      return jsonError(404, "Draft was not found.", "not_found");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "request failed";
    console.error("event=console_draft_read_failed detail=%s", detail);
    return jsonError(502, "Control plane request failed.", "control_plane_unavailable");
  }
}

export async function PUT(request: Request, context: { params: Promise<{ id: string }> }) {
  const ready = await sessionConfig(connection());
  if (ready instanceof NextResponse) {
    return ready;
  }
  const { id } = await context.params;
  let payload: { plan?: unknown; project_id?: unknown; environment_id?: unknown };
  try {
    payload = (await request.json()) as { plan?: unknown; project_id?: unknown; environment_id?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (!payload.plan) {
    return jsonError(400, "plan is required.", "invalid_draft");
  }
  const projectId = typeof payload.project_id === "string" ? payload.project_id : undefined;
  const environmentId = typeof payload.environment_id === "string" ? payload.environment_id : undefined;
  try {
    const saved = await updateControlPlaneDraft(ready.config, id, payload.plan as DeployDraftPlan, {
      actorUserId: ready.user.id,
      projectId,
      environmentId,
    });
    return NextResponse.json(saved);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 404) {
      return jsonError(404, "Draft was not found.", "not_found");
    }
    if (cause instanceof ControlPlaneError && cause.status === 422) {
      return jsonError(400, "The draft could not be saved.", "invalid_draft");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "request failed";
    console.error("event=console_draft_update_failed detail=%s", detail);
    return jsonError(502, "Control plane request failed.", "control_plane_unavailable");
  }
}
