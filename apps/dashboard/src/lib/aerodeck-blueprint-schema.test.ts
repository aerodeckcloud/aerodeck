import assert from "node:assert/strict";
import test from "node:test";

import {
  BLUEPRINT_EXAMPLE_YAML,
  BLUEPRINT_SCHEMA_MARKDOWN,
  BLUEPRINT_SCHEMA_PATH,
  agentBlueprintPrompt,
} from "./aerodeck-blueprint-schema";

test("schema path is the public markdown URL", () => {
  assert.equal(BLUEPRINT_SCHEMA_PATH, "/docs/aerodeck-blueprint-schema.md");
});

test("schema markdown names version 1 and agent instructions", () => {
  assert.match(BLUEPRINT_SCHEMA_MARKDOWN, /version: 1/);
  assert.match(BLUEPRINT_SCHEMA_MARKDOWN, /## Agent instructions/);
  assert.match(BLUEPRINT_SCHEMA_MARKDOWN, /aerodeck\.yaml/);
  assert.match(BLUEPRINT_EXAMPLE_YAML, /strategy: dockerfile/);
});

test("agent prompt points at the schema URL", () => {
  const prompt = agentBlueprintPrompt("https://dev.aerodeckcloud.com/docs/aerodeck-blueprint-schema.md");
  assert.match(prompt, /https:\/\/dev\.aerodeckcloud\.com\/docs\/aerodeck-blueprint-schema\.md/);
  assert.match(prompt, /aerodeck\.yaml/);
});
