import type { AddonType, DeploymentBlueprint, DeploymentStatus } from "@/lib/types";

export type DeployRouteStep = "repository" | "build" | "resources" | "variables" | "launch";

export type DeployDraftVariable = {
  key: string;
  value: string;
  secret: boolean;
  redacted: boolean;
};

export type DeployDraftPlan = {
  step: Exclude<DeployRouteStep, "repository">;
  blueprint: DeploymentBlueprint;
  detected_addons: AddonType[];
  selected_addons: AddonType[];
  variables: DeployDraftVariable[];
  extra_env: string;
};

export function deployStepPath(draftId: string, step: DeployDraftPlan["step"]): string {
  if (step === "build") {
    return `/deploy/${draftId}/build`;
  }
  if (step === "resources") {
    return `/deploy/${draftId}/databases`;
  }
  if (step === "variables") {
    return `/deploy/${draftId}/variables`;
  }
  return `/deploy/${draftId}/launch`;
}

/** Variables are their own page only when the scan found env keys (#315). */
export function stepAfterResources(envKeyCount: number): "variables" | "launch" {
  return envKeyCount > 0 ? "variables" : "launch";
}

export function stepBeforeLaunch(envKeyCount: number): "variables" | "resources" {
  return envKeyCount > 0 ? "variables" : "resources";
}

export function deploymentHref(row: {
  id: string;
  status: DeploymentStatus;
  draft_step?: DeployDraftPlan["step"] | null;
}): string {
  if (row.status === "draft") {
    return deployStepPath(row.id, row.draft_step ?? "build");
  }
  return `/deployments/${row.id}`;
}
