"use client";

import { Activity, Loader2, Play, RefreshCw, RotateCw, Square } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { fetchContainerMetrics, runContainerAction } from "@/lib/api";
import { formatInstant } from "@/lib/format";
import type { ContainerAction, ContainerMetrics, RuntimeLogsStatus } from "@/lib/types";

function stateBadge(status: RuntimeLogsStatus) {
  if (status === "running") {
    return <Badge variant="default">Running</Badge>;
  }
  if (status === "exited") {
    return <Badge variant="destructive">Stopped</Badge>;
  }
  if (status === "not_found") {
    return <Badge variant="secondary">Not deployed</Badge>;
  }
  return <Badge variant="outline">Unavailable</Badge>;
}

export function EnvironmentRuntimePanel({
  projectId,
  environmentId,
}: {
  projectId: string;
  environmentId: string;
}) {
  const [open, setOpen] = useState(false);
  const [metrics, setMetrics] = useState<ContainerMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [runningAction, setRunningAction] = useState<ContainerAction | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadMetrics = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMetrics(await fetchContainerMetrics(projectId, environmentId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load container metrics.");
    } finally {
      setLoading(false);
    }
  }, [projectId, environmentId]);

  useEffect(() => {
    if (open) {
      void loadMetrics();
    }
  }, [open, loadMetrics]);

  async function handleAction(action: ContainerAction) {
    setRunningAction(action);
    setError(null);
    try {
      const result = await runContainerAction(projectId, environmentId, action);
      if (!result.ok && result.error) {
        setError(result.error);
      }
      await loadMetrics();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : `Could not ${action} container.`);
    } finally {
      setRunningAction(null);
    }
  }

  return (
    <div className="sm:col-span-2 rounded-lg border border-border/70 bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium">
          <Activity className="size-3.5 text-primary" />
          Container runtime &amp; metrics
          {metrics ? stateBadge(metrics.status) : null}
        </span>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Inspect"}
        </Button>
      </div>

      {open ? (
        <div className="mt-3 flex flex-col gap-3">
          {loading && !metrics ? (
            <p className="text-xs text-muted-foreground">Collecting container metrics…</p>
          ) : metrics ? (
            <>
              <div className="grid gap-2 rounded-md border bg-background p-2.5 text-xs sm:grid-cols-4">
                <div>
                  <p className="text-[11px] text-muted-foreground">CPU</p>
                  <p className="mt-0.5 font-mono font-medium">{metrics.cpu_percent ?? "—"}</p>
                </div>
                <div>
                  <p className="text-[11px] text-muted-foreground">Memory</p>
                  <p className="mt-0.5 font-mono font-medium">
                    {metrics.memory_usage ?? "—"}
                    {metrics.memory_percent ? ` (${metrics.memory_percent})` : ""}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] text-muted-foreground">Started</p>
                  <p className="mt-0.5 font-mono">
                    {metrics.started_at ? formatInstant(metrics.started_at) : "—"}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] text-muted-foreground">Restarts</p>
                  <p className="mt-0.5 font-mono font-medium">{metrics.restart_count}</p>
                </div>
              </div>

              {metrics.error ? (
                <p className="text-xs text-muted-foreground">{metrics.error}</p>
              ) : null}

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={runningAction !== null || metrics.status === "running"}
                  onClick={() => void handleAction("start")}
                >
                  {runningAction === "start" ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <Play className="size-3.5" />
                  )}
                  Start
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={runningAction !== null || metrics.status === "not_found"}
                  onClick={() => void handleAction("restart")}
                >
                  {runningAction === "restart" ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <RotateCw className="size-3.5" />
                  )}
                  Restart
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={runningAction !== null || metrics.status !== "running"}
                  onClick={() => void handleAction("stop")}
                >
                  {runningAction === "stop" ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <Square className="size-3.5" />
                  )}
                  Stop
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={loading || runningAction !== null}
                  onClick={() => void loadMetrics()}
                >
                  <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
                  Refresh
                </Button>
              </div>
            </>
          ) : null}

          {error ? (
            <p className="text-xs text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
