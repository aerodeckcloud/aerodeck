import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  readControlPlaneConfig,
  redeployControlPlaneDeployment,
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
    return jsonError(404, "Redeploy requires the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }
  try {
    const deployment = await redeployControlPlaneDeployment(config, id, { actorUserId: user.id });
    return NextResponse.json(deployment, { status: 201 });
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_deployment_redeploy_failed");
  }
}
