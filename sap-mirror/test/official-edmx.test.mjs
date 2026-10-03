// IR-02 / SRD 6.6.2: every field the Mirror serves exists, with the same EDM type, in the
// official EDMX downloaded from the sandbox (scripts/download_sap_metadata.py). Until
// that inventory exists the test is skipped and says why; it never passes by default.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const root = join(import.meta.dirname, "..");
const inventory = join(root, "metadata");
const manifest = join(inventory, "manifest.json");

const EDM = {
  String: "Edm.String",
  Decimal: "Edm.Decimal",
  Boolean: "Edm.Boolean",
  Date: "Edm.DateTime",
  Time: "Edm.Time",
  Timestamp: "Edm.DateTimeOffset",
  DateTime: "Edm.DateTimeOffset",
  UUID: "Edm.Guid",
};

export function mirrorFields(cds) {
  const entities = {};
  let current;
  for (const line of cds.split("\n")) {
    const open = /^entity (\w+) \{/.exec(line);
    if (open) entities[(current = open[1])] = {};
    const field = /^ {2}(?:key )?(\w+) : (\w+)/.exec(line);
    if (current && field && !line.includes("Composition")) entities[current][field[1]] = field[2];
  }
  return entities;
}

export function edmxProperties(xml) {
  const types = {};
  for (const [, name, body] of xml.matchAll(/<EntityType Name="(\w+?)(?:Type)?"[^>]*>([\s\S]*?)<\/EntityType>/g)) {
    types[name] = Object.fromEntries(
      [...body.matchAll(/<Property Name="(\w+)" Type="([\w.]+)"/g)].map(([, p, t]) => [p, t]),
    );
  }
  return types;
}

test("the Mirror's fields and types exist in the official EDMX", { skip: !existsSync(manifest) && "official EDMX inventory not downloaded yet (needs the SAP sandbox key)" }, () => {
  const entries = JSON.parse(readFileSync(manifest, "utf8")).files;
  const official = Object.assign(
    {},
    ...entries.map((entry) => edmxProperties(readFileSync(join(inventory, entry.file), "utf8"))),
  );
  const mirror = mirrorFields(readFileSync(join(root, "db", "s4.cds"), "utf8"));
  const problems = [];
  for (const [entity, fields] of Object.entries(mirror)) {
    const properties = official[entity];
    if (!properties) {
      problems.push(`${entity}: not in the official EDMX`);
      continue;
    }
    for (const [field, type] of Object.entries(fields)) {
      if (!(field in properties)) problems.push(`${entity}.${field}: not in the official EDMX`);
      else if (properties[field] !== EDM[type]) {
        problems.push(`${entity}.${field}: ${EDM[type]} here, ${properties[field]} officially`);
      }
    }
  }
  assert.deepEqual(problems, []);
});

test("the cross-check parser reads SAP's EDMX layout (synthetic sample)", () => {
  const xml = `<EntityType Name="A_DemoType" sap:content-version="1"><Key/>
    <Property Name="Demo" Type="Edm.String" MaxLength="10"/>
    <Property Name="Amount" Type="Edm.Decimal" Precision="13" Scale="3"/></EntityType>`;
  assert.deepEqual(edmxProperties(xml), { A_Demo: { Demo: "Edm.String", Amount: "Edm.Decimal" } });
  assert.deepEqual(mirrorFields("entity A_Demo {\n  key Demo : String(10);\n  Amount : Decimal(13, 3);\n}"), {
    A_Demo: { Demo: "String", Amount: "Decimal" },
  });
});
