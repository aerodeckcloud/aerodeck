import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  PUBLIC_HOME_PATH,
  PUBLIC_PATH_SEGMENTS,
  LOGIN_PATH,
  anonymousEntryPath,
  buildProxyMatcher,
  pathnameMatchesProxy,
} from "@/lib/public-paths";

function matcherInProxySource(): string {
  const source = readFileSync(new URL("../proxy.ts", import.meta.url), "utf8");
  const start = source.indexOf('"/((?!');
  const end = source.indexOf('"', start + 1);
  assert.ok(start !== -1 && end !== -1, "proxy.ts must keep a literal matcher");
  return JSON.parse(`"${source.slice(start + 1, end)}"`);
}

describe("public path proxy matcher", () => {
  const matcher = matcherInProxySource();

  it("builds the matcher Next reads from the public path segments", () => {
    assert.ok(PUBLIC_PATH_SEGMENTS.includes("home"));
    assert.ok(PUBLIC_PATH_SEGMENTS.includes("features"));
    assert.equal(matcher, buildProxyMatcher());
  });

  it("leaves /home public and still matches the console root", () => {
    assert.ok(PUBLIC_PATH_SEGMENTS.includes("docs"));
    assert.equal(pathnameMatchesProxy("/home", matcher), false);
    assert.equal(pathnameMatchesProxy("/docs", matcher), false);
    assert.equal(pathnameMatchesProxy("/docs/aerodeck-blueprint-schema.md", matcher), false);
    assert.equal(pathnameMatchesProxy("/features", matcher), false);
    assert.equal(pathnameMatchesProxy("/", matcher), true);
    assert.equal(pathnameMatchesProxy("/projects", matcher), true);
  });

  it("sends an anonymous root visit to home and other console paths to login", () => {
    assert.equal(anonymousEntryPath("/"), PUBLIC_HOME_PATH);
    assert.equal(anonymousEntryPath("/projects"), LOGIN_PATH);
    assert.equal(anonymousEntryPath("/deploy"), LOGIN_PATH);
  });
});
