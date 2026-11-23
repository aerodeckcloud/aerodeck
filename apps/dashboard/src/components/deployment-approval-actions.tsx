"use client";

import { Check, Loader2, X } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { approveDeployment, rejectDeployment } from "@/lib/api";
import type { Deployment } from "@/lib/types";

type DeploymentApprovalActionsProps = {
  deploymentId: string;
  size?: "sm" | "default";
  onResolved: (deployment: Deployment) => void;
};

/** Owner approve or reject. The API returns 403 for anyone else, and that message is shown. */
export function DeploymentApprovalActions({
  deploymentId,
  size = "default",
  onResolved,
}: DeploymentApprovalActionsProps) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(action: "approve" | "reject") {
    setError(null);
    setBusy(action);
    try {
      const next =
        action === "approve"
          ? await approveDeployment(deploymentId)
          : await rejectDeployment(deploymentId);
      onResolved(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not update the deployment.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        size={size}
        disabled={busy !== null}
        onClick={() => {
          void run("approve");
        }}
      >
        {busy === "approve" ? <Loader2 className="animate-spin" /> : <Check />}
        Approve
      </Button>
      <Button
        type="button"
        size={size}
        variant="outline"
        disabled={busy !== null}
        onClick={() => {
          void run("reject");
        }}
      >
        {busy === "reject" ? <Loader2 className="animate-spin" /> : <X />}
        Reject
      </Button>
      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
