"use client";

import { KeyRound, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { DeploymentBlueprint } from "@/lib/types";

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
  if (typeof crypto === "undefined" || typeof crypto.getRandomValues !== "function") {
    throw new Error("Web Crypto API is unavailable in this browser context.");
  }
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

type DeploymentBlueprintFieldsProps = {
  blueprint: DeploymentBlueprint | null;
  fieldValues: Record<string, string>;
  onFieldChange: (key: string, value: string) => void;
  extraEnvText: string;
  onExtraEnvTextChange: (value: string) => void;
  savedEnvKeys?: Set<string>;
  disabled: boolean;
};

export function DeploymentBlueprintFields({
  blueprint,
  fieldValues,
  onFieldChange,
  extraEnvText,
  onExtraEnvTextChange,
  savedEnvKeys,
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

      {blueprint && blueprint.env.length > 0 ? (
        <div className="flex flex-col gap-3 rounded-lg border border-border/80 p-3">
          <div className="flex items-center gap-1.5">
            <KeyRound className="size-4 text-primary" />
            <Label>Application variables ({blueprint.env.length})</Label>
          </div>
          <div className="grid gap-3">
            {blueprint.env.map((item) => {
              const val = fieldValues[item.key] ?? "";
              const isSaved = savedEnvKeys?.has(item.key) ?? false;
              const missingRequired = item.required && !isSaved && val.trim().length === 0;
              return (
                <div key={item.key} className="flex flex-col gap-1">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5">
                      <Label htmlFor={`bp-env-${item.key}`} className="font-mono text-xs">
                        {item.key}
                        {item.required && !isSaved ? (
                          <span className="ml-1 text-destructive">*</span>
                        ) : null}
                      </Label>
                      {isSaved ? (
                        <Badge variant="secondary" className="text-[10px]">
                          Saved
                        </Badge>
                      ) : null}
                    </div>
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
                    placeholder={
                      isSaved
                        ? "Using saved encrypted value (enter new value to override)"
                        : (item.default ?? (item.required ? "Required value" : "Optional"))
                    }
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
