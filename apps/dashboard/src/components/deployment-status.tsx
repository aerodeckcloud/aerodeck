"use client";

import { Ban, CheckCircle2, ExternalLink, Loader2, Rocket, XCircle } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { DeploymentEnvironmentLink } from "@/components/deployment-environment-link";
import { LinkButton } from "@/components/link-button";
import { ProvisionJobPanel } from "@/components/provision-job-panel";
import { RuntimeLogsPanel } from "@/components/runtime-logs-panel";
import { Terminal } from "@/components/terminal";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { formatInstant } from "@/lib/format";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  cancelDeployment,
  deploymentLogsUrl,
  fetchDeployment,
  fetchProjectDetail,
  markDeploymentFailed,
  markDeploymentSucceeded,
} from "@/lib/api";
import { isDeploymentFinished, isDeploymentWaiting } from "@/lib/deployment-waiting";
import { useEnvironmentCatalog } from "@/lib/use-environment-catalog";
import type { Deployment, DeploymentStatus, ProjectSource } from "@/lib/types";

type DeploymentStatusViewProps = {
  deployment: Deployment;
  logSource: ProjectSource;
};

type TerminalOutcome = "succeeded" | "failed";

/** How often a waiting deployment is re-read to see whether it has started. */
const WAITING_POLL_MS = 5_000;

function logSourceCopy(source: ProjectSource): { sseLabel: string; targetLabel: string } {
  if (source === "control-plane") {
    return {
      sseLabel: "Control-plane SSE for ",
      targetLabel: "DigitalOcean · control plane",
    };
  }
  return {
    sseLabel: "Mock SSE for ",
    targetLabel: "DigitalOcean · mock provisioner",
  };
}

function statusBadge(status: DeploymentStatus, waiting: boolean) {
  if (waiting) {
    return (
      <Badge variant="secondary">
        <Loader2 />
        Waiting
      </Badge>
    );
  }
  if (status === "cancelled") {
    return (
      <Badge variant="secondary">
        <Ban />
        Cancelled
      </Badge>
    );
  }
  if (status === "failed") {
    return (
      <Badge variant="destructive">
        <XCircle />
        Failed
      </Badge>
    );
  }
  if (status === "succeeded") {
    return (
      <Badge variant="default">
        <CheckCircle2 />
        Succeeded
      </Badge>
    );
  }
  return (
    <Badge variant="secondary">
      <Loader2 className="animate-spin" />
      Running
    </Badge>
  );
}

export function DeploymentStatusView({ deployment, logSource }: DeploymentStatusViewProps) {
  const [status, setStatus] = useState<DeploymentStatus>(deployment.status);
  const [heldFor, setHeldFor] = useState<string | null>(
    deployment.held_for_deployment_id ?? null,
  );
  const [provisionJobId, setProvisionJobId] = useState<string | null>(
    deployment.provision_job_id ?? null,
  );
  const [liveHostname, setLiveHostname] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const hasLinkedEnv = Boolean(deployment.project_id && deployment.environment_id);
  const environments = useEnvironmentCatalog(logSource === "control-plane" && hasLinkedEnv);
  const copy = logSourceCopy(logSource);
  const waiting = logSource === "control-plane" && isDeploymentWaiting(status, heldFor);

  const applyStored = useCallback((stored: Deployment) => {
    setStatus(stored.status);
    setHeldFor(stored.held_for_deployment_id ?? null);
    setProvisionJobId(stored.provision_job_id ?? null);
  }, []);

  // A waiting deployment starts on its own, so re-read it until it does.
  useEffect(() => {
    if (!waiting) {
      return;
    }
    let stopped = false;
    const timer = setInterval(() => {
      void fetchDeployment(deployment.id, true)
        .then((stored) => {
          if (!stopped) {
            applyStored(stored);
          }
        })
        .catch(() => {
          // Best-effort: the next tick tries again.
        });
    }, WAITING_POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [waiting, deployment.id, applyStored]);

  useEffect(() => {
    if (logSource !== "control-plane" || !deployment.project_id || !deployment.environment_id) {
      return;
    }
    let cancelled = false;
    void fetchProjectDetail(deployment.project_id)
      .then((detail) => {
        if (cancelled) {
          return;
        }
        const match = detail.routes.find(
          (route) => route.environment_id === deployment.environment_id && route.enabled,
        );
        setLiveHostname(match?.hostname ?? null);
      })
      .catch(() => {
        // Non-blocking: live app link is best-effort.
      });
    return () => {
      cancelled = true;
    };
  }, [logSource, deployment.project_id, deployment.environment_id, status]);

  /** False when the deployment has no outcome to record: still waiting, or already finished. */
  async function hasStarted(): Promise<boolean> {
    if (!waiting) {
      return true;
    }
    // The page may be behind: the deployment can start between two polls.
    try {
      const stored = await fetchDeployment(deployment.id, true);
      applyStored(stored);
      return (
        !isDeploymentFinished(stored.status) &&
        !isDeploymentWaiting(stored.status, stored.held_for_deployment_id)
      );
    } catch {
      return false;
    }
  }

  function recordOutcome(outcome: TerminalOutcome) {
    if (isDeploymentFinished(status)) {
      return;
    }
    if (logSource !== "control-plane") {
      setStatus(outcome);
      return;
    }
    const mark = outcome === "succeeded" ? markDeploymentSucceeded : markDeploymentFailed;
    void hasStarted()
      .then((started) => (started ? mark(deployment.id).then(applyStored) : undefined))
      .catch((cause: unknown) => {
        // Stream already showed provision ended without Done; keep the badge honest
        // even if the status POST fails (worker may already have written failed).
        if (outcome === "failed") {
          setStatus("failed");
        }
        setError(cause instanceof Error ? cause.message : "Could not record the deployment status.");
      });
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-medium tracking-[0.16em] text-primary uppercase">
            Live
          </p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight md:text-3xl">
            Deployment Status
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {copy.sseLabel}
            <span className="font-mono text-foreground">
              {deployment.owner}/{deployment.repo}
            </span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {liveHostname ? (
            <a
              href={`https://${liveHostname}`}
              target="_blank"
              rel="noopener noreferrer"
              className={buttonVariants({ variant: "default" })}
            >
              Open Live App
              <ExternalLink data-icon="inline-end" />
            </a>
          ) : null}
          {logSource === "control-plane" && (status === "queued" || status === "running") ? (
            <Button
              type="button"
              variant="outline"
              disabled={cancelling}
              onClick={() => {
                setCancelling(true);
                setError(null);
                void cancelDeployment(deployment.id)
                  .then(applyStored)
                  .catch((cause: unknown) => {
                    setError(cause instanceof Error ? cause.message : "Could not cancel the deployment.");
                  })
                  .finally(() => setCancelling(false));
              }}
            >
              {cancelling ? <Loader2 className="animate-spin" /> : <Ban />}
              Cancel
            </Button>
          ) : null}
          <LinkButton href="/deploy" variant="outline">
            <Rocket data-icon="inline-start" />
            New deployment
          </LinkButton>
        </div>
      </div>

      <Card>
        <CardHeader className="border-b">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="font-mono text-sm">{deployment.id}</CardTitle>
              <CardDescription className="mt-1 break-all">
                {deployment.repo_url}
              </CardDescription>
            </div>
            {statusBadge(status, waiting)}
          </div>
        </CardHeader>
        <CardContent className="grid gap-3 pt-4 text-sm sm:grid-cols-2">
          <div>
            <p className="text-xs text-muted-foreground">Started</p>
            <p className="mt-0.5 font-mono text-xs">
              {formatInstant(deployment.started_at)}
            </p>
          </div>
          <div>
            <p className="text-xs text-muted-foreground">Target</p>
            <p className="mt-0.5">{copy.targetLabel}</p>
          </div>
          {deployment.project_id && deployment.environment_id ? (
            <div className="sm:col-span-2">
              <p className="text-xs text-muted-foreground">Environment</p>
              <DeploymentEnvironmentLink
                deployment={deployment}
                environments={environments}
                className="mt-0.5 text-sm text-muted-foreground"
              />
            </div>
          ) : null}
          {liveHostname ? (
            <div className="sm:col-span-2">
              <p className="text-xs text-muted-foreground">Live URL</p>
              <a
                href={`https://${liveHostname}`}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-0.5 inline-flex items-center gap-1 font-mono text-xs text-primary underline-offset-4 hover:underline"
              >
                https://{liveHostname}
                <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          ) : null}
        </CardContent>
      </Card>

      {waiting && heldFor ? (
        <p className="text-sm text-muted-foreground" role="status">
          Waiting for deployment{" "}
          <Link
            href={`/deployments/${heldFor}`}
            className="font-mono text-foreground underline-offset-4 hover:underline"
          >
            {heldFor.slice(0, 8)}
          </Link>{" "}
          to finish. This one starts on its own after that.
        </p>
      ) : null}

      {logSource === "control-plane" && provisionJobId ? (
        <ProvisionJobPanel jobId={provisionJobId} deploymentStatus={status} />
      ) : null}

      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}
      {/* The stream opened while waiting may have ended before the deployment
          started. Changing the key opens a fresh one that replays from the top. */}
      <Terminal
        key={waiting ? "waiting" : "started"}
        streamUrl={deploymentLogsUrl(deployment.id, logSource)}
        onComplete={() => recordOutcome("succeeded")}
        onFailed={() => recordOutcome("failed")}
      />

      {logSource === "control-plane" && hasLinkedEnv ? (
        <RuntimeLogsPanel deploymentId={deployment.id} />
      ) : null}
    </div>
  );
}
