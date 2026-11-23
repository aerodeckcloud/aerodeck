import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isDeploymentFinished, isDeploymentWaiting } from "./deployment-waiting";

const ACTIVE = "11111111-1111-4111-8111-111111111111";

describe("isDeploymentWaiting", () => {
  it("is true for a queued row held behind another deployment", () => {
    assert.equal(isDeploymentWaiting("queued", ACTIVE), true);
  });

  it("is false once the row is no longer held", () => {
    assert.equal(isDeploymentWaiting("running", null), false);
    assert.equal(isDeploymentWaiting("queued", undefined), false);
  });

  it("is false for a finished row even if the id is still set", () => {
    assert.equal(isDeploymentWaiting("cancelled", ACTIVE), false);
    assert.equal(isDeploymentWaiting("failed", ACTIVE), false);
    assert.equal(isDeploymentWaiting("succeeded", ACTIVE), false);
  });
});

describe("isDeploymentFinished", () => {
  it("is true only for succeeded, failed and cancelled", () => {
    assert.equal(isDeploymentFinished("succeeded"), true);
    assert.equal(isDeploymentFinished("failed"), true);
    assert.equal(isDeploymentFinished("cancelled"), true);
    assert.equal(isDeploymentFinished("queued"), false);
    assert.equal(isDeploymentFinished("running"), false);
  });
});
