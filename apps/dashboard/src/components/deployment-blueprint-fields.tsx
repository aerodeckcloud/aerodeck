"use client";

import { Database, KeyRound, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { AddonType, DeploymentBlueprint } from "@/lib/types";

export function parseDotenvText(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of raw.split(/\r?\n/)) {
    let line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    if (line.startsWith("export ")) {
      line = line.slice(7).trim();
    }
    const eqIndex = line.indexOf("=");
    if (eqIndex <= 0) {
      continue;
    }
    const key = line.slice(0, eqIndex).trim();
    const val = line.slice(eqIndex + 1).trim().replace(/^['"]|['"]$/g, "");
    if (/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(key)) {
      out[key] = val;
    }
  }
  return out;
}

function generateHex32(): string {
  const bytes = new Uint8Array(24);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

type DeploymentBlueprintFieldsProps = {
  blueprint: DeploymentBlueprint | null;
  fieldValues: Record<string, string>;
  onFieldChange: (key: string, value: string) => void;
  selectedAddons: AddonType[];
  onToggleAddon: (addon: AddonType) => void;
  extraEnvText: string;
  onExtraEnvTextChange: (value: string) => void;
  disabled: boolean;
};

export function DeploymentBlueprintFields({
  blueprint,
  fieldValues,
  onFieldChange,
  selectedAddons,
  onToggleAddon,
  extraEnvText,
  onExtraEnvTextChange,
  disabled,
}: DeploymentBlueprintFieldsProps) {
  return (
    <div className="flex flex-col gap-4">
      {blueprint ? (
        <div className="rounded-lg border border-border/80 bg-muted/30 p-3 text-xs">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium text-foreground">
              Blueprint detected:{" "}
              <code className="rounded bg-background px-1.5 py-0.5 font-mono">
                {blueprint.source}
              </code>
            </span>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant="outline">strategy: {blueprint.build.strategy}</Badge>
              <Badge variant="outline">port: {blueprint.build.port}</Badge>
              {blueprint.build.service ? (
                <Badge variant="outline">service: {blueprint.build.service}</Badge>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-1.5">
          <Database className="size-4 text-primary" />
          <Label>Database addons (auto-inject credentials)</Label>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {(
            [
              {
                type: "postgres" as const,
                title: "PostgreSQL 16",
                envVar: "DATABASE_URL",
                subtitle: "Dedicated postgres:16-alpine container",
              },
              {
                type: "redis" as const,
                title: "Redis 7",
                envVar: "REDIS_URL",
                subtitle: "Dedicated redis:7-alpine cache/queue",
              },
            ] as const
          ).map((item) => {
            const active = selectedAddons.includes(item.type);
            return (
              <label
                key={item.type}
                className={`flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 text-xs transition-colors ${
                  active
                    ? "border-primary bg-primary/5"
                    : "border-border hover:border-primary/40"
                }`}
              >
                <input
                  type="checkbox"
                  checked={active}
                  disabled={disabled}
                  onChange={() => onToggleAddon(item.type)}
                  className="mt-0.5 size-4 accent-primary"
                />
                <div className="flex flex-col gap-0.5">
                  <span className="font-medium text-foreground">
                    {item.title}{" "}
                    <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px]">
                      {item.envVar}
                    </code>
                  </span>
                  <span className="text-muted-foreground">{item.subtitle}</span>
                </div>
              </label>
            );
          })}
        </div>
      </div>

      {blueprint && blueprint.env.length > 0 ? (
        <div className="flex flex-col gap-3 rounded-lg border border-border/80 p-3">
          <div className="flex items-center gap-1.5">
            <KeyRound className="size-4 text-primary" />
            <Label>Application variables ({blueprint.env.length})</Label>
          </div>
          <div className="grid gap-3">
            {blueprint.env.map((item) => {
              const val = fieldValues[item.key] ?? "";
              const missingRequired = item.required && val.trim().length === 0;
              return (
                <div key={item.key} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-2">
                    <Label htmlFor={`bp-env-${item.key}`} className="font-mono text-xs">
                      {item.key}
                      {item.required ? (
                        <span className="ml-1 text-destructive">*</span>
                      ) : null}
                    </Label>
                    {item.generate === "hex32" || item.secret ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={disabled}
                        onClick={() => onFieldChange(item.key, generateHex32())}
                        className="h-6 px-2 text-[11px]"
                      >
                        <Sparkles className="mr-1 size-3" />
                        Generate
                      </Button>
                    ) : null}
                  </div>
                  <Input
                    id={`bp-env-${item.key}`}
                    type={item.secret ? "password" : "text"}
                    value={val}
                    disabled={disabled}
                    aria-invalid={missingRequired ? true : undefined}
                    placeholder={item.default ?? (item.required ? "Required value" : "Optional")}
                    onValueChange={(next) => onFieldChange(item.key, next)}
                    className="h-9 font-mono text-xs"
                  />
                  {item.description ? (
                    <span className="text-[11px] text-muted-foreground">
                      {item.description}
                    </span>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      <div className="flex flex-col gap-2">
        <Label htmlFor="env-vars">Additional environment variables (.env format, optional)</Label>
        <Textarea
          id="env-vars"
          rows={3}
          placeholder={"KEY=value\nGITLAB_TOKEN=..."}
          value={extraEnvText}
          disabled={disabled}
          onChange={(e) => onExtraEnvTextChange(e.target.value)}
          className="font-mono text-xs"
        />
        <p className="text-xs text-muted-foreground">
          One KEY=VALUE per line (or paste a .env file). Overrides auto-discovered repository defaults.
        </p>
      </div>
    </div>
  );
}
