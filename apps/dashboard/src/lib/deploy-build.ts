import type { BlueprintBuild } from "@/lib/types";

/** A Compose file, or a compose strategy, is the only reason to ask for a service. */
export function buildAsksForService(build: BlueprintBuild): boolean {
  return build.strategy === "compose" || Boolean(build.compose_file?.trim());
}

/** Port the inspector returns, or null when the operator's correction is not 1–65535. */
export function parseBuildPort(value: string): number | null {
  const text = value.trim();
  if (!/^\d+$/.test(text)) {
    return null;
  }
  const port = Number(text);
  if (port < 1 || port > 65535) {
    return null;
  }
  return port;
}

/**
 * Copy the scan's build and apply the port correction.
 * Service is applied only when this build asks for one.
 */
export function applyBuildConfirmation(
  build: BlueprintBuild,
  input: { port: string; service: string },
): BlueprintBuild | null {
  const port = parseBuildPort(input.port);
  if (port === null) {
    return null;
  }
  if (!buildAsksForService(build)) {
    return { ...build, port };
  }
  const service = input.service.trim();
  return { ...build, port, service: service.length > 0 ? service : null };
}

/** Fields the confirmed build adds to the existing create body. */
export function launchBuildFields(build: BlueprintBuild): { port: number; service?: string } {
  if (!buildAsksForService(build)) {
    return { port: build.port };
  }
  const service = build.service?.trim();
  return service ? { port: build.port, service } : { port: build.port };
}
