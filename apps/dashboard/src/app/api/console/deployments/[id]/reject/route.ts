import { connection, NextResponse } from "next/server";

import {
  ControlPlaneError,
  isControlPlaneUuid,
  readControlPlaneConfig,
  rejectControlPlaneDeployment,
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
    return jsonError(404, "Rejection requires the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }
  try {
    const deployment = await rejectControlPlaneDeployment(config, id, { actorUserId: user.id });
    return NextResponse.json(deployment);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 403) {
      return jsonError(403, "Only an owner can reject this deployment.", "forbidden");
    }
    if (cause instanceof ControlPlaneError && cause.status === 409) {
      return jsonError(409, cause.message, "conflict");
    }
    return jsonControlPlaneError(cause, "console_deployment_reject_failed");
  }
}
