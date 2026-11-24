import { connection, NextResponse } from "next/server";

import { ControlPlaneError, createControlPlaneDraft, readControlPlaneConfig } from "@/lib/control-plane";
import type { DeployDraftPlan } from "@/lib/deploy-draft";
import { jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function POST(request: Request) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Deployments stay on the mock API.", "not_found");
  }
  let payload: { repo_url?: unknown; plan?: unknown };
  try {
    payload = (await request.json()) as { repo_url?: unknown; plan?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (typeof payload.repo_url !== "string" || payload.repo_url.trim() === "" || !payload.plan) {
    return jsonError(400, "repo_url and plan are required.", "invalid_draft");
  }
  try {
    const saved = await createControlPlaneDraft(config, payload.repo_url, payload.plan as DeployDraftPlan, {
      actorUserId: user.id,
    });
    return NextResponse.json(saved, { status: 201 });
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 422) {
      return jsonError(400, "The draft could not be saved.", "invalid_draft");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "request failed";
    console.error("event=console_draft_create_failed detail=%s", detail);
    return jsonError(502, "Control plane request failed.", "control_plane_unavailable");
  }
}
