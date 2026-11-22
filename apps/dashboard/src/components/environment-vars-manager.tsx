"use client";

import { KeyRound, Loader2, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  deleteEnvironmentVar,
  fetchEnvironmentVars,
  upsertEnvironmentVars,
} from "@/lib/api";
import type { EnvironmentVarSummary } from "@/lib/types";

export function EnvironmentVarsManager({
  projectId,
  environmentId,
}: {
  projectId: string;
  environmentId: string;
}) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<EnvironmentVarSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await fetchEnvironmentVars(projectId, environmentId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load environment variables.");
    } finally {
      setLoading(false);
    }
  }, [projectId, environmentId]);

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [open, load]);

  async function handleAdd() {
    const key = newKey.trim();
    if (!key) {
      setError("Enter a variable key (e.g. GITLAB_TOKEN).");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await upsertEnvironmentVars(projectId, environmentId, [
        { key, value: newValue, is_secret: true },
      ]);
      setItems(updated);
      setNewKey("");
      setNewValue("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save variable.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(key: string) {
    setError(null);
    try {
      await deleteEnvironmentVar(projectId, environmentId, key);
      setItems((prev) => prev.filter((item) => item.key !== key));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not delete variable.");
    }
  }

  return (
    <div className="sm:col-span-2 rounded-lg border border-border/70 bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium">
          <KeyRound className="size-3.5 text-primary" />
          Encrypted environment variables
          {open ? <Badge variant="secondary">{items.length}</Badge> : null}
        </span>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Manage"}
        </Button>
      </div>

      {open ? (
        <div className="mt-3 flex flex-col gap-3">
          {loading ? (
            <p className="text-xs text-muted-foreground">Loading saved keys…</p>
          ) : items.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              No saved variables yet. Saved keys are encrypted at rest and merged automatically on every deploy.
            </p>
          ) : (
            <ul className="divide-y divide-border/60 rounded-md border bg-background">
              {items.map((item) => (
                <li
                  key={item.key}
                  className="flex items-center justify-between gap-2 px-2.5 py-1.5 text-xs"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-medium">{item.key}</span>
                    <Badge variant="outline" className="text-[10px]">
                      Encrypted
                    </Badge>
                  </div>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    aria-label={`Delete ${item.key}`}
                    onClick={() => void handleDelete(item.key)}
                  >
                    <Trash2 className="size-3.5 text-destructive" />
                  </Button>
                </li>
              ))}
            </ul>
          )}

          <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
            <Input
              placeholder="KEY_NAME"
              value={newKey}
              disabled={saving}
              onValueChange={setNewKey}
              className="h-8 font-mono text-xs"
            />
            <Input
              type="password"
              placeholder="Secret value"
              value={newValue}
              disabled={saving}
              onValueChange={setNewValue}
              className="h-8 font-mono text-xs"
            />
            <Button
              type="button"
              size="sm"
              disabled={saving || newKey.trim().length === 0}
              onClick={() => void handleAdd()}
            >
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
              Save
            </Button>
          </div>

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
