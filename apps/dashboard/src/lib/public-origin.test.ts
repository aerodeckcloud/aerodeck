import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { publicOrigin } from "@/lib/public-origin";

function request(
  url: string,
  headers: Record<string, string> = {},
): Request {
  return new Request(url, { headers });
}

describe("publicOrigin", () => {
  it("keeps the browser host when Next reports the bind address", () => {
    const origin = publicOrigin(
      request("http://0.0.0.0:3000/api/mock/v1/auth/google/start", {
        host: "localhost:3000",
      }),
    );
    assert.equal(origin, "http://localhost:3000");
  });

  it("uses https from the forwarded proto on the public console host", () => {
    const origin = publicOrigin(
      request("http://0.0.0.0:3000/api/mock/v1/auth/google/start", {
        host: "dev.app.aero-deck.app",
        "x-forwarded-proto": "https",
      }),
    );
    assert.equal(origin, "https://dev.app.aero-deck.app");
  });

  it("falls back to the forwarded host when the host header is the bind address", () => {
    const origin = publicOrigin(
      request("http://0.0.0.0:3000/api/mock/v1/auth/google/callback", {
        host: "0.0.0.0:3000",
        "x-forwarded-host": "dev.app.aero-deck.app, internal",
        "x-forwarded-proto": "https, http",
      }),
    );
    assert.equal(origin, "https://dev.app.aero-deck.app");
  });

  it("ignores a forwarded host when the browser host is already usable", () => {
    const origin = publicOrigin(
      request("http://localhost:3210/login", {
        host: "localhost:3210",
        "x-forwarded-host": "evil.example",
        "x-forwarded-proto": "https",
      }),
    );
    assert.equal(origin, "https://localhost:3210");
  });

  it("rewrites a bind-address request to localhost with the same port", () => {
    const origin = publicOrigin(request("http://0.0.0.0:3000/api/mock/v1/auth/google/start"));
    assert.equal(origin, "http://localhost:3000");
  });

  it("rejects a host header that carries a path or userinfo", () => {
    const origin = publicOrigin(
      request("http://0.0.0.0:3000/api/mock/v1/auth/google/start", {
        host: "user@evil.example/path",
        "x-forwarded-host": "dev.app.aero-deck.app",
      }),
    );
    assert.equal(origin, "http://dev.app.aero-deck.app");
  });
});
