"use client";

import { Loader2, Rocket, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import {
  DeploymentBlueprintFields,
  parseDotenvText,
} from "@/components/deployment-blueprint-fields";
import { DeployBuildStep } from "@/components/deploy-build-step";
import { EnvironmentSelect } from "@/components/environment-select";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  createDeployment,
  fetchEnvironmentVars,
  fetchLogSource,
  inspectDeploymentRepo,
  ProvisionEnqueueFailed,
  upsertEnvironmentVars,
} from "@/lib/api";
import { applyBuildConfirmation } from "@/lib/deploy-build";
import { environmentOptionKey } from "@/lib/environment-option";
import { parseGithubRepo } from "@/lib/github";
import type { AddonType, DeploymentBlueprint } from "@/lib/types";
import { useDeployEnvironments } from "@/lib/use-deploy-environments";

export function NewDeploymentForm({ controlPlane = false }: { controlPlane?: boolean }) {
  const router = useRouter();
  const [effectiveControlPlane, setEffectiveControlPlane] = useState(controlPlane);
  const isControlPlane = controlPlane || effectiveControlPlane;

  useEffect(() => {
    if (!controlPlane) {
      fetchLogSource().then((source) => { if (source === "control-plane") setEffectiveControlPlane(true); }).catch(() => {});
    }
  }, [controlPlane]);

  const [repoUrl, setRepoUrl] = useState("");
  const [envVarsText, setEnvVarsText] = useState("");
  const [blueprint, setBlueprint] = useState<DeploymentBlueprint | null>(null);
  const [step, setStep] = useState<"repository" | "build" | "launch">("repository");
  const [portDraft, setPortDraft] = useState("");
  const [serviceDraft, setServiceDraft] = useState("");
  const [portInvalid, setPortInvalid] = useState(false);
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [selectedAddons, setSelectedAddons] = useState<AddonType[]>([]);
  const [inspecting, setInspecting] = useState(false);

  const { environmentKey, setEnvironmentKey, environments, environmentsError, loadingEnvironments, reload } = useDeployEnvironments(isControlPlane);
  const [savedEnvKeys, setSavedEnvKeys] = useState<Set<string>>(() => new Set());
  const [persistEnvVars, setPersistEnvVars] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [repoInvalid, setRepoInvalid] = useState(false);
  const [environmentRequired, setEnvironmentRequired] = useState(false);
  const [enqueueBlocked, setEnqueueBlocked] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!isControlPlane) {
      return;
    }
    const selected = environments.find((row) => environmentOptionKey(row) === environmentKey);
    if (!selected) {
      setSavedEnvKeys(new Set());
      return;
    }
    let cancelled = false;
    void fetchEnvironmentVars(selected.project_id, selected.id)
      .then((rows) => {
        if (!cancelled) {
          setSavedEnvKeys(new Set(rows.map((r) => r.key)));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSavedEnvKeys(new Set());
        }
      });
    return () => {
      cancelled = true;
    };
  }, [isControlPlane, environments, environmentKey]);

  async function handleInspect() {
    setError(null);
    setRepoInvalid(false);
    if (!parseGithubRepo(repoUrl)) {
      setRepoInvalid(true);
      setError("Enter a GitHub URL like https://github.com/org/repo");
      return;
    }
    setInspecting(true);
    try {
      const bp = await inspectDeploymentRepo(repoUrl.trim());
      setBlueprint(bp);
      setPortDraft(String(bp.build.port));
      setServiceDraft(bp.build.service ?? "");
      setPortInvalid(false);
      setStep("build");
      const initialFields: Record<string, string> = {};
      for (const item of bp.env) {
        initialFields[item.key] = item.default ?? "";
      }
      setFieldValues(initialFields);
      setSelectedAddons(bp.addons.map((a) => a.type));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to inspect repository.");
    } finally {
      setInspecting(false);
    }
  }

  function toggleAddon(addon: AddonType) {
    setSelectedAddons((prev) =>
      prev.includes(addon) ? prev.filter((a) => a !== addon) : [...prev, addon],
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (enqueueBlocked) {
      return;
    }
    setError(null);
    setRepoInvalid(false);
    setEnvironmentRequired(false);

    if (!parseGithubRepo(repoUrl)) {
      setRepoInvalid(true);
      setError("Enter a GitHub URL like https://github.com/org/repo");
      return;
    }

    if (isControlPlane && !blueprint) {
      await handleInspect();
      return;
    }

    if (isControlPlane && blueprint && step === "build") {
      const confirmed = applyBuildConfirmation(blueprint.build, { port: portDraft, service: serviceDraft });
      if (!confirmed) {
        setPortInvalid(true);
        setError("Enter a port from 1 to 65535.");
        return;
      }
      setBlueprint({ ...blueprint, build: confirmed });
      setPortDraft(String(confirmed.port));
      setServiceDraft(confirmed.service ?? "");
      setStep("launch");
      return;
    }

    if (blueprint) {
      const missing = blueprint.env.find(
        (item) =>
          item.required &&
          !savedEnvKeys.has(item.key) &&
          (fieldValues[item.key] ?? "").trim().length === 0,
      );
      if (missing) {
        setError(`Required environment variable ${missing.key} is missing.`);
        return;
      }
    }

    let target: { projectId: string; environmentId: string; envVars?: Record<string, string>; addons?: AddonType[] } | undefined;
    if (isControlPlane) {
      const selected = environments.find((row) => environmentOptionKey(row) === environmentKey);
      if (!selected) {
        setEnvironmentRequired(true);
        setError("Select an environment to provision.");
        return;
      }
      const mergedEnv: Record<string, string> = {};
      for (const [k, v] of Object.entries(fieldValues)) {
        if (v.trim().length > 0) {
          mergedEnv[k] = v.trim();
        }
      }
      Object.assign(mergedEnv, parseDotenvText(envVarsText));
      target = {
        projectId: selected.project_id,
        environmentId: selected.id,
        envVars: Object.keys(mergedEnv).length > 0 ? mergedEnv : undefined,
        addons: selectedAddons.length > 0 ? selectedAddons : undefined,
      };
    }

    setSubmitting(true);
    try {
      if (isControlPlane && target && persistEnvVars && target.envVars) {
        const items = Object.entries(target.envVars).map(([key, value]) => ({
          key,
          value,
          is_secret: true,
        }));
        if (items.length > 0) {
          await upsertEnvironmentVars(target.projectId, target.environmentId, items);
        }
      }
      const deployment = isControlPlane && target ? await createDeployment(repoUrl, true, target) : await createDeployment(repoUrl, false);
      router.push(`/deployments/${deployment.id}`);
    } catch (cause) {
      if (cause instanceof ProvisionEnqueueFailed) {
        setEnqueueBlocked(true);
        setError(`${cause.message} Do not Deploy again; that row may already be marked failed.`);
        return;
      }
      setError(cause instanceof Error ? cause.message : "Deploy failed.");
    } finally {
      setSubmitting(false);
    }
  }

  const canSubmit =
    !submitting &&
    !inspecting &&
    !enqueueBlocked &&
    repoUrl.trim().length > 0 &&
    (!isControlPlane || (environmentKey.length > 0 && !loadingEnvironments));

  return (
    <Card className="overflow-hidden ring-primary/15">
      <div className="h-1 bg-gradient-to-r from-primary via-runway to-primary/40" />
      <CardHeader className="border-b">
        <CardTitle>
          {step === "build" ? "Build" : step === "launch" ? "Deploy" : "Repository"}
        </CardTitle>
        <CardDescription>
          {step === "build"
            ? "Detected from the scan. Change the port, and the Compose service when this repository has one."
            : step === "launch"
              ? "Environment, addons, and variables follow the build you confirmed."
              : isControlPlane
                ? "The GitHub repository to scan. A successful scan opens the build before anything else."
                : "HTTPS GitHub URL. Validated in the browser, then POSTed to /api/mock/v1/deployments."}
        </CardDescription>
      </CardHeader>
      <form onSubmit={handleSubmit}>
        <CardContent className="flex flex-col gap-4 pt-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="github-url">GitHub URL</Label>
            <div className="flex gap-2">
              <Input
                id="github-url"
                type="url"
                inputMode="url"
                autoComplete="url"
                placeholder="https://github.com/mangatohjunior/aerodeck"
                value={repoUrl}
                aria-invalid={repoInvalid ? true : undefined}
                disabled={submitting || inspecting || enqueueBlocked}
                onValueChange={(value) => {
                  setRepoUrl(value);
                  if (blueprint) {
                    setBlueprint(null);
                    setStep("repository");
                    setPortDraft("");
                    setServiceDraft("");
                    setPortInvalid(false);
                    setFieldValues({});
                    setSelectedAddons([]);
                  }
                  if (repoInvalid) {
                    setRepoInvalid(false);
                  }
                  if (error && !enqueueBlocked) {
                    setError(null);
                  }
                }}
                className="h-10 flex-1"
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Reads <code>aerodeck.yaml</code>, <code>docker-compose.yaml</code>,{" "}
              <code>Dockerfile</code>, and <code>.env.example</code>.
            </p>
          </div>
          {isControlPlane && blueprint && step !== "repository" ? (
            <DeployBuildStep
              blueprint={blueprint}
              port={portDraft}
              service={serviceDraft}
              onPortChange={(value) => {
                setPortDraft(value);
                if (portInvalid) {
                  setPortInvalid(false);
                }
              }}
              onServiceChange={setServiceDraft}
              portInvalid={portInvalid}
              disabled={submitting || inspecting || enqueueBlocked}
              confirmed={step === "launch"}
            />
          ) : null}
          {isControlPlane && blueprint && step === "launch" ? (
            <>
              <EnvironmentSelect
                environments={environments}
                environmentKey={environmentKey}
                onEnvironmentKeyChange={(key) => {
                  setEnvironmentKey(key);
                  if (environmentRequired) {
                    setEnvironmentRequired(false);
                  }
                  if (error && !enqueueBlocked) {
                    setError(null);
                  }
                }}
                environmentsError={environmentsError}
                loadingEnvironments={loadingEnvironments}
                invalid={!!environmentsError || environmentRequired}
                disabled={submitting || enqueueBlocked}
                onRetry={reload}
              />
              <DeploymentBlueprintFields
                blueprint={blueprint}
                fieldValues={fieldValues}
                onFieldChange={(key, value) =>
                  setFieldValues((prev) => ({ ...prev, [key]: value }))
                }
                selectedAddons={selectedAddons}
                onToggleAddon={toggleAddon}
                extraEnvText={envVarsText}
                onExtraEnvTextChange={setEnvVarsText}
                savedEnvKeys={savedEnvKeys}
                disabled={submitting || enqueueBlocked}
              />
              <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={persistEnvVars}
                  disabled={submitting || enqueueBlocked}
                  onChange={(e) => setPersistEnvVars(e.target.checked)}
                  className="size-3.5 accent-primary"
                />
                Save entered variables encrypted to this environment for future redeploys
              </label>
            </>
          ) : null}
          {error ? (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </CardContent>
        <CardFooter className="justify-end gap-2">
          {step === "launch" ? (
            <Button type="button" variant="outline" size="lg" onClick={() => setStep("build")}>
              Change build
            </Button>
          ) : null}
          {isControlPlane && !blueprint ? (
            <Button
              type="submit"
              size="lg"
              disabled={submitting || inspecting || enqueueBlocked || repoUrl.trim().length === 0}
            >
              {inspecting ? <Loader2 data-icon="inline-start" className="animate-spin" /> : <Search data-icon="inline-start" />}
              {inspecting ? "Scanning…" : "Scan repository"}
            </Button>
          ) : isControlPlane && step === "build" ? (
            <Button type="submit" size="lg" disabled={submitting || inspecting || enqueueBlocked}>
              Continue
            </Button>
          ) : (
            <Button type="submit" size="lg" disabled={!canSubmit}>
              {submitting ? <Loader2 data-icon="inline-start" className="animate-spin" /> : <Rocket data-icon="inline-start" />}
              {submitting ? "Deploying…" : "Deploy"}
            </Button>
          )}
        </CardFooter>
      </form>
    </Card>
  );
}
