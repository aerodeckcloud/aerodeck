import type { DeploymentStatus } from "./types";

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
