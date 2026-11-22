"use client";

import { RefreshCw, Terminal as TerminalIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { fetchDeploymentRuntimeLogs } from "@/lib/api";
import type { ContainerRuntimeLogs, RuntimeLogsStatus } from "@/lib/types";

const TAIL_OPTIONS = [100, 200, 500] as const;

function containerStatusBadge(status: RuntimeLogsStatus) {
  if (status === "running") {
    return <Badge variant="default">Running</Badge>;
  }
  if (status === "exited") {
    return <Badge variant="destructive">Exited</Badge>;
  }
  if (status === "not_found") {
    return <Badge variant="secondary">Not started</Badge>;
  }
  return <Badge variant="outline">Unavailable</Badge>;
}

export function RuntimeLogsPanel({ deploymentId }: { deploymentId: string }) {
  const [tail, setTail] = useState<number>(200);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [loading, setLoading] = useState(false);
  const [logs, setLogs] = useState<ContainerRuntimeLogs | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadLogs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await fetchDeploymentRuntimeLogs(deploymentId, tail);
      setLogs(next);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "Could not load runtime logs.");
    } finally {
      setLoading(false);
    }
  }, [deploymentId, tail]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  useEffect(() => {
    if (!autoRefresh) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadLogs();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadLogs]);

  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <TerminalIcon className="h-4 w-4 text-primary" />
              Runtime container logs
            </CardTitle>
            <CardDescription className="mt-1 font-mono text-xs">
              {logs?.container ? logs.container : "Active environment container"}
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {logs ? containerStatusBadge(logs.status) : null}
            <select
              aria-label="Log line limit"
              value={tail}
              onChange={(event) => setTail(Number(event.target.value))}
              className="h-8 rounded-md border border-input bg-background px-2 text-xs"
            >
              {TAIL_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  Last {option} lines
                </option>
              ))}
            </select>
            <Button
              type="button"
              size="sm"
              variant={autoRefresh ? "default" : "outline"}
              onClick={() => setAutoRefresh((prev) => !prev)}
            >
              {autoRefresh ? "Auto: On (5s)" : "Auto: Off"}
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={loading}
              onClick={() => void loadLogs()}
            >
              <RefreshCw className={loading ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} />
              Refresh
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        {error ? (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : logs?.error ? (
          <p className="text-sm text-muted-foreground">{logs.error}</p>
        ) : !logs ? (
          <p className="text-sm text-muted-foreground">Loading container logs…</p>
        ) : logs.lines.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {logs.status === "not_found"
              ? "No running container found for this environment yet."
              : "Container has not emitted any log lines yet."}
          </p>
        ) : (
          <pre className="max-h-96 overflow-auto rounded-md bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-100">
            {logs.lines.join("\n")}
          </pre>
        )}
      </CardContent>
    </Card>
  );
}
