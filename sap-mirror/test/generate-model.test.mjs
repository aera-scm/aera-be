// IR-02 / SRD 6.6.2: Mirror field names come from SAP's schema, never from memory.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { generate } from "../scripts/generate-model.mjs";

const root = join(import.meta.dirname, "..");

// Synthetic schema in SAP's CSN layout; not SAP data.
const schema = {
  definitions: {
    "SRV.A_Head": {
      kind: "entity",
      elements: {
        Id: { key: true, type: "cds.String", length: 10 },
        Amount: { type: "cds.Decimal", precision: 13, scale: 3 },
        Day: { type: "cds.Date" },
        to_Line: { type: "cds.Association" },
      },
    },
    "SRV.A_Line": {
      kind: "entity",
      elements: {
        HeadId: { key: true, type: "cds.String", length: 10 },
        Line: { key: true, type: "cds.String", length: 5 },
        Text: { type: "cds.String", length: 40 },
      },
    },
  },
};

function selection(overrides = {}) {
  return {
    services: {
      SRV: {
        package: "synthetic",
        entities: { A_Head: ["Amount", "Day"], A_Line: ["Text"] },
        compositions: [
          { parent: "A_Head", name: "to_Line", child: "A_Line", on: { HeadId: "Id" } },
        ],
        ...overrides,
      },
    },
  };
}

test("copies keys first, then selected fields with SAP's exact types", () => {
  const text = generate(selection(), () => schema);

  assert.match(text, /entity A_Head \{\n {2}key Id : String\(10\);\n {2}Amount : Decimal\(13, 3\);\n {2}Day : Date;/);
  assert.match(text, /to_Line : Composition of many A_Line on to_Line\.HeadId = Id;/);
  assert.match(text, /entity A_Line \{\n {2}key HeadId : String\(10\);\n {2}key Line : String\(5\);/);
});

test("a field SAP does not define fails generation", () => {
  assert.throws(
    () => generate(selection({ entities: { A_Head: ["Amount", "Invented"] } }), () => schema),
    /SRV\.A_Head has no field\(s\) Invented/,
  );
});

test("an entity SAP does not define fails generation", () => {
  assert.throws(
    () => generate(selection({ entities: { A_Nothing: [] } }), () => schema),
    /SRV defines no entity A_Nothing/,
  );
});

test("a navigation or join field SAP does not define fails generation", () => {
  const badName = [{ parent: "A_Head", name: "to_Other", child: "A_Line", on: { HeadId: "Id" } }];
  const badJoin = [{ parent: "A_Head", name: "to_Line", child: "A_Line", on: { Nope: "Id" } }];

  assert.throws(() => generate(selection({ compositions: badName }), () => schema), /no navigation to_Other/);
  assert.throws(() => generate(selection({ compositions: badJoin }), () => schema), /bad join Nope/);
});

test("the committed model matches SAP's published schemas exactly", () => {
  const selectionFile = JSON.parse(readFileSync(join(root, "model", "fields.json"), "utf8"));
  const committed = readFileSync(join(root, "db", "s4.cds"), "utf8");

  assert.equal(committed, generate(selectionFile));
});

test("the model covers every entity set of SRD 6.6.2", () => {
  const committed = readFileSync(join(root, "db", "s4.cds"), "utf8");
  for (const name of [
    "A_PurchaseOrder",
    "A_PurchaseOrderItem",
    "A_PurchaseOrderScheduleLine",
    "A_MatlStkInAcctMod",
    "A_ProductionOrder_2",
    "A_SalesOrderItem",
    "A_SalesOrderScheduleLine",
    "A_Supplier",
    "A_BusinessPartner",
    "A_AddressEmailAddress",
    "A_MaterialDocumentItem",
  ]) {
    assert.match(committed, new RegExp(`^entity ${name} \\{`, "m"), name);
  }
});
