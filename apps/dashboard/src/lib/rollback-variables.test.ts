import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { rollbackConfirmMessage, rollbackVariableDiff } from "./rollback-variables";

describe("rollbackVariableDiff", () => {
  it("returns null when there is no snapshot", () => {
    assert.equal(rollbackVariableDiff(null, ["A"]), null);
    assert.equal(rollbackVariableDiff(undefined, ["A"]), null);
  });

  it("lists names that would be added, removed, or restored", () => {
    assert.deepEqual(rollbackVariableDiff(["A", "B"], ["B", "C"]), {
      added: ["A"],
      removed: ["C"],
      restored: ["B"],
    });
  });
});

describe("rollbackConfirmMessage", () => {
  it("explains image-only rollback when there is no snapshot", () => {
    const message = rollbackConfirmMessage({ snapshotNames: null, currentNames: [] });
    assert.match(message, /no saved variables/);
    assert.doesNotMatch(message, /secret|password|token=/i);
  });

  it("lists names only when the current set loaded", () => {
    const message = rollbackConfirmMessage({
      snapshotNames: ["NEW_KEY", "SHARED"],
      currentNames: ["OLD_KEY", "SHARED"],
    });
    assert.match(message, /Add: NEW_KEY/);
    assert.match(message, /Remove: OLD_KEY/);
    assert.match(message, /Restore values for: SHARED/);
    assert.match(message, /Values are never shown/);
    assert.doesNotMatch(message, /restore_variables/);
  });

  it("does not pretend the environment is empty when current vars failed to load", () => {
    const message = rollbackConfirmMessage({
      snapshotNames: ["A", "B"],
      currentNames: null,
    });
    assert.match(message, /Saved variable names: A, B/);
    assert.match(message, /Could not load the current environment variables/);
    assert.doesNotMatch(message, /^Add:/m);
    assert.doesNotMatch(message, /Remove:/);
  });
});
