"use client";

import { Loader2, Shield, Trash2, UserPlus } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { fetchProjectMembers, removeProjectMember, upsertProjectMember } from "@/lib/api";
import type { ProjectMember, ProjectRole, ProjectSource } from "@/lib/types";

const ROLES: ProjectRole[] = ["owner", "developer", "viewer"];

export function ProjectMembersSection({
  projectId,
  source,
}: {
  projectId?: string;
  source?: ProjectSource;
}) {
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<ProjectRole>("developer");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId || source !== "control-plane") {
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setMembers(await fetchProjectMembers(projectId));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load project members.");
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

  async function handleInvite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!projectId || !email.trim()) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await upsertProjectMember(projectId, email.trim(), role);
      setMembers((prev) => [
        ...prev.filter((m) => m.user_id !== updated.user_id),
        updated,
      ].sort((a, b) => a.email.localeCompare(b.email)));
      setEmail("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not add member.");
    } finally {
      setSaving(false);
    }
  }

  async function handleRoleChange(member: ProjectMember, nextRole: ProjectRole) {
    if (!projectId) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await upsertProjectMember(projectId, member.email, nextRole);
      setMembers((prev) =>
        prev.map((m) => (m.user_id === updated.user_id ? updated : m)),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not update role.");
    } finally {
      setSaving(false);
    }
  }

  async function handleRemove(member: ProjectMember) {
    if (!projectId) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await removeProjectMember(projectId, member.user_id);
      setMembers((prev) => prev.filter((m) => m.user_id !== member.user_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not remove member.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section id="members" className="mt-8 scroll-mt-6">
      <h2 className="mb-3 inline-flex items-center gap-2 text-lg font-medium">
        <Shield className="size-4 text-primary" />
        Members &amp; access
      </h2>

      {error ? (
        <p className="mb-3 text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}

      <Card>
        <CardContent className="flex flex-col gap-4 pt-4">
          <form onSubmit={handleInvite} className="grid gap-2 sm:grid-cols-[2fr_1fr_auto]">
            <Input
              type="email"
              placeholder="teammate@example.com"
              value={email}
              disabled={saving}
              onValueChange={setEmail}
              className="h-9 text-xs"
            />
            <select
              aria-label="Member role"
              value={role}
              disabled={saving}
              onChange={(e) => setRole(e.target.value as ProjectRole)}
              className="h-9 rounded-md border border-input bg-background px-2 text-xs"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <Button type="submit" size="sm" disabled={saving || !email.trim()}>
              {saving ? <Loader2 className="size-3.5 animate-spin" /> : <UserPlus className="size-3.5" />}
              Add member
            </Button>
          </form>

          {loading && members.length === 0 ? (
            <p className="text-sm text-muted-foreground">Loading members…</p>
          ) : members.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No explicit members configured yet. Add an owner above to enable role enforcement on this project.
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {members.map((member) => (
                <li
                  key={member.user_id}
                  className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-sm"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-xs">{member.email}</span>
                    <Badge variant={member.role === "owner" ? "default" : "secondary"}>
                      {member.role}
                    </Badge>
                  </div>
                  <div className="flex items-center gap-2">
                    <select
                      aria-label={`Role for ${member.email}`}
                      value={member.role}
                      disabled={saving}
                      onChange={(e) => void handleRoleChange(member, e.target.value as ProjectRole)}
                      className="h-7 rounded-md border border-input bg-background px-2 text-xs"
                    >
                      {ROLES.map((r) => (
                        <option key={r} value={r}>
                          {r}
                        </option>
                      ))}
                    </select>
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={saving}
                      aria-label={`Remove ${member.email}`}
                      onClick={() => void handleRemove(member)}
                    >
                      <Trash2 className="size-3.5 text-destructive" />
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
