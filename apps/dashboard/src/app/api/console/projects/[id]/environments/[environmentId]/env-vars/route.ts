import { connection, NextResponse } from "next/server";

import {
  isControlPlaneUuid,
  listControlPlaneEnvironmentVars,
  readControlPlaneConfig,
  upsertControlPlaneEnvironmentVars,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";
import type { EnvironmentVarUpsertItem } from "@/lib/types";

const ENV_KEY_RE = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

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
    return jsonError(404, "Environment variables require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  try {
    const items = await listControlPlaneEnvironmentVars(config, id, environmentId);
    return NextResponse.json(items);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_env_vars_list_failed");
  }
}

export async function PUT(
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
    return jsonError(404, "Environment variables require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  let body: { items?: unknown };
  try {
    body = (await request.json()) as { items?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (!Array.isArray(body.items) || body.items.length === 0 || body.items.length > 100) {
    return jsonError(400, "Provide between 1 and 100 environment variables.", "invalid_env_vars");
  }
  const parsed: EnvironmentVarUpsertItem[] = [];
  for (const raw of body.items) {
    if (!raw || typeof raw !== "object") {
      return jsonError(400, "Invalid environment variable entry.", "invalid_env_vars");
    }
    const row = raw as Record<string, unknown>;
    const key = typeof row.key === "string" ? row.key.trim() : "";
    const value = typeof row.value === "string" ? row.value : "";
    if (!ENV_KEY_RE.test(key) || value.length > 8192) {
      return jsonError(400, `Invalid environment variable key or value: ${key || "(empty)"}`, "invalid_env_vars");
    }
    parsed.push({
      key,
      value,
      is_secret: typeof row.is_secret === "boolean" ? row.is_secret : true,
    });
  }
  try {
    const items = await upsertControlPlaneEnvironmentVars(config, id, environmentId, parsed);
    return NextResponse.json(items);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_env_vars_upsert_failed");
  }
}
