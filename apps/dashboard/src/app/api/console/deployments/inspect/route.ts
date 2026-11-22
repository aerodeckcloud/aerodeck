import { connection, NextResponse } from "next/server";

import {
  ControlPlaneError,
  inspectControlPlaneDeploymentRepo,
  readControlPlaneConfig,
} from "@/lib/control-plane";
import { jsonError } from "@/lib/mock-http";
import { requireSessionUser } from "@/lib/session";

export async function POST(request: Request) {
  await connection();

  const user = await requireSessionUser();
  if (user instanceof NextResponse) {
    return user;
  }

  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, "Deployments stay on the mock API.", "not_found");
  }

  let payload: { repo_url?: unknown };
  try {
    payload = (await request.json()) as { repo_url?: unknown };
  } catch {
    return jsonError(400, "Request body must be JSON.", "invalid_json");
  }
  if (typeof payload.repo_url !== "string" || payload.repo_url.trim() === "") {
    return jsonError(400, "repo_url is required.", "missing_repo_url");
  }

  try {
    const blueprint = await inspectControlPlaneDeploymentRepo(config, payload.repo_url.trim());
    return NextResponse.json(blueprint);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 422) {
      return jsonError(400, "Enter a GitHub URL like https://github.com/org/repo", "invalid_github_url");
    }
    const detail = cause instanceof ControlPlaneError ? cause.message : "inspect failed";
    console.error("event=console_deployment_inspect_failed detail=%s", detail);
    return jsonError(502, "Could not inspect GitHub repository.", "inspect_failed");
  }
}
