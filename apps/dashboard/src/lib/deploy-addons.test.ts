import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { knownDetectedAddons, offeredAddonTypes } from "./deploy-addons";

describe("deploy addon offer", () => {
  it("keeps detected postgres, redis, and mongodb and drops anything else", () => {
    assert.deepEqual(knownDetectedAddons(["postgres", "mysql", "redis", "postgres", "mongodb"]), [
      "postgres",
      "redis",
      "mongodb",
    ]);
  });

  it("offers only the databases the scan found", () => {
    assert.deepEqual(offeredAddonTypes(["postgres"]), ["postgres"]);
  });

  it("offers the three accepted types when the scan found none", () => {
    assert.deepEqual(offeredAddonTypes([]), ["postgres", "redis", "mongodb"]);
  });
});
