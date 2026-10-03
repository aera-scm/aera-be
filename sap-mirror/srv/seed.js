// Reference-scenario seed (SRD 6.6.3). The CSV templates in db/seed hold dates relative
// to the scenario start as tokens such as {T0}, {T0+9d} or {T0+6h12m}; reset() resolves
// them for a given T0 so the scenario can be replayed on any day.
const fs = require("node:fs");
const path = require("node:path");
const cds = require("@sap/cds");

const SEED_DIRECTORY = path.join(__dirname, "..", "db", "seed");
const TOKEN = /^\{T0((?:[+-]\d+[dhm])*)\}$/;
const UNIT_MS = { d: 86_400_000, h: 3_600_000, m: 60_000 };

function offsetMs(spec) {
  let total = 0;
  for (const [, sign, amount, unit] of spec.matchAll(/([+-])(\d+)([dhm])/g)) {
    total += (sign === "-" ? -1 : 1) * Number(amount) * UNIT_MS[unit];
  }
  return total;
}

function parseCsv(text) {
  const rows = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.startsWith("#")) continue;
    const cells = [];
    let cell = "";
    let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      const char = line[i];
      if (quoted && char === '"' && line[i + 1] === '"') {
        cell += '"';
        i += 1;
      } else if (char === '"') {
        quoted = !quoted;
      } else if (char === "," && !quoted) {
        cells.push(cell);
        cell = "";
      } else {
        cell += char;
      }
    }
    cells.push(cell);
    rows.push(cells);
  }
  const [header, ...body] = rows;
  return body.map((cells) => Object.fromEntries(header.map((name, i) => [name, cells[i] ?? ""])));
}

function convert(raw, element, t0, where) {
  const token = TOKEN.exec(raw);
  if (token) {
    const at = new Date(t0.getTime() + offsetMs(token[1]));
    switch (element.type) {
      case "cds.Date":
        return at.toISOString().slice(0, 10);
      case "cds.Time":
        return at.toISOString().slice(11, 19);
      case "cds.Timestamp":
      case "cds.DateTime":
        return at.toISOString();
      default:
        throw new Error(`${where}: T0 token in a ${element.type} field`);
    }
  }
  if (raw === "") return element.key ? "" : null;
  if (element.type === "cds.Boolean") return raw === "true";
  return raw;
}

function entityFor(file) {
  const name = path.basename(file, ".csv").replace(/-/g, ".");
  const entity = cds.model.definitions[name];
  if (!entity) throw new Error(`${file}: no entity ${name}`);
  return entity;
}

function load(t0) {
  const seed = [];
  for (const file of fs.readdirSync(SEED_DIRECTORY).filter((f) => f.endsWith(".csv")).sort()) {
    const entity = entityFor(file);
    const rows = parseCsv(fs.readFileSync(path.join(SEED_DIRECTORY, file), "utf8")).map(
      (row, index) =>
        Object.fromEntries(
          Object.entries(row).map(([field, raw]) => {
            const element = entity.elements[field];
            if (!element) throw new Error(`${file}: ${entity.name} has no field ${field}`);
            return [field, convert(raw, element, t0, `${file}:${index + 2}:${field}`)];
          }),
        ),
    );
    seed.push({ entity, rows });
  }
  return seed;
}

// Every persisted Mirror entity is cleared, including ones written since the last reset.
function mirrorEntities() {
  return Object.values(cds.model.definitions).filter(
    (d) => d.kind === "entity" && d.name.startsWith("aera.mirror.") && !d.query,
  );
}

async function reset(t0) {
  const started = Date.now();
  const seed = load(t0);
  let rows = 0;
  await cds.tx(async (tx) => {
    for (const entity of mirrorEntities()) await tx.run(DELETE.from(entity));
    for (const { entity, rows: entries } of seed) {
      if (entries.length) await tx.run(INSERT.into(entity).entries(entries));
      rows += entries.length;
    }
  });
  return { t0: t0.toISOString(), rows, durationMs: Date.now() - started };
}

// Evaluation patches (WP-13): entity names are the S/4 or Mirror entity names, e.g.
// A_MatlStkInAcctMod or MaterialConsumptionRate; T0 tokens resolve against the last reset.
function mirrorEntity(name) {
  const entity =
    cds.model.definitions[`aera.mirror.s4.${name}`] ??
    cds.model.definitions[`aera.mirror.${name}`];
  if (!entity || entity.kind !== "entity" || entity.query) {
    throw new Error(`unknown entity ${name}`);
  }
  return entity;
}

function values(entity, row, t0, where) {
  return Object.fromEntries(
    Object.entries(row ?? {}).map(([field, raw]) => {
      const element = entity.elements[field];
      if (!element) throw new Error(`${where}: ${entity.name} has no field ${field}`);
      return [field, convert(String(raw), element, t0, `${where}:${field}`)];
    }),
  );
}

async function patch(changes, t0) {
  if (!Array.isArray(changes)) throw new Error("changes must be a JSON list");
  let applied = 0;
  await cds.tx(async (tx) => {
    for (const [index, change] of changes.entries()) {
      const where = `change ${index}`;
      const entity = mirrorEntity(change.entity);
      const match = values(entity, change.where, t0, where);
      if (change.upsert === true && change.insert && change.set && Object.keys(match).length) {
        const count = await tx.run(
          UPDATE(entity).set(values(entity, change.set, t0, where)).where(match),
        );
        if (!count) await tx.run(INSERT.into(entity).entries(values(entity, change.insert, t0, where)));
      } else if (change.insert) {
        await tx.run(INSERT.into(entity).entries(values(entity, change.insert, t0, where)));
      } else if (change.remove === true && Object.keys(match).length) {
        await tx.run(DELETE.from(entity).where(match));
      } else if (change.set && Object.keys(match).length) {
        const count = await tx.run(
          UPDATE(entity).set(values(entity, change.set, t0, where)).where(match),
        );
        if (!count) throw new Error(`${where}: no ${change.entity} row matches`);
      } else {
        throw new Error(`${where}: needs insert, set with where, or remove with where`);
      }
      applied += 1;
    }
  });
  return { applied };
}

module.exports = { reset, load, parseCsv, offsetMs, patch };
