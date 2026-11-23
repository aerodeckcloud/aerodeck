import { connection, NextResponse } from "next/server";

import { readJsonObject } from "@/lib/console-json-body";
import {
  ControlPlaneError,
  isControlPlaneUuid,
  promoteControlPlaneDeployment,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { promotionRefusal } from "@/lib/promotion";
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
    return jsonError(404, "Promote requires the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }
  const body = await readJsonObject(request);
  if (!body.ok) {
    return body.response;
  }
  const environmentId = body.value.environment_id;
  if (typeof environmentId !== "string" || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "environment_id must be a UUID.", "invalid_environment_id");
  }
  try {
    const deployment = await promoteControlPlaneDeployment(config, id, environmentId, {
      actorUserId: user.id,
    });
    return NextResponse.json(deployment, { status: 201 });
  } catch (cause) {
    if (cause instanceof ControlPlaneError) {
      const refused = promotionRefusal(cause.status, cause.message);
      if (refused) {
        return jsonError(refused.status, refused.detail, refused.code);
      }
    }
    return jsonControlPlaneError(cause, "console_deployment_promote_failed", "Deployment not found.", "The job queue is unavailable. Try again shortly.");
  }
}
