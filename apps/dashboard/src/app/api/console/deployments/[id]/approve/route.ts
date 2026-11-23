import { connection, NextResponse } from "next/server";

import {
  approveControlPlaneDeployment,
  ControlPlaneError,
  isControlPlaneUuid,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Approval requires the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }
  try {
    const deployment = await approveControlPlaneDeployment(config, id, { actorUserId: user.id });
    return NextResponse.json(deployment);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 403) {
      return jsonError(403, "Only an owner can approve this deployment.", "forbidden");
    }
    if (cause instanceof ControlPlaneError && cause.status === 409) {
      return jsonError(409, cause.message, "conflict");
    }
    return jsonControlPlaneError(cause, "console_deployment_approve_failed");
  }
}
