"use client";

import { Loader2, Rocket, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import {
  DeploymentBlueprintFields,
  parseDotenvText,
} from "@/components/deployment-blueprint-fields";
import { DeployBuildStep } from "@/components/deploy-build-step";
import { DeployResourcesStep } from "@/components/deploy-resources-step";
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
  createDeployDraft,
  fetchEnvironmentVars,
  fetchLogSource,
  inspectDeploymentRepo,
  launchDeployDraft,
  ProvisionEnqueueFailed,
  readDeployDraft,
  updateDeployDraft,
  upsertEnvironmentVars,
} from "@/lib/api";
import { knownDetectedAddons } from "@/lib/deploy-addons";
import { deployStepPath, stepAfterResources, stepBeforeLaunch, type DeployDraftPlan, type DeployRouteStep } from "@/lib/deploy-draft";
import { applyBuildConfirmation, launchBuildFields } from "@/lib/deploy-build";
import { environmentOptionKey } from "@/lib/environment-option";
import { parseGithubRepo } from "@/lib/github";
import type { AddonType, DeploymentBlueprint } from "@/lib/types";
import { useDeployEnvironments } from "@/lib/use-deploy-environments";

export function NewDeploymentForm({
  controlPlane = false,
  routeStep = "repository",
  draftId,
}: {
  controlPlane?: boolean;
  routeStep?: DeployRouteStep;
  draftId?: string;
}) {
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
  const [step, setStep] = useState<DeployRouteStep>(routeStep);
  const [portDraft, setPortDraft] = useState("");
  const [serviceDraft, setServiceDraft] = useState("");
  const [portInvalid, setPortInvalid] = useState(false);
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [redactedKeys, setRedactedKeys] = useState<Set<string>>(() => new Set());
  const [draftReady, setDraftReady] = useState(draftId === undefined);
  const [detectedAddons, setDetectedAddons] = useState<AddonType[]>([]);
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

  function planFor(nextStep: DeployDraftPlan["step"], source: DeploymentBlueprint): DeployDraftPlan {
    return {
      step: nextStep,
      blueprint: source,
      detected_addons: detectedAddons,
      selected_addons: selectedAddons,
      extra_env: envVarsText,
      variables: source.env.map((item) => {
        const value = fieldValues[item.key] ?? "";
        return {
          key: item.key,
          value,
          secret: item.secret,
          redacted: item.secret && value.length === 0 && redactedKeys.has(item.key),
        };
      }),
    };
  }

  useEffect(() => {
    if (!draftId) {
      return;
    }
    let cancelled = false;
    void readDeployDraft(draftId)
      .then((saved) => {
        if (cancelled) {
          return;
        }
        const plan = saved.plan;
        setRepoUrl(plan.blueprint.repo_url);
        setBlueprint(plan.blueprint);
        setPortDraft(String(plan.blueprint.build.port));
        setServiceDraft(plan.blueprint.build.service ?? "");
        setDetectedAddons(plan.detected_addons);
        setSelectedAddons(plan.selected_addons);
        setEnvVarsText(plan.extra_env);
        const fields: Record<string, string> = {};
        const redacted = new Set<string>();
        for (const item of plan.variables) {
          fields[item.key] = item.value;
          if (item.redacted) {
            redacted.add(item.key);
          }
        }
        setFieldValues(fields);
        setRedactedKeys(redacted);
        setDraftReady(true);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "Could not open this draft.");
          setDraftReady(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [draftId]);

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
      const detected = knownDetectedAddons(bp.addons.map((addon) => addon.type));
      setDetectedAddons(detected);
      setSelectedAddons(detected);
      if (isControlPlane) {
        const saved = await createDeployDraft(repoUrl.trim(), {
          step: "build",
          blueprint: bp,
          detected_addons: detected,
          selected_addons: detected,
          extra_env: "",
          variables: bp.env.map((item) => ({
            key: item.key,
            value: item.default ?? "",
            secret: item.secret,
            redacted: false,
          })),
        });
        router.push(deployStepPath(saved.deployment.id, "build"));
      }
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
      const confirmedBlueprint = { ...blueprint, build: confirmed };
      setBlueprint(confirmedBlueprint);
      setPortDraft(String(confirmed.port));
      setServiceDraft(confirmed.service ?? "");
      if (draftId) {
        try {
          await updateDeployDraft(draftId, planFor("resources", confirmedBlueprint));
          router.push(deployStepPath(draftId, "resources"));
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "Could not save this step.");
        }
        return;
      }
      setStep("resources");
      return;
    }

    if (isControlPlane && blueprint && step === "resources") {
      const next = stepAfterResources(blueprint.env.length);
      if (draftId) {
        try {
          await updateDeployDraft(draftId, planFor(next, blueprint));
          router.push(deployStepPath(draftId, next));
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "Could not save this step.");
        }
        return;
      }
      setStep(next);
      return;
    }

    if (isControlPlane && blueprint && step === "variables") {
      if (draftId) {
        try {
          await updateDeployDraft(draftId, planFor("launch", blueprint));
          router.push(deployStepPath(draftId, "launch"));
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : "Could not save this step.");
        }
        return;
      }
      setStep("launch");
      return;
    }

    if (blueprint) {
      const missing = blueprint.env.find(
        (item) =>
          item.required &&
          !savedEnvKeys.has(item.key) &&
          !redactedKeys.has(item.key) &&
          (fieldValues[item.key] ?? "").trim().length === 0,
      );
      if (missing) {
        setError(`Required environment variable ${missing.key} is missing.`);
        return;
      }
    }

    let target: {
      projectId: string;
      environmentId: string;
      envVars?: Record<string, string>;
      addons?: AddonType[];
      port?: number;
      service?: string;
    } | undefined;
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
      const confirmedBuild = blueprint ? launchBuildFields(blueprint.build) : undefined;
      target = {
        projectId: selected.project_id,
        environmentId: selected.id,
        envVars: Object.keys(mergedEnv).length > 0 ? mergedEnv : undefined,
        addons: selectedAddons.length > 0 ? selectedAddons : undefined,
        port: confirmedBuild?.port,
        service: confirmedBuild?.service,
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
      const deployment =
        isControlPlane && target && draftId && blueprint
          ? await launchDeployDraft(draftId, planFor("launch", blueprint), {
              projectId: target.projectId,
              environmentId: target.environmentId,
            })
          : isControlPlane && target
            ? await createDeployment(repoUrl, true, target)
            : await createDeployment(repoUrl, false);
      router.push(`/deployments/${deployment.id}`);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Deploy failed.";
      if (isControlPlane && draftId) {
        setError(message);
        return;
      }
      if (cause instanceof ProvisionEnqueueFailed) {
        setEnqueueBlocked(true);
        setError(`${cause.message} Do not Deploy again; that row may already be marked failed.`);
        return;
      }
      setError(message);
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

  if (draftId && !draftReady) {
    return <p className="text-sm text-muted-foreground">Loading draft…</p>;
  }

  return (
    <Card className="overflow-hidden ring-primary/15">
      <div className="h-1 bg-gradient-to-r from-primary via-runway to-primary/40" />
      <CardHeader className="border-b">
        <CardTitle>
          {step === "build" ? "Build" : step === "resources" ? "Databases" : step === "variables" ? "Variables" : step === "launch" ? "Deploy" : "Repository"}
        </CardTitle>
        <CardDescription>
          {step === "build"
            ? "Detected from the scan. Change the port, and the Compose service when this repository has one."
            : step === "resources"
              ? "Databases from the scan. A scan that found none still lets you add Postgres, Redis, or MongoDB."
              : step === "variables"
                ? "One field for each key the scan found. A required value that is still empty blocks launch."
              : step === "launch"
              ? blueprint && blueprint.env.length > 0
                ? "Choose the environment. The variables you entered are saved on this draft."
                : "Environment and variables follow the databases you confirmed."
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
                    if (draftId) {
                      router.push("/deploy");
                    } else {
                      setStep("repository");
                    }
                    setPortDraft("");
                    setServiceDraft("");
                    setPortInvalid(false);
                    setFieldValues({});
                    setDetectedAddons([]);
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
              confirmed={step === "resources" || step === "variables" || step === "launch"}
            />
          ) : null}
          {isControlPlane && blueprint && step === "resources" ? (
            <DeployResourcesStep
              detected={detectedAddons}
              selected={selectedAddons}
              onToggle={toggleAddon}
              disabled={submitting || inspecting || enqueueBlocked}
            />
          ) : null}
          {isControlPlane && blueprint && step === "variables" ? (
            <DeploymentBlueprintFields
              blueprint={blueprint}
              fieldValues={fieldValues}
              onFieldChange={(key, value) =>
                setFieldValues((prev) => ({ ...prev, [key]: value }))
              }
              extraEnvText={envVarsText}
              onExtraEnvTextChange={setEnvVarsText}
              savedEnvKeys={savedEnvKeys}
              disabled={submitting || enqueueBlocked}
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
              {blueprint.env.length === 0 ? (
                <DeploymentBlueprintFields
                  blueprint={blueprint}
                  fieldValues={fieldValues}
                  onFieldChange={(key, value) =>
                    setFieldValues((prev) => ({ ...prev, [key]: value }))
                  }
                  extraEnvText={envVarsText}
                  onExtraEnvTextChange={setEnvVarsText}
                  savedEnvKeys={savedEnvKeys}
                  disabled={submitting || enqueueBlocked}
                />
              ) : null}
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
          {step === "resources" ? (
            <Button
              type="button"
              variant="outline"
              size="lg"
              onClick={() => {
                if (draftId && blueprint) {
                  void updateDeployDraft(draftId, planFor("build", blueprint)).then(() => {
                    router.push(deployStepPath(draftId, "build"));
                  });
                  return;
                }
                setStep("build");
              }}
            >
              Change build
            </Button>
          ) : null}
          {step === "variables" ? (
            <Button
              type="button"
              variant="outline"
              size="lg"
              onClick={() => {
                if (draftId && blueprint) {
                  void updateDeployDraft(draftId, planFor("resources", blueprint))
                    .then(() => {
                      router.push(deployStepPath(draftId, "resources"));
                    })
                    .catch((cause: unknown) => {
                      setError(cause instanceof Error ? cause.message : "Could not save this step.");
                    });
                  return;
                }
                setStep("resources");
              }}
            >
              Change databases
            </Button>
          ) : null}
          {step === "launch" ? (
            <Button
              type="button"
              variant="outline"
              size="lg"
              onClick={() => {
                if (!blueprint) {
                  setStep("resources");
                  return;
                }
                const previous = stepBeforeLaunch(blueprint.env.length);
                if (draftId) {
                  void updateDeployDraft(draftId, planFor(previous, blueprint))
                    .then(() => {
                      router.push(deployStepPath(draftId, previous));
                    })
                    .catch((cause: unknown) => {
                      setError(cause instanceof Error ? cause.message : "Could not save this step.");
                    });
                  return;
                }
                setStep(previous);
              }}
            >
              {blueprint && blueprint.env.length > 0 ? "Change variables" : "Change databases"}
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
          ) : isControlPlane && (step === "build" || step === "resources" || step === "variables") ? (
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
