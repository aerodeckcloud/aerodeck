"use client";

import { Copy, FileUp, KeyRound, Loader2, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { parseDotenvText } from "@/components/deployment-blueprint-fields";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  cloneEnvironmentVars,
  deleteEnvironmentVar,
  fetchEnvironmentVars,
  upsertEnvironmentVars,
} from "@/lib/api";
import type { EnvironmentOption, EnvironmentVarSummary } from "@/lib/types";

export function EnvironmentVarsManager({
  projectId,
  environmentId,
  peerEnvironments = [],
}: {
  projectId: string;
  environmentId: string;
  peerEnvironments?: EnvironmentOption[];
}) {
  const [open, setOpen] = useState(false);
  const [bulkMode, setBulkMode] = useState(false);
  const [items, setItems] = useState<EnvironmentVarSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [bulkText, setBulkText] = useState("");
  const [cloneSourceId, setCloneSourceId] = useState("");
  const [cloneOverwrite, setCloneOverwrite] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
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
    setNotice(null);
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

  async function handleBulkImport() {
    const parsed = parseDotenvText(bulkText);
    const entries = Object.entries(parsed).map(([key, value]) => ({
      key,
      value,
      is_secret: true,
    }));
    if (entries.length === 0) {
      setError("Paste at least one valid KEY=VALUE line.");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const updated = await upsertEnvironmentVars(projectId, environmentId, entries);
      setItems(updated);
      setBulkText("");
      setBulkMode(false);
      setNotice(`Imported ${entries.length} encrypted variable(s).`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not import variables.");
    } finally {
      setSaving(false);
    }
  }

  async function handleClone() {
    if (!cloneSourceId) {
      setError("Select a source environment to clone from.");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await cloneEnvironmentVars(
        projectId,
        environmentId,
        cloneSourceId,
        cloneOverwrite,
      );
      setItems(res.items);
      setNotice(`Cloned ${res.copied} variable(s), skipped ${res.skipped}.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not clone variables.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(key: string) {
    setError(null);
    setNotice(null);
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

          {bulkMode ? (
            <div className="flex flex-col gap-2">
              <Textarea
                rows={4}
                placeholder={"KEY_ONE=value1\nKEY_TWO=value2"}
                value={bulkText}
                disabled={saving}
                onChange={(e) => setBulkText(e.target.value)}
                className="font-mono text-xs"
              />
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  size="sm"
                  disabled={saving || bulkText.trim().length === 0}
                  onClick={() => void handleBulkImport()}
                >
                  {saving ? <Loader2 className="size-3.5 animate-spin" /> : <FileUp className="size-3.5" />}
                  Import .env
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  disabled={saving}
                  onClick={() => setBulkMode(false)}
                >
                  Single key mode
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
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
              <div>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  onClick={() => setBulkMode(true)}
                  className="h-7 px-2 text-xs text-muted-foreground"
                >
                  <FileUp className="size-3.5" />
                  Bulk paste .env
                </Button>
              </div>
            </div>
          )}

          {peerEnvironments.length > 0 ? (
            <div className="flex flex-wrap items-center gap-2 border-t border-border/60 pt-2.5 text-xs">
              <select
                aria-label="Clone variables from environment"
                value={cloneSourceId}
                disabled={saving}
                onChange={(e) => setCloneSourceId(e.target.value)}
                className="h-8 rounded-md border border-input bg-background px-2 text-xs"
              >
                <option value="">Clone from environment…</option>
                {peerEnvironments.map((peer) => (
                  <option key={peer.id} value={peer.id}>
                    {peer.slug} ({peer.kind})
                  </option>
                ))}
              </select>
              <label className="flex cursor-pointer items-center gap-1.5 text-muted-foreground">
                <input
                  type="checkbox"
                  checked={cloneOverwrite}
                  disabled={saving}
                  onChange={(e) => setCloneOverwrite(e.target.checked)}
                  className="size-3.5 accent-primary"
                />
                Overwrite existing
              </label>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={saving || !cloneSourceId}
                onClick={() => void handleClone()}
              >
                <Copy className="size-3.5" />
                Clone
              </Button>
            </div>
          ) : null}

          {notice ? (
            <p className="text-xs text-primary" role="status">
              {notice}
            </p>
          ) : null}
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
