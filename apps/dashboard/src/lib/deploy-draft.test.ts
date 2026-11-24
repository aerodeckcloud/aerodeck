import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { deployStepPath, deploymentHref } from "./deploy-draft";

describe("deploy draft routes", () => {
  it("gives each confirmed step its own page", () => {
    assert.equal(deployStepPath("draft-1", "build"), "/deploy/draft-1/build");
    assert.equal(deployStepPath("draft-1", "resources"), "/deploy/draft-1/databases");
    assert.equal(deployStepPath("draft-1", "launch"), "/deploy/draft-1/launch");
  });

  it("sends a draft back to its page and a launched row to the deployment", () => {
    assert.equal(
      deploymentHref({ id: "draft-1", status: "draft", draft_step: "resources" }),
      "/deploy/draft-1/databases",
    );
    assert.equal(deploymentHref({ id: "live-1", status: "running" }), "/deployments/live-1");
  });
});
