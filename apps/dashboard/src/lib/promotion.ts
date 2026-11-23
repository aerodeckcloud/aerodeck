import type { Deployment, EnvironmentOption } from "./types";

/** Only a succeeded deployment with a recorded commit kept an image to promote. */
export function canPromote(deployment: Pick<Deployment, "status" | "git_sha">): boolean {
  return deployment.status === "succeeded" && Boolean(deployment.git_sha);
}

/** Other environments of the deployment's project. Empty when it is not linked to one. */
export function promotionTargets(
  deployment: Pick<Deployment, "project_id" | "environment_id">,
  environments: EnvironmentOption[],
): EnvironmentOption[] {
  if (!deployment.project_id || !deployment.environment_id) {
    return [];
  }
  return environments.filter(
    (env) => env.project_id === deployment.project_id && env.id !== deployment.environment_id,
  );
}

export function promotionTargetLabel(env: Pick<EnvironmentOption, "slug" | "kind">): string {
  return env.kind === "prod" ? `${env.slug} (production, owner only)` : `${env.slug} (${env.kind})`;
}

export type PromotionRefusal = { status: number; detail: string; code: string };

const REFUSALS: Record<number, PromotionRefusal> = {
  403: {
    status: 403,
    detail: "Promoting needs the developer role, and the owner role for a production environment.",
    code: "forbidden",
  },
  409: {
    status: 409,
    detail: "The target environment is switching containers for another deployment. Try again shortly.",
    code: "target_busy",
  },
  422: {
    status: 422,
    detail:
      "This deployment cannot be promoted there. Its image may no longer be on the host; deploy the commit instead.",
    code: "not_promotable",
  },
  503: {
    status: 503,
    detail: "The job queue is unavailable. Try again shortly.",
    code: "queue_unavailable",
  },
};

/** What the console tells the user when the control plane refuses a promotion. */
export function promotionRefusal(upstreamStatus: number | undefined): PromotionRefusal | null {
  return upstreamStatus === undefined ? null : (REFUSALS[upstreamStatus] ?? null);
}
