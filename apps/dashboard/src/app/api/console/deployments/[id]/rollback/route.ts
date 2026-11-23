import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  readControlPlaneConfig,
  rollbackControlPlaneDeployment,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
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
  try {
    const payload = (await request.json()) as { restore_variables?: unknown };
    if ("restore_variables" in payload && typeof payload.restore_variables !== "boolean") {
      return jsonError(
        400,
        "restore_variables must be a boolean.",
        "invalid_restore_variables",
      );
    }
    if (typeof payload.restore_variables === "boolean") {
      restoreVariables = payload.restore_variables;
    }
  } catch {
    // Empty or non-JSON body keeps the default restore.
  }
  try {
    const deployment = await rollbackControlPlaneDeployment(config, id, {
      actorUserId: user.id,
      restoreVariables,
    });
    return NextResponse.json(deployment, { status: 201 });
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_deployment_rollback_failed");
  }
}
