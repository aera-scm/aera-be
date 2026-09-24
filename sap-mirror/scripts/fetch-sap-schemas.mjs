// Fetch SAP's published service schemas (CSN) for the Mirror model (SRD 6.6.2, IR-02).
//
// SAP ships the CSN, generated from the official EDMX, inside its @sap/cloud-sdk-vdm-*
// packages (SAP Developer License). Only the schema file is needed, so the pinned
// tarballs are downloaded, checked against the registry's sha512 integrity and just the
// CSN is extracted into .sap-schemas/ (gitignored). No package code is installed and
// nothing of SAP's is committed.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";

export const CACHE = join(dirname(fileURLToPath(import.meta.url)), "..", ".sap-schemas");

const REGISTRY = "https://registry.npmjs.org";
// Registry integrity checksums, public like lockfile hashes.
export const SCHEMAS = {
  "@sap/cloud-sdk-vdm-business-partner-service": "sha512-XQqY8FXEgpC6l3NOlEIPYj44bH+I0jaM7tzHADxLz/VT/4TQ3d32gTzRHQjcYCarBXlyNglB5TYW9PmZniULQQ==", // pragma: allowlist secret
  "@sap/cloud-sdk-vdm-material-document-service": "sha512-lYIGNXqsAIW5jBOCYUnj3MXhwYzCym0q3qlcxNkG9wNn5hpdhrh2wRx3rOgS9LkLEYAExHWC248bYEUbXCotzg==", // pragma: allowlist secret
  "@sap/cloud-sdk-vdm-material-stock-service": "sha512-GnChxiKCh+sK1rSgByP++0lFJt3luW7nk6oX10dWQPFieRf0khBnhQBlBQshk7hwJTT05kUHJxPhCrkOFqAOuw==", // pragma: allowlist secret
  "@sap/cloud-sdk-vdm-production-order-v2-service": "sha512-6XAGTxuGGhPQXqjXoUSuTRoaBIV2IiEw0BthCPQxc6LwMSTl28Tq97ybHqqsNLG4CrdBfphi+B/3WUg9eeNPEA==", // pragma: allowlist secret
  "@sap/cloud-sdk-vdm-purchase-order-service": "sha512-PU80nERWC8TpXRYrgQHQYoPGDmrY43ZjfhydctKj6CGSMTA04b5ughx4O7zngTjDcd37rdaGJPNCydQS/lfxZA==", // pragma: allowlist secret
  "@sap/cloud-sdk-vdm-sales-order-service": "sha512-/amkTvZKARWe8MxC1+gRIj8FT2rU41wPd1ngX+Nfbz1LSogL6zTWitHZaFyFutwBg7hY+kWyKDs8M412dbwm7Q==", // pragma: allowlist secret
};
const VERSION = "2.1.0";

export function cacheFile(packageName) {
  return join(CACHE, `${packageName.replace(/^@sap\//, "")}.csn.json`);
}

export function verifyIntegrity(bytes, integrity) {
  const [algorithm, expected] = integrity.split("-", 2);
  const actual = createHash(algorithm).update(bytes).digest("base64");
  if (actual !== expected) throw new Error(`integrity mismatch (${algorithm})`);
}

// Minimal ustar reader: returns the first regular file whose name matches.
export function extractFromTar(tar, matches) {
  let offset = 0;
  while (offset + 512 <= tar.length) {
    const header = tar.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    const field = (start, length) =>
      header.subarray(start, start + length).toString("utf8").replace(/\0.*$/s, "").trim();
    const name = (field(345, 155) ? `${field(345, 155)}/` : "") + field(0, 100);
    const size = parseInt(field(124, 12) || "0", 8);
    const type = field(156, 1) || "0";
    const body = tar.subarray(offset + 512, offset + 512 + size);
    if (type === "0" && matches(name)) return { name, body };
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  throw new Error("no matching file in archive");
}

export async function fetchSchema(packageName, integrity, download = fetch) {
  const base = packageName.split("/").pop();
  const url = `${REGISTRY}/${packageName}/-/${base}-${VERSION}.tgz`;
  const response = await download(url);
  if (!response.ok) throw new Error(`${packageName}: HTTP ${response.status}`);
  const archive = Buffer.from(await response.arrayBuffer());
  verifyIntegrity(archive, integrity);
  const { body } = extractFromTar(gunzipSync(archive), (name) => /^package\/[^/]+-csn\.json$/.test(name));
  JSON.parse(body.toString("utf8"));
  return body;
}

export async function ensureSchemas({ force = false } = {}) {
  mkdirSync(CACHE, { recursive: true });
  for (const [packageName, integrity] of Object.entries(SCHEMAS)) {
    const target = cacheFile(packageName);
    if (!force && existsSync(target)) continue;
    writeFileSync(target, await fetchSchema(packageName, integrity));
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await ensureSchemas({ force: process.argv.includes("--force") });
  console.log(`SAP schemas ready in ${CACHE}`);
}
