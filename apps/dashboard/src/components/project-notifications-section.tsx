"use client";

import { Bell, Loader2, Send, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  deleteProjectNotifications,
  fetchProjectNotifications,
  testProjectNotifications,
  upsertProjectNotifications,
} from "@/lib/api";
import type { NotificationChannel, NotificationProvider, ProjectSource } from "@/lib/types";

export function ProjectNotificationsSection({
  projectId,
  source,
}: {
  projectId?: string;
  source?: ProjectSource;
}) {
  const [channel, setChannel] = useState<NotificationChannel | null>(null);
  const [provider, setProvider] = useState<NotificationProvider>("slack");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [notifyStarted, setNotifyStarted] = useState(false);
  const [notifySucceeded, setNotifySucceeded] = useState(true);
  const [notifyFailed, setNotifyFailed] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId || source !== "control-plane") {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const current = await fetchProjectNotifications(projectId);
      setChannel(current);
      if (current) {
        setProvider(current.provider);
        setNotifyStarted(current.notify_on_started);
        setNotifySucceeded(current.notify_on_succeeded);
        setNotifyFailed(current.notify_on_failed);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load notification settings.");
    } finally {
      setLoading(false);
    }
  }, [projectId, source]);

  useEffect(() => {
    void load();
  }, [load]);

  if (source !== "control-plane" || !projectId) {
    return null;
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId) {
      return;
    }
    if (!channel && !webhookUrl.trim()) {
      setError("Enter an https:// Slack, Discord, or webhook URL.");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await upsertProjectNotifications(projectId, {
        provider,
        webhook_url: webhookUrl.trim() || undefined,
        notify_on_started: notifyStarted,
        notify_on_succeeded: notifySucceeded,
        notify_on_failed: notifyFailed,
        enabled: true,
      });
      setChannel(saved);
      setWebhookUrl("");
      setNotice("Notification channel saved (URL stored encrypted).");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save notification channel.");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    if (!projectId) {
      return;
    }
    setTesting(true);
    setError(null);
    setNotice(null);
    try {
      const res = await testProjectNotifications(projectId);
      if (res.ok) {
        setNotice(`Test notification delivered (HTTP ${res.status_code ?? 200}).`);
      } else {
        setError(res.error ?? "Test notification failed.");
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not send test notification.");
    } finally {
      setTesting(false);
    }
  }

  async function handleDelete() {
    if (!projectId) {
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      await deleteProjectNotifications(projectId);
      setChannel(null);
      setWebhookUrl("");
      setNotice("Notification channel removed.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not remove notification channel.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section id="notifications" className="mt-8 scroll-mt-6">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="inline-flex items-center gap-2 text-lg font-medium">
          <Bell className="size-4 text-primary" />
          Deployment notifications
        </h2>
        {channel ? (
          <Badge variant="secondary">
            {channel.provider} · {channel.url_hint}
          </Badge>
        ) : null}
      </div>

      <Card>
        <CardContent className="pt-4">
          {loading && !channel ? (
            <p className="text-sm text-muted-foreground">Loading notification settings…</p>
          ) : (
            <form onSubmit={handleSave} className="flex flex-col gap-3">
              <div className="grid gap-2 sm:grid-cols-[1fr_2fr]">
                <div className="flex flex-col gap-1">
                  <Label className="text-xs">Provider</Label>
                  <select
                    aria-label="Notification provider"
                    value={provider}
                    disabled={saving}
                    onChange={(e) => setProvider(e.target.value as NotificationProvider)}
                    className="h-9 rounded-md border border-input bg-background px-2 text-xs"
                  >
                    <option value="slack">Slack Incoming Webhook</option>
                    <option value="discord">Discord Webhook</option>
                    <option value="webhook">Generic JSON Webhook</option>
                  </select>
                </div>
                <div className="flex flex-col gap-1">
                  <Label className="text-xs">HTTPS Webhook URL</Label>
                  <Input
                    type="password"
                    placeholder={
                      channel
                        ? `Encrypted (${channel.url_hint}) — enter new URL to replace`
                        : "https://hooks.slack.com/services/..."
                    }
                    value={webhookUrl}
                    disabled={saving}
                    onValueChange={setWebhookUrl}
                    className="h-9 font-mono text-xs"
                  />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
                <label className="flex cursor-pointer items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={notifyStarted}
                    disabled={saving}
                    onChange={(e) => setNotifyStarted(e.target.checked)}
                    className="size-3.5 accent-primary"
                  />
                  On started
                </label>
                <label className="flex cursor-pointer items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={notifySucceeded}
                    disabled={saving}
                    onChange={(e) => setNotifySucceeded(e.target.checked)}
                    className="size-3.5 accent-primary"
                  />
                  On succeeded
                </label>
                <label className="flex cursor-pointer items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={notifyFailed}
                    disabled={saving}
                    onChange={(e) => setNotifyFailed(e.target.checked)}
                    className="size-3.5 accent-primary"
                  />
                  On failed
                </label>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button type="submit" size="sm" disabled={saving}>
                  {saving ? <Loader2 className="size-3.5 animate-spin" /> : null}
                  {channel ? "Update notifications" : "Save notifications"}
                </Button>
                {channel ? (
                  <>
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={testing || saving}
                      onClick={() => void handleTest()}
                    >
                      {testing ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}
                      Send test notification
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
            </form>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
