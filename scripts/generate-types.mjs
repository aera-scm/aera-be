// Generate the console's API types from the exported JSON Schema (SRD 6.19, 7.2).
//   uv run --locked python scripts/export_schemas.py > build/aera-contract.schema.json
//   node scripts/generate-types.mjs build/aera-contract.schema.json ../aera-fe/src/api/types.generated.ts
import { readFileSync, writeFileSync } from "node:fs";
import { compile } from "json-schema-to-typescript";

const [input, output] = process.argv.slice(2);
const schema = JSON.parse(readFileSync(input, "utf8"));
const banner =
  "/* Generated from aera-be services/shared/models.py by `make types`. Do not edit. */";
const text = await compile(schema, "AeraContract", {
  bannerComment: banner,
  additionalProperties: false,
  unreachableDefinitions: true,
  style: { singleQuote: false, semi: true, printWidth: 100 },
});
writeFileSync(output, text);
console.log(`wrote ${output}`);
