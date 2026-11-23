import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { canPromote, promotionRefusal, promotionTargetLabel, promotionTargets } from "./promotion";
import type { EnvironmentOption } from "./types";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const OTHER_PROJECT = "22222222-2222-4222-8222-222222222222";
const SHA = "0123456789abcdef0123456789abcdef01234567";

function env(id: string, slug: string, kind: string, projectId = PROJECT): EnvironmentOption {
  return {
    id,
    project_id: projectId,
    project_slug: "portal",
    project_name: "Portal",
    slug,
    name: slug,
    kind,
    region: "ams3",
  };
}

const dev = env("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "dev", "dev");
const staging = env("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "staging", "staging");
const prod = env("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "live", "prod");
const elsewhere = env("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "staging", "staging", OTHER_PROJECT);

describe("canPromote", () => {
  it("needs a succeeded deployment with a recorded commit", () => {
    assert.equal(canPromote({ status: "succeeded", git_sha: SHA }), true);
    assert.equal(canPromote({ status: "succeeded", git_sha: null }), false);
    assert.equal(canPromote({ status: "succeeded" }), false);
    assert.equal(canPromote({ status: "running", git_sha: SHA }), false);
    assert.equal(canPromote({ status: "failed", git_sha: SHA }), false);
  });
});

describe("promotionTargets", () => {
  it("offers the other environments of the same project", () => {
    const targets = promotionTargets(
      { project_id: PROJECT, environment_id: dev.id },
      [dev, staging, prod, elsewhere],
    );
    assert.deepEqual(
      targets.map((target) => target.id),
      [staging.id, prod.id],
    );
  });

  it("offers nothing for a deployment that is not linked to an environment", () => {
    assert.deepEqual(promotionTargets({ project_id: PROJECT }, [dev, staging]), []);
    assert.deepEqual(promotionTargets({ project_id: null, environment_id: null }, [dev]), []);
  });
});

describe("promotionTargetLabel", () => {
  it("says production needs an owner", () => {
    assert.equal(promotionTargetLabel(staging), "staging (staging)");
    assert.equal(promotionTargetLabel(prod), "live (production, owner only)");
  });
});

describe("promotionRefusal", () => {
  it("keeps the status for refusals the user can act on", () => {
    assert.equal(promotionRefusal(403)?.code, "forbidden");
    assert.equal(promotionRefusal(409)?.code, "target_busy");
    assert.equal(promotionRefusal(422)?.code, "not_promotable");
    assert.equal(promotionRefusal(503)?.code, "queue_unavailable");
    assert.equal(promotionRefusal(403)?.status, 403);
  });

  it("leaves everything else to the generic handler", () => {
    assert.equal(promotionRefusal(404), null);
    assert.equal(promotionRefusal(500), null);
    assert.equal(promotionRefusal(undefined), null);
  });
});
