import { connection, NextResponse } from "next/server";

import {
  cloneControlPlaneEnvironmentVars,
  isControlPlaneUuid,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

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
    return jsonError(404, "Environment variable cloning requires the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  let body: { source_environment_id?: unknown; overwrite?: unknown };
  try {
    body = (await request.json()) as { source_environment_id?: unknown; overwrite?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (
    typeof body.source_environment_id !== "string" ||
    !isControlPlaneUuid(body.source_environment_id)
  ) {
    return jsonError(400, "source_environment_id must be a UUID.", "invalid_source_environment_id");
  }
  try {
    const result = await cloneControlPlaneEnvironmentVars(
      config,
      id,
      environmentId,
      body.source_environment_id,
      body.overwrite === true,
    );
    return NextResponse.json(result);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_env_vars_clone_failed");
  }
}
