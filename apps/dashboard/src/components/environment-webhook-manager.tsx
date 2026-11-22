"use client";

import { Check, Copy, GitBranch, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  deleteEnvironmentWebhook,
  fetchEnvironmentWebhook,
  upsertEnvironmentWebhook,
} from "@/lib/api";
import type { EnvironmentWebhook } from "@/lib/types";

export function EnvironmentWebhookManager({
  projectId,
  environmentId,
}: {
  projectId: string;
  environmentId: string;
}) {
  const [open, setOpen] = useState(false);
  const [webhook, setWebhook] = useState<EnvironmentWebhook | null>(null);
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);
  const [repoUrl, setRepoUrl] = useState("");
  const [branch, setBranch] = useState("main");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [copiedField, setCopiedField] = useState<"url" | "secret" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const payloadUrl =
    typeof window !== "undefined"
      ? `${window.location.origin}/api/webhooks/github`
      : "/api/webhooks/github";

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const current = await fetchEnvironmentWebhook(projectId, environmentId);
      setWebhook(current);
      if (current) {
        setRepoUrl(current.repo_url);
        setBranch(current.branch);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load webhook settings.");
    } finally {
      setLoading(false);
    }
  }, [projectId, environmentId]);

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [open, load]);

  async function handleSave(rotateSecret = false) {
    if (!repoUrl.trim()) {
      setError("Enter a GitHub repository URL (e.g. https://github.com/org/repo).");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const saved = await upsertEnvironmentWebhook(projectId, environmentId, {
        repo_url: repoUrl.trim(),
        branch: branch.trim() || "main",
        enabled: true,
        rotate_secret: rotateSecret,
      });
      setWebhook(saved);
      if (saved.secret) {
        setRevealedSecret(saved.secret);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save webhook.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    setError(null);
    setSaving(true);
    try {
      await deleteEnvironmentWebhook(projectId, environmentId);
      setWebhook(null);
      setRevealedSecret(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not remove webhook.");
    } finally {
      setSaving(false);
    }
  }

  function copyValue(field: "url" | "secret", value: string) {
    void navigator.clipboard.writeText(value);
    setCopiedField(field);
    window.setTimeout(() => setCopiedField(null), 2000);
  }

  return (
    <div className="sm:col-span-2 rounded-lg border border-border/70 bg-muted/20 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium">
          <GitBranch className="size-3.5 text-primary" />
          GitHub push webhook (auto-redeploy)
          {webhook ? <Badge variant="secondary">{webhook.branch}</Badge> : null}
        </span>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide" : "Configure"}
        </Button>
      </div>

      {open ? (
        <div className="mt-3 flex flex-col gap-3">
          {loading ? (
            <p className="text-xs text-muted-foreground">Loading webhook settings…</p>
          ) : (
            <>
              <div className="flex flex-col gap-1">
                <Label className="text-xs">GitHub Payload URL (Content type: application/json)</Label>
                <div className="flex items-center gap-2">
                  <code className="flex-1 truncate rounded border bg-background px-2 py-1 font-mono text-xs">
                    {payloadUrl}
                  </code>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => copyValue("url", payloadUrl)}
                  >
                    {copiedField === "url" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                    Copy
                  </Button>
                </div>
              </div>

              <div className="grid gap-2 sm:grid-cols-[2fr_1fr]">
                <div className="flex flex-col gap-1">
                  <Label className="text-xs">Repository URL</Label>
                  <Input
                    placeholder="https://github.com/org/repo"
                    value={repoUrl}
                    disabled={saving}
                    onValueChange={setRepoUrl}
                    className="h-8 font-mono text-xs"
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <Label className="text-xs">Branch</Label>
                  <Input
                    placeholder="main"
                    value={branch}
                    disabled={saving}
                    onValueChange={setBranch}
                    className="h-8 font-mono text-xs"
                  />
                </div>
              </div>

              {revealedSecret ? (
                <div className="rounded-md border border-primary/40 bg-primary/5 p-2.5 text-xs">
                  <p className="font-medium text-foreground">
                    Webhook Secret (shown once — copy into GitHub → Settings → Webhooks):
                  </p>
                  <div className="mt-1.5 flex items-center gap-2">
                    <code className="flex-1 truncate rounded bg-background px-2 py-1 font-mono text-[11px]">
                      {revealedSecret}
                    </code>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => copyValue("secret", revealedSecret)}
                    >
                      {copiedField === "secret" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                      Copy
                    </Button>
                  </div>
                </div>
              ) : null}

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  disabled={saving}
                  onClick={() => void handleSave(false)}
                >
                  {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
                  {webhook ? "Update webhook" : "Enable webhook"}
                </Button>
                {webhook ? (
                  <>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={saving}
                      onClick={() => void handleSave(true)}
                    >
                      <RefreshCw className="size-3.5" />
                      Rotate secret
                    </Button>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={saving}
                      onClick={() => void handleDelete()}
                    >
                      <Trash2 className="size-3.5 text-destructive" />
                      Remove
                    </Button>
                  </>
                ) : null}
              </div>
            </>
          )}

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
