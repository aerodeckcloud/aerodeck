import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  BLUEPRINT_SCHEMA_PATH,
  agentBlueprintPrompt,
  extractSchemaTables,
  extractYamlExample,
  readBlueprintSchemaMarkdown,
  resolveBlueprintSchemaPath,
} from "./aerodeck-blueprint-schema";

const canonicalPath = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "..",
  "docs",
  "aerodeck-blueprint-schema.md",
);

test("schema path is the public markdown URL", () => {
  assert.equal(BLUEPRINT_SCHEMA_PATH, "/docs/aerodeck-blueprint-schema.md");
});

test("published schema body matches docs/aerodeck-blueprint-schema.md", () => {
  const canonical = readFileSync(canonicalPath, "utf8");
  assert.equal(resolveBlueprintSchemaPath(), canonicalPath);
  assert.equal(readBlueprintSchemaMarkdown(), canonical);
  assert.match(canonical, /## Agent instructions/);
  assert.match(canonical, /mongodb/);
});

test("yaml example and schema tables come from the canonical markdown", () => {
  const markdown = readBlueprintSchemaMarkdown();
  const example = extractYamlExample(markdown);
  assert.match(example, /strategy: dockerfile/);
  const tables = extractSchemaTables(markdown);
  assert.equal(tables.length >= 4, true);
  assert.equal(
    tables.some((table) => table.heading.includes("Root Fields")),
    true,
  );
  assert.equal(
    tables.some((table) => table.rows.some((row) => row.join(" ").includes("mongodb"))),
    true,
  );
});

test("agent prompt points at the schema URL", () => {
  const prompt = agentBlueprintPrompt(
    "https://dev.aerodeckcloud.com/docs/aerodeck-blueprint-schema.md",
  );
  assert.match(
    prompt,
    /https:\/\/dev\.aerodeckcloud\.com\/docs\/aerodeck-blueprint-schema\.md/,
  );
  assert.match(prompt, /aerodeck\.yaml/);
});
