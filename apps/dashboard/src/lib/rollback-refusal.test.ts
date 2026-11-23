import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ControlPlaneError } from "./control-plane";
import { rollbackRefusalResponse } from "./rollback-refusal";

describe("rollbackRefusalResponse", () => {
  it("returns the control-plane 422 detail and code", async () => {
    const response = rollbackRefusalResponse(
      new ControlPlaneError("This deployment's image is no longer retained.", 422, undefined, {
        code: "image_not_retained",
      }),
    );
    assert.ok(response);
    assert.equal(response.status, 422);
    assert.deepEqual(await response.json(), {
      detail: "This deployment's image is no longer retained.",
      code: "image_not_retained",
    });
  });

  it("uses unprocessable when the control plane sent no code", async () => {
    const response = rollbackRefusalResponse(
      new ControlPlaneError("Only succeeded deployments can be rolled back to.", 422),
    );
    assert.ok(response);
    assert.equal((await response.json()).code, "unprocessable");
  });

  it("leaves other failures to the generic handler", () => {
    assert.equal(rollbackRefusalResponse(new ControlPlaneError("missing", 404)), null);
    assert.equal(rollbackRefusalResponse(new Error("boom")), null);
  });
});
