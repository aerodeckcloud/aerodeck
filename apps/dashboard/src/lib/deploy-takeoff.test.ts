import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { TAKEOFF_MS, shouldPlayTakeoff, takeoffHoldMs } from "./deploy-takeoff";

describe("launch takeoff", () => {
  it("plays only when the operator has not asked for reduced motion", () => {
    assert.equal(shouldPlayTakeoff(false), true);
    assert.equal(shouldPlayTakeoff(true), false);
  });

  it("does not wait on the animation timeline when motion is reduced", () => {
    assert.equal(takeoffHoldMs(0, true), 0);
    assert.equal(takeoffHoldMs(TAKEOFF_MS, true), 0);
    assert.equal(takeoffHoldMs(0, false), TAKEOFF_MS);
    assert.equal(takeoffHoldMs(TAKEOFF_MS, false), 0);
    assert.equal(takeoffHoldMs(400, false), TAKEOFF_MS - 400);
  });
});
