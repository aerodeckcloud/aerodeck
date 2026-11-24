import type { AddonType, DeploymentBlueprint, DeploymentStatus } from "@/lib/types";

export type DeployRouteStep = "repository" | "build" | "resources" | "variables" | "target" | "launch";

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
  if (step === "target") {
    return `/deploy/${draftId}/target`;
  }
  return `/deploy/${draftId}/launch`;
}

/** Variables are their own page only when the scan found env keys (#315). */
export function stepAfterResources(envKeyCount: number): "variables" | "target" {
  return envKeyCount > 0 ? "variables" : "target";
}

/** The target page follows variables when the scan found keys, and databases otherwise. */
export function stepBeforeTarget(envKeyCount: number): "variables" | "resources" {
  return envKeyCount > 0 ? "variables" : "resources";
}

export function countLaunchVariables(
  fieldValues: Record<string, string>,
  redactedKeys: Iterable<string>,
  extraKeys: Iterable<string>,
): number {
  const keys = new Set<string>();
  for (const [key, value] of Object.entries(fieldValues)) {
    if (value.trim().length > 0) {
      keys.add(key);
    }
  }
  for (const key of redactedKeys) {
    keys.add(key);
  }
  for (const key of extraKeys) {
    keys.add(key);
  }
  return keys.size;
}

export type LaunchReview = {
  repository: string;
  buildSource: string;
  addons: string;
  variableCount: number;
  environment: string;
};

/** Summary of the scan and the operator's edits before provision (#316). */
export function launchReview(input: {
  repository: string;
  source: string;
  service: string | null;
  port: number;
  selectedAddons: readonly string[];
  variableCount: number;
  environmentLabel: string | null;
}): LaunchReview {
  const service = input.service?.trim();
  const buildSource = service
    ? `${input.source} · ${service} · port ${input.port}`
    : `${input.source} · port ${input.port}`;
  return {
    repository: input.repository,
    buildSource,
    addons: input.selectedAddons.length > 0 ? input.selectedAddons.join(", ") : "None",
    variableCount: input.variableCount,
    environment: input.environmentLabel ?? "No environment selected",
  };
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
