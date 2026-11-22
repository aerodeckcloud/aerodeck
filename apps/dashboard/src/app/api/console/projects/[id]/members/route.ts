import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  listControlPlaneProjectMembers,
  readControlPlaneConfig,
  upsertControlPlaneProjectMember,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";
import type { ProjectRole } from "@/lib/types";

const ALLOWED_ROLES = new Set<ProjectRole>(["owner", "developer", "viewer"]);

export async function GET(
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
    return jsonError(404, "Project members require the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Project id must be a UUID.", "invalid_project_id");
  }
  try {
    const members = await listControlPlaneProjectMembers(config, id);
    return NextResponse.json(members);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_project_members_list_failed");
  }
}

export async function PUT(
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
    return jsonError(404, "Project members require the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Project id must be a UUID.", "invalid_project_id");
  }
  let body: { email?: unknown; role?: unknown };
  try {
    body = (await request.json()) as { email?: unknown; role?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const role = typeof body.role === "string" ? body.role : "developer";
  if (!email.includes("@") || !ALLOWED_ROLES.has(role as ProjectRole)) {
    return jsonError(400, "Valid email and role (owner, developer, viewer) are required.", "invalid_member");
  }
  try {
    const member = await upsertControlPlaneProjectMember(
      config,
      id,
      email,
      role as ProjectRole,
      user.id,
    );
    return NextResponse.json(member);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_project_members_upsert_failed");
  }
}
