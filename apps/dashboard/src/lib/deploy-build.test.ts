import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { applyBuildConfirmation, buildAsksForService, launchBuildFields, parseBuildPort } from "./deploy-build";
import type { BlueprintBuild } from "./types";

function build(overrides: Partial<BlueprintBuild> = {}): BlueprintBuild {
  return {
    strategy: "dockerfile",
    dockerfile: "Dockerfile",
    compose_file: null,
    service: null,
    port: 8000,
    ...overrides,
  };
}

describe("deploy build confirmation", () => {
  it("does not ask for a service when the scan found no Compose file", () => {
    assert.equal(buildAsksForService(build()), false);
    assert.equal(buildAsksForService(build({ strategy: "auto", dockerfile: "" })), false);
  });

  it("asks for a service when the scan found Compose", () => {
    assert.equal(buildAsksForService(build({ strategy: "compose", compose_file: "docker-compose.yaml", service: "web" })), true);
    assert.equal(
      buildAsksForService(build({ strategy: "dockerfile", compose_file: "docker-compose.yaml", service: "portal" })),
      true,
    );
  });

  it("keeps a corrected port and leaves service untouched without Compose", () => {
    const next = applyBuildConfirmation(build({ service: null }), { port: "3000", service: "web" });
    assert.deepEqual(next, build({ port: 3000 }));
  });

  it("keeps a corrected Compose service", () => {
    const detected = build({ strategy: "compose", compose_file: "docker-compose.yaml", service: "web" });
    const next = applyBuildConfirmation(detected, { port: "8080", service: " api " });
    assert.equal(next?.port, 8080);
    assert.equal(next?.service, "api");
  });

  it("sends the confirmed port, and the service only for Compose", () => {
    assert.deepEqual(launchBuildFields(build({ port: 3000, service: "ignored" })), { port: 3000 });
    assert.deepEqual(
      launchBuildFields(build({ strategy: "compose", compose_file: "docker-compose.yaml", service: "api", port: 8080 })),
      { port: 8080, service: "api" },
    );
  });

  it("rejects a port outside 1–65535", () => {
    assert.equal(parseBuildPort(""), null);
    assert.equal(parseBuildPort("0"), null);
    assert.equal(parseBuildPort("65536"), null);
    assert.equal(parseBuildPort("80"), 80);
    assert.equal(applyBuildConfirmation(build(), { port: "0", service: "" }), null);
  });
});
