"use client";

import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { NewEnvironmentForm } from "@/components/new-environment-form";
import { NewProjectForm } from "@/components/new-project-form";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { fetchDeployments, fetchEnvironments, fetchProjectPage } from "@/lib/api";
import { environmentsForProject, mergeEnvironmentsById } from "@/lib/environment-list";
import { projectDetailPath } from "@/lib/project-detail-path";
import type { Deployment, EnvironmentOption, Project, ProjectPage } from "@/lib/types";

export default function ProjectsPage() {
  const [page, setPage] = useState<ProjectPage | null>(null);
  const [environments, setEnvironments] = useState<EnvironmentOption[]>([]);
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [environmentsWarning, setEnvironmentsWarning] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchProjectPage()
      .then(async (nextPage) => {
        if (cancelled) {
          return;
        }
        setPage(nextPage);
        setError(null);
        if (nextPage.source !== "control-plane") {
          setEnvironments([]);
          setDeployments([]);
          setEnvironmentsWarning(null);
          return;
        }
        void fetchDeployments(true)
          .then((items) => {
            if (!cancelled) {
              setDeployments(items);
            }
          })
          .catch(() => {});
        try {
          const catalog = await fetchEnvironments();
          if (!cancelled) {
            setEnvironments(catalog.items);
            setEnvironmentsWarning(null);
          }
        } catch (cause: unknown) {
          if (!cancelled) {
            setEnvironments([]);
            setEnvironmentsWarning(
              cause instanceof Error ? cause.message : "Could not load environments.",
            );
          }
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Failed to load projects.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function handleProjectCreated(project: Project) {
    setPage((current) => {
      if (!current || current.source !== "control-plane") {
        return current;
      }
      const alreadyListed = current.items.some((row) => row.id === project.id);
      const items = [project, ...current.items.filter((row) => row.id !== project.id)];
      return {
        ...current,
        items,
        total: alreadyListed ? current.total : current.total + 1,
      };
    });
  }

  function handleEnvironmentCreated(environment: EnvironmentOption) {
    setEnvironments((current) => mergeEnvironmentsById([environment], current));
    setEnvironmentsWarning(null);
  }

  const rows = page?.items ?? [];
  const canCreate = page?.source === "control-plane";

  return (
    <div className="mx-auto w-full max-w-4xl">
      <PageHeader
        eyebrow="Tenants"
        title="Projects"
        description={
          page?.source === "control-plane"
            ? "Live projects and environments through /api/console/projects and /api/console/environments."
            : "Local seed. The server uses the control plane when CONTROL_PLANE_URL is set."
        }
      />
      {canCreate ? <NewProjectForm onCreated={handleProjectCreated} /> : null}
      {canCreate ? (
        <NewEnvironmentForm projects={rows} onCreated={handleEnvironmentCreated} />
      ) : null}
      {error ? (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : (
        <>
          {environmentsWarning ? (
            <p className="mb-4 text-sm text-destructive" role="status">
              {environmentsWarning} Project list is still shown; environment rows may be incomplete.
            </p>
          ) : null}
          <Card>
            <CardContent className="divide-y divide-border pt-0">
              {page && rows.length === 0 ? (
                <p className="py-4 text-sm text-muted-foreground">No projects yet.</p>
              ) : null}
              {rows.map((row) => {
                const envs = environmentsForProject(environments, row.id);
                const latestDeploy = deployments.find((dep) => dep.project_id === row.id);
                return (
                  <div key={row.id} className="py-4 first:pt-4">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="font-medium">
                          <Link
                            href={projectDetailPath(row.id)}
                            className="text-foreground hover:text-primary hover:underline"
                          >
                            {row.name}
                          </Link>
                        </p>
                        <p className="font-mono text-xs text-primary">{row.slug}</p>
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Badge variant="secondary">
                          {envs.length} env{envs.length === 1 ? "" : "s"}
                        </Badge>
                        {latestDeploy ? (
                          <Link href={`/deployments/${latestDeploy.id}`}>
                            {latestDeploy.status === "succeeded" ? (
                              <Badge variant="default">
                                <CheckCircle2 className="size-3" />
                                Last deploy ok
                              </Badge>
                            ) : latestDeploy.status === "failed" ? (
                              <Badge variant="destructive">
                                <XCircle className="size-3" />
                                Last deploy failed
                              </Badge>
                            ) : (
                              <Badge variant="secondary">
                                <Loader2 className="size-3 animate-spin" />
                                Deploying
                              </Badge>
                            )}
                          </Link>
                        ) : null}
                      </div>
                    </div>
                    {row.description ? (
                      <p className="mt-1 text-sm text-muted-foreground">{row.description}</p>
                    ) : null}
                    {envs.length > 0 ? (
                      <ul className="mt-2 space-y-1 text-sm text-muted-foreground">
                        {envs.map((env) => (
                          <li key={env.id} className="font-mono text-xs">
                            <Link
                              href={projectDetailPath(row.id, "environments")}
                              className="hover:text-primary hover:underline"
                            >
                              {env.slug} ({env.kind}, {env.region})
                            </Link>
                          </li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                );
              })}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
