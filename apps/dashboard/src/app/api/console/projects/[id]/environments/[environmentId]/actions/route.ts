import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  readControlPlaneConfig,
  runControlPlaneContainerAction,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";
import type { ContainerAction } from "@/lib/types";

const ALLOWED_ACTIONS = new Set<ContainerAction>(["start", "stop", "restart"]);

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string; environmentId: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Container actions require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  let body: { action?: unknown };
  try {
    body = (await request.json()) as { action?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (typeof body.action !== "string" || !ALLOWED_ACTIONS.has(body.action as ContainerAction)) {
    return jsonError(400, "action must be one of: start, stop, restart.", "invalid_action");
  }
  try {
    const result = await runControlPlaneContainerAction(
      config,
      id,
      environmentId,
      body.action as ContainerAction,
    );
    return NextResponse.json(result);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_container_action_failed");
  }
}
