import { connection, NextResponse } from "next/server";

import {
  deleteControlPlaneProjectMember,
  isControlPlaneUuid,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ id: string; userId: string }> },
) {
  await connection();
  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Project members require the control plane.", "not_found");
  }
  const { id, userId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(userId)) {
    return jsonError(400, "Project and user ids must be UUIDs.", "invalid_id");
  }
  try {
    await deleteControlPlaneProjectMember(config, id, userId, user.id);
    return new NextResponse(null, { status: 204 });
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_project_members_delete_failed");
  }
}
