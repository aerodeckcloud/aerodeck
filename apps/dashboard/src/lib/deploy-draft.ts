import type { AddonType, DeploymentBlueprint, DeploymentStatus } from "@/lib/types";

export type DeployRouteStep = "repository" | "build" | "resources" | "launch";

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
  return `/deploy/${draftId}/launch`;
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
