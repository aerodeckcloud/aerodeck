import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  listControlPlaneProjectDeployments,
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
    return jsonError(404, "Project deployments require the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Project id must be a UUID.", "invalid_project_id");
  }
  const envId = new URL(request.url).searchParams.get("environment_id") ?? undefined;
  if (envId && !isControlPlaneUuid(envId)) {
    return jsonError(400, "environment_id must be a UUID.", "invalid_environment_id");
  }
  try {
    const items = await listControlPlaneProjectDeployments(config, id, envId);
    return NextResponse.json(items);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_project_deployments_failed");
  }
}
