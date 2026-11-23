import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isAwaitingApproval, isDeploymentFinished, isDeploymentWaiting } from "./deployment-waiting";

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

describe("isAwaitingApproval", () => {
  it("is true while a queued deploy has been asked for and not approved", () => {
    assert.equal(
      isAwaitingApproval({
        status: "queued",
        approval_required_at: "2026-11-23T00:00:00.000Z",
        approved_at: null,
      }),
      true,
    );
  });

  it("is false after an owner approves, and for a normal queued deploy", () => {
    assert.equal(
      isAwaitingApproval({
        status: "queued",
        approval_required_at: "2026-11-23T00:00:00.000Z",
        approved_at: "2026-11-23T00:01:00.000Z",
      }),
      false,
    );
    assert.equal(isAwaitingApproval({ status: "queued" }), false);
    assert.equal(
      isAwaitingApproval({
        status: "running",
        approval_required_at: "2026-11-23T00:00:00.000Z",
      }),
      false,
    );
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
