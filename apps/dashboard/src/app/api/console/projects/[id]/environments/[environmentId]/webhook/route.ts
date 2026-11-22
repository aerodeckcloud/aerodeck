import { connection, NextResponse } from "next/server";

import {
  deleteControlPlaneEnvironmentWebhook,
  getControlPlaneEnvironmentWebhook,
  isControlPlaneUuid,
  readControlPlaneConfig,
  upsertControlPlaneEnvironmentWebhook,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";
import type { AddonType, EnvironmentWebhookInput } from "@/lib/types";

const ALLOWED_ADDONS = new Set<AddonType>(["postgres", "redis", "mongodb"]);

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
    return jsonError(404, "Webhooks require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  try {
    const webhook = await getControlPlaneEnvironmentWebhook(config, id, environmentId);
    return NextResponse.json(webhook);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_webhook_read_failed");
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
    return jsonError(404, "Webhooks require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  const repoUrl = typeof body.repo_url === "string" ? body.repo_url.trim() : "";
  const branch = typeof body.branch === "string" && body.branch.trim() ? body.branch.trim() : "main";
  if (!repoUrl) {
    return jsonError(400, "repo_url is required.", "invalid_webhook");
  }
  const addons = Array.isArray(body.addons)
    ? body.addons.filter((a): a is AddonType => typeof a === "string" && ALLOWED_ADDONS.has(a as AddonType))
    : [];
  const input: EnvironmentWebhookInput = {
    repo_url: repoUrl,
    branch,
    addons,
    enabled: typeof body.enabled === "boolean" ? body.enabled : true,
    rotate_secret: body.rotate_secret === true,
  };
  try {
    const webhook = await upsertControlPlaneEnvironmentWebhook(config, id, environmentId, input);
    return NextResponse.json(webhook);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_webhook_upsert_failed");
  }
}

export async function DELETE(
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
    return jsonError(404, "Webhooks require the control plane.", "not_found");
  }
  const { id, environmentId } = await context.params;
  if (!isControlPlaneUuid(id) || !isControlPlaneUuid(environmentId)) {
    return jsonError(400, "Project and environment ids must be UUIDs.", "invalid_id");
  }
  try {
    await deleteControlPlaneEnvironmentWebhook(config, id, environmentId);
    return new NextResponse(null, { status: 204 });
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_webhook_delete_failed");
  }
}
