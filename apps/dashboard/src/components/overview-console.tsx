"use client";

import { Activity, Box, FolderGit2, Rocket } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { LinkButton } from "@/components/link-button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { fetchDeployments, fetchProjectPage, fetchSandboxPage } from "@/lib/api";
import type { Deployment, DeploymentStatus, ProjectPage, SandboxPage } from "@/lib/types";
import { cn } from "@/lib/utils";

export function OverviewConsole({ controlPlane = false }: { controlPlane?: boolean }) {
  const [projects, setProjects] = useState<ProjectPage | null>(null);
  const [sandboxes, setSandboxes] = useState<SandboxPage | null>(null);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [error, setError] = useState<string | null>(null);

  const isControlPlane = controlPlane || projects?.source === "control-plane";

  useEffect(() => {
    Promise.all([fetchProjectPage(), fetchSandboxPage()])
      .then(async ([nextProjects, nextSandboxes]) => {
        const nextDeployments = await fetchDeployments(
          controlPlane || nextProjects.source === "control-plane",
        );
        setProjects(nextProjects);
        setSandboxes(nextSandboxes);
        setDeployments(nextDeployments);
      })
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : "Failed to load the console.");
      });
  }, [controlPlane]);

  const sandboxRows = sandboxes?.items ?? [];
  const runningSandboxes = sandboxRows.filter((row) => row.status === "running").length;

  return (
    <div className="mx-auto w-full max-w-5xl">
      <PageHeader
        eyebrow="Overview"
        title="AeroDeck console"
        description={
          isControlPlane
            ? "Projects, sandboxes, and deployments are read by this server from the control plane."
            : "Projects and sandboxes are read by this server from the control plane. Deployments on this page stay local."
        }
        actions={
          <LinkButton href="/deploy">
            <Rocket data-icon="inline-start" />
            New deployment
          </LinkButton>
        }
      />

      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-3">
            <StatCard
              tick="bg-primary"
              icon={FolderGit2}
              label="Projects"
              value={projects?.total ?? 0}
              hint={projects?.source === "control-plane" ? "Live control plane" : "Local seed"}
            />
            <StatCard
              tick="bg-chart-3"
              icon={Box}
              label="Sandboxes"
              value={runningSandboxes}
              hint={
                sandboxes?.source === "control-plane"
                  ? `${sandboxRows.length} total · Live control plane`
                  : `${sandboxRows.length} total · Local seed`
              }
            />
            <StatCard
              tick="bg-runway"
              icon={Activity}
              label="Deployments"
              value={deployments.length}
              hint={isControlPlane ? "Stored by the control plane" : "Created this session"}
            />
          </div>

          <Card className="mt-6">
            <CardHeader className="border-b">
              <CardTitle>Recent deployments</CardTitle>
              <CardDescription>
                {isControlPlane
                  ? "Rows stored by the control plane."
                  : "Empty until you POST a GitHub URL from Deploy."}
              </CardDescription>
            </CardHeader>
            <CardContent className="pt-4">
              {deployments.length === 0 ? (
                <p className="text-sm text-muted-foreground">No deployments yet.</p>
              ) : (
                <ul className="divide-y divide-border">
                  {deployments.slice(0, 5).map((row) => (
                    <li key={row.id} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
                      <div className="min-w-0 flex-1">
                        <Link
                          href={`/deployments/${row.id}`}
                          className="font-heading text-sm font-medium tracking-tight hover:text-primary"
                        >
                          {row.owner}/{row.repo}
                        </Link>
                        <p className="truncate font-mono text-xs text-muted-foreground">{row.id}</p>
                      </div>
                      <span className="flex items-center gap-2 font-mono text-[11px] tracking-[0.14em] text-muted-foreground uppercase">
                        <span
                          className={cn("size-1.5 rounded-full", statusLamp(row.status))}
                          aria-hidden
                        />
                        {row.status}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

function statusLamp(status: DeploymentStatus): string {
  switch (status) {
    case "succeeded":
      return "bg-primary";
    case "running":
      return "bg-primary shadow-[0_0_8px_var(--primary)]";
    case "queued":
      return "bg-runway";
    case "failed":
      return "bg-destructive";
    case "cancelled":
      return "bg-muted-foreground/40";
    case "draft":
      return "bg-runway";
  }
}

function StatCard({
  tick,
  icon: Icon,
  label,
  value,
  hint,
}: {
  tick: string;
  icon: typeof FolderGit2;
  label: string;
  value: number;
  hint: string;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-2">
          <span className={cn("h-4 w-1 rounded-full", tick)} aria-hidden />
          <Icon className="size-4" />
          {label}
        </CardDescription>
        <CardTitle className="font-heading text-4xl tabular-nums tracking-tight">{value}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </CardContent>
    </Card>
  );
}
