import { test } from "node:test";
import assert from "node:assert/strict";
import { authenticated, localRequest, sessionCookie } from "./security";

const origin = "http://localhost:8787";
const secret = "a-local-test-secret-that-is-long-enough";

test("local session rejects cross-origin HTTP, websocket, rebinding and forged cookies", async () => {
  const cookie = (await sessionCookie(secret)).split(";")[0];
  const request = (headers: Record<string, string> = {}) => new Request(origin + "/api/state", { headers });
  assert.equal(localRequest(request(), origin), true);
  assert.equal(localRequest(request({ Origin: "http://other.invalid" }), origin), false);
  assert.equal(localRequest(request({ "Sec-Fetch-Site": "cross-site" }), origin), false);
  assert.equal(localRequest(request({ Upgrade: "websocket" }), origin), false);
  assert.equal(localRequest(request({ Upgrade: "websocket", Origin: origin }), origin), true);
  assert.equal(localRequest(new Request("http://other.invalid:8787"), origin), false);
  assert.equal(localRequest(new Request(origin + "/session", { method: "POST" }), origin), false);
  assert.equal(await authenticated(request({ Cookie: cookie }), secret), true);
  assert.equal(await authenticated(request({ Cookie: cookie }), secret + "wrong"), false);
  assert.equal(await authenticated(request({ Cookie: "shopping_voice=0000000000." + "0".repeat(64) }), secret), false);
  assert.equal(await authenticated(request(), secret), false);
});
