// The Mirror outside SAP BTP (profile `aws`): OAuth 2.0 access tokens of an OIDC issuer
// replace XSUAA (IR-02, NFR-SEC-04). Keys are synthetic and generated per run.
import assert from "node:assert/strict";
import { createSign, generateKeyPairSync } from "node:crypto";
import { after, before, test } from "node:test";

import { client, startMirror } from "./support/server.mjs";

const ISSUER = "https://issuer.example.invalid/pool";
const CLIENT = "synthetic-mirror-client";
const SCOPE = "aera-test-mirror/access";
const ADMIN = "aera-test-mirror/admin";
const PO = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder?$top=1";

const trusted = generateKeyPairSync("rsa", { modulusLength: 2048 });
const stranger = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwks = {
  keys: [{ ...trusted.publicKey.export({ format: "jwk" }), kid: "k1", alg: "RS256", use: "sig" }],
};

const encode = (value) => Buffer.from(JSON.stringify(value)).toString("base64url");

function token(claims = {}, key = trusted.privateKey) {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    iss: ISSUER,
    client_id: CLIENT,
    token_use: "access",
    scope: SCOPE,
    iat: now,
    exp: now + 300,
    ...claims,
  };
  const head = `${encode({ alg: "RS256", kid: "k1", typ: "JWT" })}.${encode(payload)}`;
  return `${head}.${createSign("RSA-SHA256").update(head).sign(key).toString("base64url")}`;
}

const config = (auth) => JSON.stringify({ requires: { auth } });
const settings = { issuer: ISSUER, clientId: CLIENT, scope: SCOPE, adminScope: ADMIN, jwks };
const bearer = (value) => ({ authorization: `Bearer ${value}` });

let mirror;
let http;

before(async () => {
  mirror = await startMirror({ NODE_ENV: "", CDS_ENV: "aws", CDS_CONFIG: config(settings) });
  http = client(mirror.url);
});
after(() => mirror?.stop());

// Modifying requests need a fetched CSRF token and its session cookie, as on SAP Gateway.
async function post(path, body, authorization) {
  const fetched = await http.get(PO, { "x-csrf-token": "Fetch", ...bearer(token()) });
  return http.post(path, body, {
    "x-csrf-token": fetched.header("x-csrf-token"),
    cookie: fetched.cookies(),
    ...authorization,
  });
}

test("a request without a token is refused", async () => {
  assert.equal((await http.get(PO)).status, 401);
});

test("an access token of the Mirror client with the access scope reads data", async () => {
  const response = await http.get(PO, bearer(token()));

  assert.equal(response.status, 200);
  assert.equal(response.data.d.results.length, 1);
});

test("the access scope alone cannot reset the scenario", async () => {
  const response = await post("/admin/reset", {}, bearer(token()));

  assert.equal(response.status, 403);
});

test("the admin scope resets the scenario", async () => {
  const admin = bearer(token({ scope: `${SCOPE} ${ADMIN}` }));
  const response = await post("/admin/reset", {}, admin);

  assert.equal(response.status, 200);
  assert.ok(response.data.rows > 0);
});

for (const [name, value] of [
  ["another client", () => token({ client_id: "someone-else" })],
  ["an ID token", () => token({ token_use: "id" })],
  ["an expired token", () => token({ exp: Math.floor(Date.now() / 1000) - 60 })],
  ["another issuer", () => token({ iss: "https://other.example.invalid/pool" })],
  ["a token without a Mirror scope", () => token({ scope: "aera-test-interop/invoke" })],
  ["a token signed with an unknown key", () => token({}, stranger.privateKey)],
  ["an unsigned token", () => token().split(".").slice(0, 2).join(".") + "."],
  ["a malformed token", () => "not-a-jwt"],
]) {
  test(`${name} is refused`, async () => {
    assert.equal((await http.get(PO, bearer(value()))).status, 401);
    assert.equal((await post("/admin/reset", {}, bearer(value()))).status, 401);
  });
}

for (const [name, auth] of [
  ["no issuer", { ...settings, issuer: "" }],
  ["a plain-http issuer", { ...settings, issuer: "http://issuer.example.invalid/pool" }],
  ["no client id", { ...settings, clientId: "" }],
  ["no access scope", { ...settings, scope: "" }],
  ["no admin scope", { ...settings, adminScope: "" }],
]) {
  test(`the server does not start with ${name}`, async () => {
    let started;
    try {
      started = await startMirror({ NODE_ENV: "", CDS_ENV: "aws", CDS_CONFIG: config(auth) });
    } catch (error) {
      assert.match(String(error), /Mirror OAuth/);
      return;
    }
    await started.stop();
    assert.fail("the server started without a complete OAuth configuration");
  });
}
