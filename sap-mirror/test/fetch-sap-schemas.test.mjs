// Schema download is pinned by integrity and extracts only the schema file (IR-02).
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";
import { gzipSync } from "node:zlib";

import { SCHEMAS, extractFromTar, fetchSchema, verifyIntegrity } from "../scripts/fetch-sap-schemas.mjs";

// Builds a ustar archive in memory; synthetic content only.
function tar(files) {
  const blocks = [];
  for (const [name, content] of Object.entries(files)) {
    const body = Buffer.from(content);
    const header = Buffer.alloc(512);
    header.write(name, 0, "utf8");
    header.write("0000644\0", 100);
    header.write(body.length.toString(8).padStart(11, "0") + "\0", 124);
    header.write("0", 156);
    header.write("ustar\0", 257);
    header.fill(" ", 148, 156);
    const sum = header.reduce((total, byte) => total + byte, 0);
    header.write(sum.toString(8).padStart(6, "0") + "\0 ", 148);
    blocks.push(header, body, Buffer.alloc((512 - (body.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  return Buffer.concat(blocks);
}

const integrityOf = (bytes) => `sha512-${createHash("sha512").update(bytes).digest("base64")}`;
const respond = (bytes) => async () => ({ ok: true, status: 200, arrayBuffer: async () => bytes });

test("every schema package is pinned by a sha512 integrity", () => {
  assert.equal(Object.keys(SCHEMAS).length, 6);
  for (const integrity of Object.values(SCHEMAS)) assert.match(integrity, /^sha512-[A-Za-z0-9+/]+=*$/);
});

test("only the CSN file is extracted from the package archive", async () => {
  const archive = gzipSync(
    tar({
      "package/index.js": "module.exports = 1",
      "package/demo-service-csn.json": '{"definitions":{}}',
    }),
  );

  const schema = await fetchSchema("@sap/demo", integrityOf(archive), respond(archive));

  assert.equal(schema.toString("utf8"), '{"definitions":{}}');
});

test("an archive that does not match the pinned integrity is refused", async () => {
  const archive = gzipSync(tar({ "package/demo-service-csn.json": "{}" }));
  const other = gzipSync(tar({ "package/demo-service-csn.json": '{"tampered":true}' }));

  await assert.rejects(fetchSchema("@sap/demo", integrityOf(archive), respond(other)), /integrity mismatch/);
  assert.throws(() => verifyIntegrity(other, integrityOf(archive)), /integrity mismatch/);
});

test("an archive without a schema file is refused", () => {
  assert.throws(() => extractFromTar(tar({ "package/index.js": "" }), (n) => n.endsWith("-csn.json")), /no matching file/);
});
