import { connection, NextResponse } from "next/server";

import {
  deleteControlPlaneProjectNotifications,
  getControlPlaneProjectNotifications,
  isControlPlaneUuid,
  readControlPlaneConfig,
  upsertControlPlaneProjectNotifications,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";
import type { NotificationChannelInput, NotificationProvider } from "@/lib/types";

const ALLOWED_PROVIDERS = new Set<NotificationProvider>(["slack", "discord", "webhook"]);

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
    return jsonError(404, "Notifications require the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Project id must be a UUID.", "invalid_project_id");
  }
  try {
    return NextResponse.json(await getControlPlaneProjectNotifications(config, id));
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_notifications_read_failed");
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
    return jsonError(404, "Notifications require the control plane.", "not_found");
  }
  const { id } = await context.params;
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Project id must be a UUID.", "invalid_project_id");
  }
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  const provider = typeof body.provider === "string" ? body.provider : "slack";
  if (!ALLOWED_PROVIDERS.has(provider as NotificationProvider)) {
    return jsonError(400, "provider must be slack, discord, or webhook.", "invalid_provider");
  }
  const input: NotificationChannelInput = {
    provider: provider as NotificationProvider,
    webhook_url: typeof body.webhook_url === "string" && body.webhook_url.trim() ? body.webhook_url.trim() : undefined,
    notify_on_started: body.notify_on_started === true,
    notify_on_succeeded: body.notify_on_succeeded !== false,
    notify_on_failed: body.notify_on_failed !== false,
    enabled: body.enabled !== false,
  };
  try {
    const channel = await upsertControlPlaneProjectNotifications(config, id, input, user.id);
    return NextResponse.json(channel);
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_notifications_upsert_failed");
  }
}

export async function DELETE(
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
    await deleteControlPlaneProjectNotifications(config, id, user.id);
    return new NextResponse(null, { status: 204 });
  } catch (cause) {
    return jsonControlPlaneError(cause, "console_notifications_delete_failed");
  }
}
