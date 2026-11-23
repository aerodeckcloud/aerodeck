import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  readControlPlaneConfig,
  rollbackControlPlaneDeployment,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { rollbackRefusalResponse } from "@/lib/rollback-refusal";
import { requireSessionUser } from "@/lib/session";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Rollback requires the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }
  let restoreVariables = true;
  const raw = await request.text();
  if (raw.trim()) {
    let payload: unknown;
    try {
      payload = JSON.parse(raw);
    } catch {
      return jsonError(400, "Rollback body must be a JSON object.", "invalid_rollback_body");
    }
    if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
      return jsonError(400, "Rollback body must be a JSON object.", "invalid_rollback_body");
    }
    const record = payload as { restore_variables?: unknown };
    if ("restore_variables" in record && typeof record.restore_variables !== "boolean") {
      return jsonError(
        400,
        "restore_variables must be a boolean.",
        "invalid_restore_variables",
      );
    }
    if (typeof record.restore_variables === "boolean") {
      restoreVariables = record.restore_variables;
    }
  }
  try {
    const deployment = await rollbackControlPlaneDeployment(config, id, {
      actorUserId: user.id,
      restoreVariables,
    });
    return NextResponse.json(deployment, { status: 201 });
  } catch (cause) {
    const refused = rollbackRefusalResponse(cause);
    if (refused) {
      return refused;
    }
    return jsonControlPlaneError(cause, "console_deployment_rollback_failed");
  }
}
