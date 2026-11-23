import { NextResponse } from "next/server";

import {
  approveControlPlaneDeployment,
  ControlPlaneError,
  isControlPlaneUuid,
  readControlPlaneConfig,
  rejectControlPlaneDeployment,
} from "@/lib/control-plane";
import { jsonControlPlaneError, jsonError } from "@/lib/mock-http";

const DECISION = {
  approve: {
    missing: "Approval requires the control plane.",
    forbidden: "Only an owner can approve this deployment.",
    event: "console_deployment_approve_failed",
    call: approveControlPlaneDeployment,
  },
  reject: {
    missing: "Rejection requires the control plane.",
    forbidden: "Only an owner can reject this deployment.",
    event: "console_deployment_reject_failed",
    call: rejectControlPlaneDeployment,
  },
} as const;

/** Shared POST body for console approve and reject. The route still checks the session. */
export async function postDeploymentOwnerDecision(
  id: string,
  decision: "approve" | "reject",
  userId: string,
): Promise<NextResponse> {
  const chosen = DECISION[decision];
  const config = readControlPlaneConfig();
  if (!config) {
    return jsonError(404, chosen.missing, "not_found");
  }
  if (!isControlPlaneUuid(id)) {
    return jsonError(400, "Deployment id must be a UUID.", "invalid_deployment_id");
  }
  try {
    const deployment = await chosen.call(config, id, { actorUserId: userId });
    return NextResponse.json(deployment);
  } catch (cause) {
    if (cause instanceof ControlPlaneError && cause.status === 403) {
      return jsonError(403, chosen.forbidden, "forbidden");
    }
    if (cause instanceof ControlPlaneError && cause.status === 409) {
      return jsonError(409, cause.message, "conflict");
    }
    return jsonControlPlaneError(cause, chosen.event);
  }
}
