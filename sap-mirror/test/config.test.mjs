// Deployment configuration safety (NFR-SEC-03): dummy auth and the in-memory database exist
// only in the development profile; production uses XSUAA and HANA.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const root = join(import.meta.dirname, "..");
const { cds } = JSON.parse(readFileSync(join(root, "package.json"), "utf8"));

test("no authentication or database setting outside a profile", () => {
  assert.equal(cds.requires, undefined);
});

test("production authenticates with XSUAA and persists in HANA", () => {
  assert.equal(cds["[production]"].requires.auth.kind, "xsuaa");
  assert.equal(cds["[production]"].requires.db.kind, "hana");
});

test("dummy authentication is confined to development", () => {
  assert.equal(cds["[development]"].requires.auth.kind, "dummy");
  const production = JSON.stringify(cds["[production]"]);
  assert.ok(!production.includes("dummy") && !production.includes("mocked"));
});

test("the XSUAA descriptor grants client-credential tokens only the Mirror's own scope", () => {
  const security = JSON.parse(readFileSync(join(root, "xs-security.json"), "utf8"));

  assert.deepEqual(security["oauth2-configuration"]["grant-types"], ["client_credentials"]);
  assert.deepEqual(security.authorities, ["$XSAPPNAME.MirrorAdmin"]);
});
