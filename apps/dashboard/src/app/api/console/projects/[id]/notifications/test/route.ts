import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  readControlPlaneConfig,
  testControlPlaneProjectNotifications,
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
    return jsonError(404, "Notifications require the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Project id must be a UUID.", "invalid_project_id");
  }
  try {
    const result = await testControlPlaneProjectNotifications(config, id, user.id);
    return NextResponse.json(result);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_notifications_test_failed");
  }
}
