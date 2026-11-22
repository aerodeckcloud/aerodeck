"use client";

import { ClipboardList, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { fetchProjectAuditLogs } from "@/lib/api";
import { formatInstant } from "@/lib/format";
import type { AuditEvent, ProjectSource } from "@/lib/types";

export function ProjectAuditSection({
  projectId,
  source,
}: {
  projectId?: string;
  source?: ProjectSource;
}) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId || source !== "control-plane") {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setEvents(await fetchProjectAuditLogs(projectId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load audit log.");
    } finally {
      setLoading(false);
    }
  }, [projectId, source]);

  useEffect(() => {
    void load();
  }, [load]);

  if (source !== "control-plane" || !projectId) {
    return null;
  }

  return (
    <section id="activity" className="mt-8 scroll-mt-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-2 text-lg font-medium">
          <ClipboardList className="size-4 text-primary" />
          Activity &amp; audit log
        </h2>
        <Button type="button" size="sm" variant="outline" disabled={loading} onClick={() => void load()}>
          <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
          Refresh
        </Button>
      </div>

      {error ? (
        <p className="mb-3 text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      <Card>
        <CardContent className="divide-y divide-border pt-0">
          {loading && events.length === 0 ? (
            <p className="py-6 text-sm text-muted-foreground">Loading activity…</p>
          ) : events.length === 0 ? (
            <p className="py-6 text-sm text-muted-foreground">No recorded project activity yet.</p>
          ) : (
            events.map((evt) => (
              <div key={evt.id} className="flex flex-col gap-1 py-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="font-mono text-[11px]">
                      {evt.action}
                    </Badge>
                    <span className="font-mono text-xs text-muted-foreground">{evt.actor}</span>
                  </div>
                  <span className="font-mono text-xs text-muted-foreground">
                    {formatInstant(evt.created_at)}
                  </span>
                </div>
                <p className="text-xs text-foreground">{evt.summary}</p>
              </div>
            ))
          )}
        </CardContent>
      </Card>
    </section>
  );
}
