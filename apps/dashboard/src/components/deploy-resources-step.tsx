import { Database } from "lucide-react";

import { Label } from "@/components/ui/label";
import { offeredAddonTypes } from "@/lib/deploy-addons";
import type { AddonType } from "@/lib/types";

const ADDON_COPY: Record<AddonType, { title: string; envVar: string; subtitle: string }> = {
  postgres: {
    title: "PostgreSQL 16",
    envVar: "DATABASE_URL",
    subtitle: "Dedicated postgres:16-alpine container",
  },
  redis: {
    title: "Redis 7",
    envVar: "REDIS_URL",
    subtitle: "Dedicated redis:7-alpine cache/queue",
  },
  mongodb: {
    title: "MongoDB 7",
    envVar: "MONGODB_URI",
    subtitle: "Dedicated mongo:7 document database",
  },
};

type DeployResourcesStepProps = {
  detected: readonly AddonType[];
  selected: readonly AddonType[];
  onToggle: (addon: AddonType) => void;
  disabled: boolean;
};

export function DeployResourcesStep({ detected, selected, onToggle, disabled }: DeployResourcesStepProps) {
  const offered = offeredAddonTypes(detected);

  return (
    <section className="flex flex-col gap-3" aria-label="Databases">
      <div className="flex items-center gap-1.5">
        <Database className="size-4 text-primary" />
        <Label>Databases</Label>
      </div>
      {detected.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No databases were detected. You can add Postgres, Redis, or MongoDB.
        </p>
      ) : (
        <p className="text-sm text-muted-foreground">These are the databases the scan found.</p>
      )}
      <div className="grid gap-2 sm:grid-cols-3">
        {offered.map((type) => {
          const item = ADDON_COPY[type];
          const active = selected.includes(type);
          return (
            <label
              key={type}
              className={`flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 text-xs transition-colors ${
                active ? "border-primary bg-primary/5" : "border-border hover:border-primary/40"
              }`}
            >
              <input
                type="checkbox"
                checked={active}
                disabled={disabled}
                onChange={() => onToggle(type)}
                className="mt-0.5 size-4 accent-primary"
              />
              <span className="flex flex-col gap-0.5">
                <span className="font-medium text-foreground">
                  {item.title}{" "}
                  <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">{item.envVar}</code>
                </span>
                <span className="text-muted-foreground">{item.subtitle}</span>
              </span>
            </label>
          );
        })}
      </div>
    </section>
  );
}
