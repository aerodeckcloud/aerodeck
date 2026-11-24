import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { PRODUCT_NAME } from "./brand";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

test("product name is Aerodeck Cloud", () => {
  assert.equal(PRODUCT_NAME, "Aerodeck Cloud");
});

test("shell chrome and page copy use the product name constant", () => {
  const files = [
    "components/public-shell.tsx",
    "components/public-mobile-nav.tsx",
    "components/app-sidebar.tsx",
    "components/overview-console.tsx",
    "app/layout.tsx",
    "app/(public)/layout.tsx",
    "app/(public)/home/page.tsx",
    "app/(public)/about/page.tsx",
    "app/(public)/features/page.tsx",
    "app/(public)/login/page.tsx",
    "app/(public)/register/page.tsx",
    "app/(public)/policy/page.tsx",
    "app/(console)/deploy/page.tsx",
  ];
  for (const relative of files) {
    const text = readFileSync(join(root, relative), "utf8");
    assert.match(text, /PRODUCT_NAME/, `${relative} imports PRODUCT_NAME`);
    assert.equal(text.includes("AeroDeck"), false, `${relative} still says AeroDeck`);
  }
});
