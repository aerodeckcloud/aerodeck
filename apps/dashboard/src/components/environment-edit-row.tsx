"use client";

import { Loader2, RefreshCw, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";

import {
  EnvironmentKindField,
  EnvironmentRegionField,
  withCurrentOption,
} from "@/components/environment-form-fields";
import { EnvironmentRuntimePanel } from "@/components/environment-runtime-panel";
import { EnvironmentVarsManager } from "@/components/environment-vars-manager";
import { EnvironmentWebhookManager } from "@/components/environment-webhook-manager";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { deleteEnvironment, queueEnvironmentReconcile, updateEnvironment } from "@/lib/api";
import {
  ENVIRONMENT_KINDS,
  ENVIRONMENT_REGIONS,
  RELEASE_COMMAND_MAX,
  ROLLBACK_IMAGES_DEFAULT,
  ROLLBACK_IMAGES_MAX,
  ROLLBACK_IMAGES_MIN,
  parseEnvironmentUpdateFields,
} from "@/lib/environment-input";
import { nativeSelectClassName } from "@/lib/native-select";
import { PROJECT_NAME_MAX } from "@/lib/project-input";
import type { DeployOverlap, EnvironmentOption } from "@/lib/types";

function retainedImageLabel(count: number): string {
  return count === 1 ? "1 rollback image still present" : `${count} rollback images still present`;
}

type EnvironmentEditRowProps = {
  environment: EnvironmentOption;
  peerEnvironments?: EnvironmentOption[];
  onUpdated: (environment: EnvironmentOption) => void;
  onDeleted: (environmentId: string) => void;
};

export function EnvironmentEditRow({
  environment,
  peerEnvironments = [],
  onUpdated,
  onDeleted,
}: EnvironmentEditRowProps) {
  const [name, setName] = useState(environment.name);
  const [kind, setKind] = useState(environment.kind);
  const [region, setRegion] = useState(environment.region);
  const [overlap, setOverlap] = useState<DeployOverlap>(environment.deploy_overlap ?? "wait");
  const [keep, setKeep] = useState(
    String(environment.rollback_images_to_keep ?? ROLLBACK_IMAGES_DEFAULT),
  );
  const [command, setCommand] = useState(environment.release_command ?? "");
  const [paused, setPaused] = useState(environment.auto_deploy_paused ?? false);
  const [approval, setApproval] = useState(environment.prod_deploy_approval ?? false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [reconcileJobId, setReconcileJobId] = useState<string | null>(null);

  const idPrefix = `env-${environment.id}`;

  function clearError() {
    if (error) {
      setError(null);
    }
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const fields: {
      name?: string;
      kind?: string;
      region?: string;
      deploy_overlap?: DeployOverlap;
      rollback_images_to_keep?: number;
      release_command?: string | null;
      auto_deploy_paused?: boolean;
      prod_deploy_approval?: boolean;
    } = {};
    if (name.trim() !== environment.name) {
      fields.name = name;
    }
    if (kind !== environment.kind) {
      fields.kind = kind;
    }
    if (region !== environment.region) {
      fields.region = region;
    }
    if (overlap !== (environment.deploy_overlap ?? "wait")) {
      fields.deploy_overlap = overlap;
    }
    const keepCount = Number(keep);
    if (keepCount !== (environment.rollback_images_to_keep ?? ROLLBACK_IMAGES_DEFAULT)) {
      fields.rollback_images_to_keep = keepCount;
    }
    const nextCommand = command.trim();
    if (nextCommand !== (environment.release_command ?? "")) {
      fields.release_command = nextCommand === "" ? null : nextCommand;
    }
    if (paused !== (environment.auto_deploy_paused ?? false)) {
      fields.auto_deploy_paused = paused;
    }
    if (approval !== (environment.prod_deploy_approval ?? false)) {
      fields.prod_deploy_approval = approval;
    }
    const parsed = parseEnvironmentUpdateFields(fields);
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }
    setSaving(true);
    try {
      const updated = await updateEnvironment(
        environment.project_id,
        environment.id,
        parsed.value,
      );
      onUpdated(updated);
      setName(updated.name);
      setKind(updated.kind);
      setRegion(updated.region);
      setOverlap(updated.deploy_overlap ?? "wait");
      setKeep(String(updated.rollback_images_to_keep ?? ROLLBACK_IMAGES_DEFAULT));
      setCommand(updated.release_command ?? "");
      setPaused(updated.auto_deploy_paused ?? false);
      setApproval(updated.prod_deploy_approval ?? false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Update failed.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    const confirmed = window.confirm(
      `Delete environment “${environment.slug}”? Linked deployments will be cleared.`,
    );
    if (!confirmed) {
      return;
    }
    setError(null);
    setDeleting(true);
    try {
      await deleteEnvironment(environment.project_id, environment.id);
      onDeleted(environment.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Delete failed.");
      setDeleting(false);
    }
  }

  async function handleReconcile() {
    setError(null);
    setReconcileJobId(null);
    setReconciling(true);
    try {
      const accepted = await queueEnvironmentReconcile(environment.project_id, environment.id);
      setReconcileJobId(accepted.job_id);
    } catch (cause) {
      setReconcileJobId(null);
      setError(cause instanceof Error ? cause.message : "Reconcile failed.");
    } finally {
      setReconciling(false);
    }
  }

  const busy = saving || deleting || reconciling;

  return (
    <form onSubmit={handleSave} className="grid gap-3 py-4 first:pt-4 sm:grid-cols-2">
      <div className="sm:col-span-2">
        <p className="font-mono text-xs text-primary">{environment.slug}</p>
        <p className="text-sm text-muted-foreground">
          {retainedImageLabel(environment.retained_rollback_images ?? 0)}
        </p>
      </div>
      <div className="grid gap-2">
        <Label htmlFor={`${idPrefix}-name`}>Name</Label>
        <Input
          id={`${idPrefix}-name`}
          value={name}
          maxLength={PROJECT_NAME_MAX}
          disabled={busy}
          onChange={(event) => {
            setName(event.currentTarget.value);
            clearError();
          }}
        />
      </div>
      <EnvironmentKindField
        idPrefix={idPrefix}
        kind={kind}
        options={withCurrentOption(ENVIRONMENT_KINDS, environment.kind)}
        disabled={busy}
        onKindChange={setKind}
        onClearError={clearError}
      />
      <EnvironmentRegionField
        idPrefix={idPrefix}
        region={region}
        options={withCurrentOption(ENVIRONMENT_REGIONS, environment.region)}
        disabled={busy}
        onRegionChange={setRegion}
        onClearError={clearError}
      />
      <div className="grid gap-2 sm:col-span-2">
        <Label htmlFor={`${idPrefix}-overlap`}>When a deploy is already running</Label>
        <select
          id={`${idPrefix}-overlap`}
          className={nativeSelectClassName()}
          value={overlap}
          disabled={busy}
          onChange={(event) => {
            const next = event.target.value;
            if (next === "wait" || next === "override") {
              setOverlap(next);
              clearError();
            }
          }}
        >
          <option value="wait">Wait for the current deploy</option>
          <option value="override">Replace the current deploy</option>
        </select>
      </div>
      <div className="grid gap-2 sm:col-span-2">
        <Label htmlFor={`${idPrefix}-keep`}>Rollback images to keep</Label>
        <Input
          id={`${idPrefix}-keep`}
          type="number"
          inputMode="numeric"
          min={ROLLBACK_IMAGES_MIN}
          max={ROLLBACK_IMAGES_MAX}
          step={1}
          value={keep}
          disabled={busy}
          onChange={(event) => {
            setKeep(event.currentTarget.value);
            clearError();
          }}
        />
      </div>
      <div className="grid gap-2 sm:col-span-2">
        <Label htmlFor={`${idPrefix}-release`}>Release command</Label>
        <Input
          id={`${idPrefix}-release`}
          value={command}
          maxLength={RELEASE_COMMAND_MAX}
          placeholder="python manage.py migrate"
          disabled={busy}
          spellCheck={false}
          onChange={(event) => {
            setCommand(event.currentTarget.value);
            clearError();
          }}
        />
        <p className="text-sm text-muted-foreground">
          Optional. An executable and its arguments, not a shell script. It runs in the new
          container after the health check and before traffic moves. If it fails or times out,
          the current release stays up. Rolling an image back does not undo migrations this
          command applied.
        </p>
      </div>
      <label className="flex items-center gap-2 text-sm sm:col-span-2">
        <input
          type="checkbox"
          className="size-3.5 accent-primary"
          checked={paused}
          disabled={busy}
          onChange={(event) => {
            setPaused(event.currentTarget.checked);
            clearError();
          }}
        />
        Pause automatic redeploys
      </label>
      <div className="grid gap-1 sm:col-span-2">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="size-3.5 accent-primary"
            checked={approval}
            disabled={busy}
            onChange={(event) => {
              setApproval(event.currentTarget.checked);
              clearError();
            }}
          />
          Require owner approval for production deploys
        </label>
        <p className="text-sm text-muted-foreground">
          Applies when this environment is production. A developer or a GitHub push is stored
          until an owner approves it. An owner can still deploy immediately.
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          {saving ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => {
            void handleReconcile();
          }}
        >
          {reconciling ? (
            <Loader2 className="size-4 animate-spin" aria-hidden />
          ) : (
            <RefreshCw className="size-4" aria-hidden />
          )}
          {reconciling ? "Queuing…" : "Reconcile"}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => {
            void handleDelete();
          }}
        >
          {deleting ? (
            <Loader2 className="size-4 animate-spin" aria-hidden />
          ) : (
            <Trash2 className="size-4" aria-hidden />
          )}
          Delete
        </Button>
      </div>
      {reconcileJobId ? (
        <p className="sm:col-span-2 text-sm text-muted-foreground" role="status">
          Reconcile queued · job{" "}
          <span className="font-mono text-xs text-foreground break-all">{reconcileJobId}</span>
        </p>
      ) : null}
      {error ? (
        <p className="sm:col-span-2 text-sm text-destructive" role="alert">
          {error}
        </p>
      ) : null}
      <EnvironmentRuntimePanel
        projectId={environment.project_id}
        environmentId={environment.id}
      />
      <EnvironmentVarsManager
        projectId={environment.project_id}
        environmentId={environment.id}
        peerEnvironments={peerEnvironments}
      />
      <EnvironmentWebhookManager
        projectId={environment.project_id}
        environmentId={environment.id}
      />
    </form>
  );
}
