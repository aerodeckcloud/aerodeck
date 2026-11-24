import { Box, Cloud, GitBranch } from "lucide-react";
import type { Metadata } from "next";

import { BrandMark } from "@/components/brand-mark";
import { LinkButton } from "@/components/link-button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PRODUCT_NAME } from "@/lib/brand";

export const metadata: Metadata = {
  title: "About",
  description: `${PRODUCT_NAME} is the operator console for GitOps on DigitalOcean. A GitHub repository is the source. Sandboxes are short-lived Firecracker microVMs.`,
};

const PILLARS = [
  {
    icon: GitBranch,
    tick: "bg-primary",
    title: "A repository is the source",
    body: "An operator points a project at a GitHub repository. Deployments launch from that repository, and the deck keeps the status, the logs, and the history with the flight.",
  },
  {
    icon: Cloud,
    tick: "bg-chart-3",
    title: "One cloud, on purpose",
    body: "Compute, Postgres, Redis, Spaces, and DNS stay on DigitalOcean. A hostname is published from the project with its DNS record and its certificate. The engine does not provision any other cloud.",
  },
  {
    icon: Box,
    tick: "bg-runway",
    title: "Sandboxes that expire",
    body: "A sandbox runs in a Firecracker microVM. It is a lab for work that should not last. It is not a production tenant, and it does not outlive the job.",
  },
] as const;

export default function AboutPage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 md:px-8 md:py-20">
      <div className="mb-14 flex flex-col gap-8 sm:flex-row sm:items-start">
        <BrandMark className="size-16 shrink-0 text-primary" />
        <div className="max-w-2xl">
          <p className="font-mono text-[11px] tracking-[0.22em] text-primary uppercase">
            About {PRODUCT_NAME}
          </p>
          <h1 className="mt-3 font-heading text-4xl font-semibold tracking-tight md:text-5xl">
            The flight deck for GitOps on DigitalOcean
          </h1>
          <div className="mt-6 space-y-4 text-sm leading-relaxed text-muted-foreground md:text-base">
            <p>
              {PRODUCT_NAME} is the console an operator uses to run a workload from a GitHub
              repository onto DigitalOcean. The repository is the source of the change.
              The control plane plans that change and applies it. The deck is where you
              watch the flight: the project, the deployment, the hostname, and any
              sandbox opened for the work.
            </p>
            <p>
              Production can wait for an owner before it leaves the gate, and automatic
              deploys can be held when the runway should stay quiet. The record of each
              flight stays with the deployment, including the variables you may need to
              restore.
            </p>
          </div>
          <div className="mt-8 flex flex-wrap gap-3">
            <LinkButton href="/login">Open the deck</LinkButton>
            <LinkButton href="/register" variant="outline">
              Create an account
            </LinkButton>
          </div>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-3">
        {PILLARS.map((pillar) => {
          const Icon = pillar.icon;
          return (
            <Card key={pillar.title}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <span className={`h-4 w-1 rounded-full ${pillar.tick}`} aria-hidden />
                  <Icon className="size-4" />
                  {pillar.title}
                </CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm leading-relaxed text-muted-foreground">{pillar.body}</p>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
