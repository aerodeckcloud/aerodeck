import { connection, NextResponse } from "next/server";

import {
  getControlPlaneDeploymentRuntimeLogs,
  isControlPlaneUuid,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function GET(
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
    return jsonError(404, "Runtime container logs require the control plane.", "not_found");
  }

  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }

  const url = new URL(request.url);
  const rawTail = Number.parseInt(url.searchParams.get("tail") ?? "200", 10);
  const tail = Number.isFinite(rawTail) ? Math.max(1, Math.min(rawTail, 500)) : 200;

  try {
    const logs = await getControlPlaneDeploymentRuntimeLogs(config, id, tail, {
      actorUserId: user.id,
    });
    return NextResponse.json(logs);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_deployment_runtime_logs_failed");
  }
}
