import assert from "node:assert/strict";
import test from "node:test";

import { PRODUCT_NAME } from "./brand";

test("product name is Aerodeck Cloud", () => {
  assert.equal(PRODUCT_NAME, "Aerodeck Cloud");
});
