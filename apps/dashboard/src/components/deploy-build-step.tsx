import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { buildAsksForService } from "@/lib/deploy-build";
import type { DeploymentBlueprint } from "@/lib/types";

type DeployBuildStepProps = {
  blueprint: DeploymentBlueprint;
  port: string;
  service: string;
  onPortChange: (value: string) => void;
  onServiceChange: (value: string) => void;
  portInvalid: boolean;
  disabled: boolean;
  confirmed: boolean;
};

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="font-mono text-[10px] tracking-[0.16em] text-muted-foreground uppercase">{label}</dt>
      <dd className="mt-1 font-mono text-sm text-foreground">{value}</dd>
    </div>
  );
}

export function DeployBuildStep({
  blueprint,
  port,
  service,
  onPortChange,
  onServiceChange,
  portInvalid,
  disabled,
  confirmed,
}: DeployBuildStepProps) {
  const asksForService = buildAsksForService(blueprint.build);
  const composeFile = blueprint.build.compose_file?.trim() ?? "";

  return (
    <section className="flex flex-col gap-4 rounded-lg border border-border/80 bg-muted/30 p-3" aria-label="Build">
      <dl className="grid gap-3 sm:grid-cols-2">
        <Fact label="Source" value={blueprint.source} />
        <Fact label="Strategy" value={blueprint.build.strategy} />
        {blueprint.build.strategy === "dockerfile" && blueprint.build.dockerfile ? (
          <Fact label="Dockerfile" value={blueprint.build.dockerfile} />
        ) : null}
        {composeFile ? <Fact label="Compose file" value={composeFile} /> : null}
        {confirmed ? <Fact label="Port" value={port} /> : null}
        {confirmed && asksForService ? <Fact label="Service" value={service.trim() || "—"} /> : null}
      </dl>
      {confirmed ? null : (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Label htmlFor="build-port">Port</Label>
            <Input
              id="build-port"
              inputMode="numeric"
              autoComplete="off"
              value={port}
              aria-invalid={portInvalid ? true : undefined}
              disabled={disabled}
              onValueChange={onPortChange}
              className="h-10"
            />
          </div>
          {asksForService ? (
            <div className="flex flex-col gap-2">
              <Label htmlFor="build-service">Service</Label>
              <Input
                id="build-service"
                autoComplete="off"
                value={service}
                disabled={disabled}
                onValueChange={onServiceChange}
                className="h-10"
              />
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}
