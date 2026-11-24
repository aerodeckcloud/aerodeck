import { connection } from "next/server";
import { redirect } from "next/navigation";

import { NewDeploymentForm } from "@/components/new-deployment-form";
import { PageHeader } from "@/components/page-header";
import { readControlPlaneConfig } from "@/lib/control-plane";
import type { DeployRouteStep } from "@/lib/deploy-draft";

type DraftStep = Exclude<DeployRouteStep, "repository">;

export function draftStepPage(routeStep: DraftStep, description: string) {
  return async function DeployDraftStepPage({ params }: { params: Promise<{ draftId: string }> }) {
    await connection();
    if (readControlPlaneConfig() === null) {
      redirect("/deploy");
    }
    const { draftId } = await params;
    return (
      <div className="mx-auto w-full max-w-xl">
        <PageHeader eyebrow="Deploy" title="New Deployment" description={description} />
        <NewDeploymentForm controlPlane routeStep={routeStep} draftId={draftId} />
      </div>
    );
  };
}
