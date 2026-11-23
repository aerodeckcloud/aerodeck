import { Box, Cloud, GitBranch } from "lucide-react";
import type { Metadata } from "next";

import { BrandMark } from "@/components/brand-mark";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export const metadata: Metadata = {
  title: "About",
  description:
    "AeroDeck is a GitOps operator console for DigitalOcean, with Firecracker sandboxes.",
};

const PILLARS = [
  {
    icon: GitBranch,
    tick: "bg-primary",
    title: "GitOps console",
    body: "Operators point AeroDeck at a GitHub repository. The control plane plans and applies from that repo instead of clicking around a cloud console.",
  },
  {
    icon: Cloud,
    tick: "bg-chart-3",
    title: "DigitalOcean only",
    body: "Compute, Postgres, Redis, Spaces, and DNS live on DigitalOcean. The IaC engine does not target AWS, GCP, or other clouds.",
  },
  {
    icon: Box,
    tick: "bg-runway",
    title: "Firecracker sandboxes",
    body: "Short-lived environments run in Firecracker microVMs, not long-lived shared clusters. Sandboxes expire; they are not production tenants.",
  },
] as const;

export default function AboutPage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-4 py-12 md:px-8 md:py-20">
      <div className="mb-12 flex flex-col gap-6 sm:flex-row sm:items-end">
        <BrandMark className="size-16 shrink-0 text-primary" />
        <div>
          <p className="font-mono text-[11px] tracking-[0.22em] text-primary uppercase">
            Flight deck
          </p>
          <h1 className="mt-2 font-heading text-4xl font-semibold tracking-tight md:text-5xl">
            A GitOps flight deck for DigitalOcean
          </h1>
          <p className="mt-3 max-w-2xl text-sm text-muted-foreground">
            AeroDeck is the operator console for GitOps deployments on DigitalOcean. This page is
            static product copy — it does not call FastAPI.
          </p>
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
                <p className="text-sm text-muted-foreground">{pillar.body}</p>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
