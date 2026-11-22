import { connection, NextResponse } from "next/server";

import {
  getControlPlaneContainerMetrics,
  isControlPlaneUuid,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string; environmentId: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Container metrics require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  try {
    const metrics = await getControlPlaneContainerMetrics(config, id, environmentId);
    return NextResponse.json(metrics);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_container_metrics_failed");
  }
}
