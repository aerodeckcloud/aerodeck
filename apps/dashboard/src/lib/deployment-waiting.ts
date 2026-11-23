import type { Deployment, DeploymentStatus } from "./types";

/**
 * True while a deployment is held behind another one and has not started.
 * The control plane keeps such a row `queued` with `held_for_deployment_id` set.
 */
export function isDeploymentWaiting(
  status: DeploymentStatus,
  heldFor?: string | null,
): boolean {
  return Boolean(heldFor) && (status === "queued" || status === "running");
}

/** True once nothing more will happen to the deployment. */
export function isDeploymentFinished(status: DeploymentStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

/**
 * True while a production deploy is stored and an owner has not approved it.
 * The row stays `queued` with `approval_required_at` set and `approved_at` empty.
 */
export function isAwaitingApproval(
  deployment: Pick<Deployment, "status" | "approval_required_at" | "approved_at">,
): boolean {
  return (
    deployment.status === "queued" &&
    Boolean(deployment.approval_required_at) &&
    !deployment.approved_at
  );
}
