import { connection } from "next/server";

import { NewDeploymentForm } from "@/components/new-deployment-form";
import { PageHeader } from "@/components/page-header";
import { PRODUCT_NAME } from "@/lib/brand";
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
            ? "Start with the GitHub repository. After the scan, confirm the build, then the databases it found. Environment and variables follow."
            : `Point ${PRODUCT_NAME} at a GitHub repository. The request stays on this Next.js app — the mock API pretends to be the control plane.`
        }
      />
      <NewDeploymentForm controlPlane={controlPlane} />
    </div>
  );
}
