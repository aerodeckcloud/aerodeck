"use client";

import {
  ArrowUpFromLine,
  Ban,
  CheckCircle2,
  GitCommitHorizontal,
  History,
  Loader2,
  RefreshCw,
  RotateCcw,
  XCircle,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  deployLatestDeployment,
  fetchEnvironmentVars,
  fetchProjectDeployments,
  promoteDeployment,
  redeployDeployment,
  rollbackDeployment,
} from "@/lib/api";
import { isDeploymentWaiting } from "@/lib/deployment-waiting";
import { formatDuration, formatInstant } from "@/lib/format";
import { nativeSelectClassName } from "@/lib/native-select";
import { canPromote, promotionTargetLabel, promotionTargets } from "@/lib/promotion";
import { rollbackConfirmMessage, rollbackRestoreWarning } from "@/lib/rollback-variables";
import type { Deployment, DeploymentStatus, EnvironmentOption, ProjectSource } from "@/lib/types";

function statusBadge(status: DeploymentStatus, heldFor?: string | null) {
  if (isDeploymentWaiting(status, heldFor)) {
    return (
      <Badge variant="secondary">
        <Loader2 className="size-3" />
        Waiting
      </Badge>
    );
  }
  if (status === "succeeded") {
    return (
      <Badge variant="default">
        <CheckCircle2 className="size-3" />
        Succeeded
      </Badge>
    );
  }
  if (status === "cancelled") {
    return (
      <Badge variant="secondary">
        <Ban className="size-3" />
        Cancelled
      </Badge>
    );
  }
  if (status === "failed") {
    return (
      <Badge variant="destructive">
        <XCircle className="size-3" />
        Failed
      </Badge>
    );
  }
  return (
    <Badge variant="secondary">
      <Loader2 className="size-3 animate-spin" />
      Running
    </Badge>
  );
}

export function ProjectDeploymentsSection({
  projectId,
  environments,
  source,
}: {
  projectId?: string;
  environments: EnvironmentOption[];
  source?: ProjectSource;
}) {
  const router = useRouter();
  const [selectedEnv, setSelectedEnv] = useState<string>("");
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Selected promote target per succeeded deployment id. */
  const [promoteTargetById, setPromoteTargetById] = useState<Record<string, string>>({});


  const load = useCallback(async () => {
    if (!projectId || source !== "control-plane") {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const items = await fetchProjectDeployments(projectId, selectedEnv || undefined);
      setDeployments(items);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load deployment history.");
    } finally {
      setLoading(false);
    }
  }, [projectId, source, selectedEnv]);

  useEffect(() => {
    void load();
  }, [load]);

  if (source !== "control-plane" || !projectId) {
    return null;
  }

  const envNameById = new Map(environments.map((env) => [env.id, env.slug]));

  async function confirmRollback(dep: Deployment): Promise<boolean> {
    let currentNames: string[] | null = [];
    if (dep.snapshot_variable_names != null && dep.project_id && dep.environment_id) {
      try {
        const vars = await fetchEnvironmentVars(dep.project_id, dep.environment_id);
        currentNames = vars.map((row) => row.key);
      } catch {
        // Do not treat a failed load as an empty environment (false "Add" list).
        currentNames = null;
      }
    }
    return window.confirm(
      rollbackConfirmMessage({
        snapshotNames: dep.snapshot_variable_names,
        currentNames,
      }),
    );
  }

  async function handleAction(
    deploymentId: string,
    action: "redeploy" | "deploy-latest" | "rollback" | "promote",
  ) {
    setError(null);
    let rollbackSource: Deployment | null = null;
    if (action === "rollback") {
      const dep = deployments.find((row) => row.id === deploymentId);
      if (!dep) {
        return;
      }
      if (!(await confirmRollback(dep))) {
        return;
      }
      rollbackSource = dep;
    }
    setBusyId(`${deploymentId}:${action}`);
    try {
      let next: Deployment;
      if (action === "redeploy") {
        next = await redeployDeployment(deploymentId);
      } else if (action === "deploy-latest") {
        next = await deployLatestDeployment(deploymentId);
      } else if (action === "rollback") {
        next = await rollbackDeployment(deploymentId, { restoreVariables: true });
        const warning = rollbackRestoreWarning({
          sourceNames: rollbackSource?.snapshot_variable_names,
          variablesRestored: next.variables_restored,
          heldForDeploymentId: next.held_for_deployment_id,
        });
        if (warning) {
          window.alert(warning);
        }
      } else {
        const targetId = promoteTargetById[deploymentId];
        if (!targetId) {
          setError("Choose an environment to promote into.");
          setBusyId(null);
          return;
        }
        next = await promoteDeployment(deploymentId, targetId);
      }
      router.push(`/deployments/${next.id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `Could not ${action} deployment.`);
      setBusyId(null);
    }
  }

  return (
    <section id="deployments" className="mt-8 scroll-mt-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-2 text-lg font-medium">
          <History className="size-4 text-primary" />
          Deployment history
        </h2>
        <div className="flex items-center gap-2">
          {environments.length > 1 ? (
            <select
              aria-label="Filter by environment"
              value={selectedEnv}
              onChange={(event) => setSelectedEnv(event.target.value)}
              className="h-8 rounded-md border border-input bg-background px-2 text-xs"
            >
              <option value="">All environments</option>
              {environments.map((env) => (
                <option key={env.id} value={env.id}>
                  {env.slug}
                </option>
              ))}
            </select>
          ) : null}
          <Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => void load()}>
            <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
            Refresh
          </Button>
        </div>
      </div>

      {error ? (
        <p className="mb-3 text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      <Card>
        <CardContent className="divide-y divide-border pt-0">
          {loading && deployments.length === 0 ? (
            <p className="py-6 text-sm text-muted-foreground">Loading deployments…</p>
          ) : deployments.length === 0 ? (
            <p className="py-6 text-sm text-muted-foreground">
              No deployments recorded for this project yet.
            </p>
          ) : (
            deployments.map((dep) => {
              const envSlug = dep.environment_id ? envNameById.get(dep.environment_id) : null;
              const duration = formatDuration(dep.started_at, dep.finished_at);
              const targets = canPromote(dep) ? promotionTargets(dep, environments) : [];
              const promoteTarget = promoteTargetById[dep.id] ?? "";
              return (
                <div
                  key={dep.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3 text-sm"
                >
                  <div className="flex flex-col gap-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link
                        href={`/deployments/${dep.id}`}
                        className="font-mono text-xs font-medium text-primary hover:underline"
                      >
                        {dep.id.slice(0, 8)}
                      </Link>
                      {statusBadge(dep.status, dep.held_for_deployment_id)}
                      {envSlug ? <Badge variant="outline">{envSlug}</Badge> : null}
                    </div>
                    <p className="font-mono text-xs text-muted-foreground">
                      {dep.owner}/{dep.repo}
                      {dep.git_sha ? ` · ${dep.git_sha.slice(0, 7)}` : ""}
                      {dep.trigger ? ` · ${dep.trigger}` : ""}
                      {` · ${formatInstant(dep.started_at)}`}
                      {duration ? ` · ${duration}` : ""}
                    </p>
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={busyId !== null || !dep.git_sha}
                      title={dep.git_sha ? "Repeat this commit" : "Repeat needs a recorded commit"}
                      onClick={() => void handleAction(dep.id, "redeploy")}
                    >
                      {busyId === `${dep.id}:redeploy` ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <RefreshCw className="size-3.5" />
                      )}
                      Redeploy
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={busyId !== null}
                      title="Deploy the repository default branch"
                      onClick={() => void handleAction(dep.id, "deploy-latest")}
                    >
                      {busyId === `${dep.id}:deploy-latest` ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <GitCommitHorizontal className="size-3.5" />
                      )}
                      Deploy latest
                    </Button>
                    {dep.status === "succeeded" ? (
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={busyId !== null}
                        onClick={() => void handleAction(dep.id, "rollback")}
                      >
                        {busyId === `${dep.id}:rollback` ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <RotateCcw className="size-3.5" />
                        )}
                        Rollback
                      </Button>
                    ) : null}
                    {targets.length > 0 ? (
                      <>
                        <select
                          aria-label={`Promote ${dep.id.slice(0, 8)} to environment`}
                          value={promoteTarget}
                          disabled={busyId !== null}
                          onChange={(event) =>
                            setPromoteTargetById((current) => ({
                              ...current,
                              [dep.id]: event.target.value,
                            }))
                          }
                          className={nativeSelectClassName("h-8 max-w-48 text-xs")}
                        >
                          <option value="">Promote to…</option>
                          {targets.map((env) => (
                            <option key={env.id} value={env.id}>
                              {promotionTargetLabel(env)}
                            </option>
                          ))}
                        </select>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={busyId !== null || !promoteTarget}
                          title="Run this image in another environment"
                          onClick={() => void handleAction(dep.id, "promote")}
                        >
                          {busyId === `${dep.id}:promote` ? (
                            <Loader2 className="size-3.5 animate-spin" />
                          ) : (
                            <ArrowUpFromLine className="size-3.5" />
                          )}
                          Promote
                        </Button>
                      </>
                    ) : null}
                  </div>
                </div>
              );
            })
          )}
        </CardContent>
      </Card>
    </section>
  );
}
