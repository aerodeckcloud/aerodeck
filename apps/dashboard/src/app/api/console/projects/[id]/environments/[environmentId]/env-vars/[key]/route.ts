import { connection, NextResponse } from "next/server";

import {
  deleteControlPlaneEnvironmentVar,
  isControlPlaneUuid,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ id: string; environmentId: string; key: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Environment variables require the control plane.", "not_found");
  }
  const { id, environmentId, key } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId) || !ENV_KEY_RE.test(key)) {
    return jsonError(400, "Invalid project, environment, or variable key.", "invalid_id");
  }
  try {
    await deleteControlPlaneEnvironmentVar(config, id, environmentId, key);
    return new NextResponse(null, { status: 204 });
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_env_vars_delete_failed");
  }
}
