import { connection } from "next/server";

import { NewDeploymentForm } from "@/components/new-deployment-form";
import { PageHeader } from "@/components/page-header";
import { readControlPlaneConfig } from "@/lib/control-plane";

export default async function DeployPage() {
  await connection();
  const controlPlane = readControlPlaneConfig() !== null;
  return (
    <div className="mx-auto w-full max-w-xl">
      <PageHeader
        eyebrow="Deploy"
        title="New Deployment"
        description={
          controlPlane
            ? "Start with the GitHub repository. After the scan, confirm the build. Environment, addons, and variables follow."
            : "Point AeroDeck at a GitHub repository. The request stays on this Next.js app — the mock API pretends to be the control plane."
        }
      />
      <NewDeploymentForm controlPlane={controlPlane} />
    </div>
  );
}
