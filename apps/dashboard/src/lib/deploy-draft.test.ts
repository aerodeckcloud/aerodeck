import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  countLaunchVariables,
  deployStepPath,
  deploymentHref,
  launchReview,
  parseSecurityReport,
  securityNeedsAcknowledgement,
  stepAfterResources,
  stepBeforeTarget,
} from "./deploy-draft";

describe("deploy draft routes", () => {
  it("gives each confirmed step its own page", () => {
    assert.equal(deployStepPath("draft-1", "build"), "/deploy/draft-1/build");
    assert.equal(deployStepPath("draft-1", "security"), "/deploy/draft-1/security");
    assert.equal(deployStepPath("draft-1", "resources"), "/deploy/draft-1/databases");
    assert.equal(deployStepPath("draft-1", "variables"), "/deploy/draft-1/variables");
    assert.equal(deployStepPath("draft-1", "target"), "/deploy/draft-1/target");
    assert.equal(deployStepPath("draft-1", "launch"), "/deploy/draft-1/launch");
  });

  it("asks for an acknowledgement only when the scan found findings or did not finish", () => {
    assert.equal(securityNeedsAcknowledgement("clear"), false);
    assert.equal(securityNeedsAcknowledgement("no_manifest"), false);
    assert.equal(securityNeedsAcknowledgement("findings"), true);
    assert.equal(securityNeedsAcknowledgement("incomplete"), true);
    const report = parseSecurityReport({
      outcome: "findings",
      acknowledgement: null,
      findings: [
        {
          severity: "high",
          package: "lodash",
          advisory_id: "GHSA-test",
          summary: "Prototype pollution in lodash\nRequest a payload",
        },
      ],
    });
    assert.equal(report.findings[0]?.summary, "Prototype pollution in lodash Request a payload");
    assert.equal(report.findings[0]?.summary.includes("\n"), false);
  });

  it("puts the target after variables, or after databases when the scan found no env keys", () => {
    assert.equal(stepAfterResources(2), "variables");
    assert.equal(stepAfterResources(0), "target");
    assert.equal(stepBeforeTarget(2), "variables");
    assert.equal(stepBeforeTarget(0), "resources");
  });

  it("reviews the scan and the operator's edits", () => {
    assert.equal(countLaunchVariables({ PORT: "3000", EMPTY: "  " }, ["TOKEN"], ["EXTRA"]), 3);
    const review = launchReview({
      repository: "https://github.com/acme/demo",
      source: "docker-compose",
      service: "portal",
      port: 8080,
      selectedAddons: ["postgres"],
      variableCount: 3,
      environmentLabel: "demo/dev (dev, ams3)",
    });
    assert.equal(review.repository, "https://github.com/acme/demo");
    assert.equal(review.buildSource, "docker-compose · portal · port 8080");
    assert.equal(review.addons, "postgres");
    assert.equal(review.variableCount, 3);
    assert.equal(review.environment, "demo/dev (dev, ams3)");
    assert.equal(
      launchReview({
        repository: "https://github.com/acme/demo",
        source: "dockerfile",
        service: null,
        port: 8000,
        selectedAddons: [],
        variableCount: 0,
        environmentLabel: null,
      }).environment,
      "No environment selected",
    );
  });

  it("sends a draft back to its page and a launched row to the deployment", () => {
    assert.equal(
      deploymentHref({ id: "draft-1", status: "draft", draft_step: "resources" }),
      "/deploy/draft-1/databases",
    );
    assert.equal(
      deploymentHref({ id: "draft-1", status: "draft", draft_step: "target" }),
      "/deploy/draft-1/target",
    );
    assert.equal(deploymentHref({ id: "live-1", status: "running" }), "/deployments/live-1");
  });
});
