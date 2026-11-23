import { connection } from "next/server";
import { redirect } from "next/navigation";

import { NewDeploymentForm } from "@/components/new-deployment-form";
import { PageHeader } from "@/components/page-header";
import { readControlPlaneConfig } from "@/lib/control-plane";

export default async function DeployLaunchPage({ params }: { params: Promise<{ draftId: string }> }) {
  await connection();
  if (readControlPlaneConfig() === null) {
    redirect("/deploy");
  }
  const { draftId } = await params;
  return (
    <div className="mx-auto w-full max-w-xl">
      <PageHeader
        eyebrow="Deploy"
        title="New Deployment"
        description="Choose the environment and launch. Provision starts from this page."
      />
      <NewDeploymentForm controlPlane routeStep="launch" draftId={draftId} />
    </div>
  );
}
