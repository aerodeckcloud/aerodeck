import type { AddonType, DeploymentBlueprint, DeploymentStatus } from "@/lib/types";

export type DeployRouteStep = "repository" | "build" | "security" | "resources" | "variables" | "target" | "launch";

export type SecurityOutcome = "clear" | "findings" | "incomplete" | "no_manifest";

export type SecurityFinding = {
  severity: "critical" | "high" | "medium" | "low" | "unknown";
  package: string;
  advisory_id: string;
  summary: string;
};

export type SecurityReport = {
  outcome: SecurityOutcome;
  findings: SecurityFinding[];
  acknowledgement: "findings" | "incomplete" | null;
};

export function securityNeedsAcknowledgement(outcome: SecurityOutcome): boolean {
  return outcome === "findings" || outcome === "incomplete";
}

export function launchReturnsToSecurity(detail: string): boolean {
  return (
    detail === "Acknowledge the dependency findings before deploying." ||
    detail === "The dependency scan did not complete." ||
    detail === "Run the dependency scan before deploying." ||
    detail === "The dependency scan is out of date. Run it again before deploying."
  );
}

export function parseSecurityReport(value: unknown): SecurityReport {
  if (!value || typeof value !== "object") {
    throw new Error("The dependency scan did not complete.");
  }
  const row = value as Record<string, unknown>;
  const outcome = row.outcome;
  if (
    outcome !== "clear" &&
    outcome !== "findings" &&
    outcome !== "incomplete" &&
    outcome !== "no_manifest"
  ) {
    throw new Error("The dependency scan did not complete.");
  }
  const findings: SecurityFinding[] = [];
  if (Array.isArray(row.findings)) {
    for (const item of row.findings) {
      if (!item || typeof item !== "object") {
        continue;
      }
      const finding = item as Record<string, unknown>;
      if (
        typeof finding.package !== "string" ||
        typeof finding.advisory_id !== "string" ||
        typeof finding.summary !== "string"
      ) {
        continue;
      }
      findings.push({
        severity:
          finding.severity === "critical" ||
          finding.severity === "high" ||
          finding.severity === "medium" ||
          finding.severity === "low"
            ? finding.severity
            : "unknown",
        package: finding.package,
        advisory_id: finding.advisory_id,
        summary: finding.summary.replace(/\s+/g, " ").slice(0, 160),
      });
    }
  }
  return {
    outcome,
    findings,
    acknowledgement:
      row.acknowledgement === "findings" || row.acknowledgement === "incomplete"
        ? row.acknowledgement
        : null,
  };
}

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
  if (step === "security") {
    return `/deploy/${draftId}/security`;
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
