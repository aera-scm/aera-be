// SAP Mirror server (SRD 6.6, IR-02): S/4HANA-shaped write behaviour on top of CAP.
const cds = require("@sap/cds");
const { csrf } = require("./csrf");
const { reset } = require("./seed");

// The V2 adapter reads the raw language header; `Accept-Language: *` (sent by default by
// Node's fetch, among others) would make it localize $metadata for every locale at once
// and fail. CDS itself treats a wildcard as "no preference"; do the same for the adapter.
const rawLocale = cds.i18n.locale.header;
cds.i18n.locale.header = (request) => {
  const raw = rawLocale(request);
  return raw && !/^\s*\*/.test(raw) ? raw : undefined;
};

// AT-08 fault injection, armed only through the admin service (MirrorAdmin scope).
const fault = { skip: 0, count: 0, status: 500 };
const WRITES = new Set(["POST", "PATCH", "MERGE", "PUT", "DELETE"]);

const PURCHASE_ORDER_RANGE = { first: 4500000000, last: 4599999999 };
const MATERIAL_DOCUMENT_RANGE = { first: 4900000000, last: 4999999999 };

cds.on("bootstrap", (app) => {
  // Every modifying request, whichever path it uses, needs a fetched token.
  app.use(csrf());
  // Runs inside the OData V2 adapter, before it proxies to the V4 service.
  cds.cov2ap.before = (request, response, next) => {
    // S/4HANA answers PATCH/MERGE with 204 No Content.
    if (["PATCH", "MERGE", "PUT"].includes(request.method)) {
      request.headers.prefer ??= "return=minimal";
    }
    if (WRITES.has(request.method) && fault.count > 0) {
      if (fault.skip > 0) {
        fault.skip -= 1;
      } else {
        fault.count -= 1;
        response.status(fault.status).json({ error: { code: String(fault.status), message: { lang: "en", value: "Injected fault" } } });
        return;
      }
    }
    next();
  };
});

function scenarioStart(value) {
  const candidate = value ?? process.env.SCENARIO_T0;
  if (candidate === undefined || candidate === null || candidate === "") return new Date();
  const t0 = new Date(candidate);
  if (Number.isNaN(t0.getTime())) return undefined;
  return t0;
}

async function nextNumber(entity, field, range, where = {}) {
  const [row] = await SELECT.from(entity)
    .columns(`max(${field}) as last`)
    .where({ [field]: { between: String(range.first), and: String(range.last) }, ...where });
  const next = row?.last ? Number(row.last) + 1 : range.first;
  if (next > range.last) throw new Error(`number range for ${field} exhausted`);
  return String(next);
}

function requireIfMatch(service) {
  service.before("UPDATE", "*", (request) => {
    const etagged = Object.values(request.target?.elements ?? {}).some((e) => e["@odata.etag"]);
    if (etagged && !request.headers?.["if-match"]) {
      request.reject(428, "Precondition required: send If-Match with the entity ETag");
    }
  });
}

cds.on("served", async (services) => {
  const po = services.API_PURCHASEORDER_PROCESS_SRV;
  const s4 = "aera.mirror.s4";

  requireIfMatch(po);
  po.before("CREATE", "A_PurchaseOrder", async (request) => {
    const header = request.data;
    header.PurchaseOrder ??= await nextNumber(`${s4}.A_PurchaseOrder`, "PurchaseOrder", PURCHASE_ORDER_RANGE);
    const today = new Date().toISOString().slice(0, 10);
    header.CreationDate ??= today;
    header.PurchaseOrderDate ??= today;
    for (const item of header.to_PurchaseOrderItem ?? []) {
      item.PurchaseOrder = header.PurchaseOrder;
      for (const line of item.to_ScheduleLine ?? []) {
        line.PurchasingDocument = header.PurchaseOrder;
        line.PurchasingDocumentItem = item.PurchaseOrderItem;
      }
    }
  });

  const documents = services.API_MATERIAL_DOCUMENT_SRV;
  documents.before(["UPDATE", "DELETE"], "A_MaterialDocumentItem", (request) =>
    request.reject(405, "Material documents are posted, never changed"),
  );
  documents.before("CREATE", "A_MaterialDocumentItem", async (request) => {
    const item = request.data;
    item.MaterialDocumentYear ??= String(new Date().getUTCFullYear());
    item.MaterialDocument ??= await nextNumber(
      `${s4}.A_MaterialDocumentItem`,
      "MaterialDocument",
      MATERIAL_DOCUMENT_RANGE,
    );
    item.MaterialDocumentItem ??= "1";
    item.EntryUnit ??= item.MaterialBaseUnit;
    item.MaterialBaseUnit ??= item.EntryUnit;
    item.QuantityInBaseUnit ??= item.QuantityInEntryUnit;
  });
  // A goods receipt (movement 101) raises unrestricted stock, as posting it in S/4HANA does.
  documents.after("CREATE", "A_MaterialDocumentItem", async (item) => {
    if (item.GoodsMovementType !== "101") return;
    const stock = `${s4}.A_MatlStkInAcctMod`;
    const key = { Material: item.Material, Plant: item.Plant, InventoryStockType: "01" };
    const [row] = await SELECT.from(stock).where(key).limit(1);
    if (row) {
      const keys = Object.values(cds.model.definitions[stock].elements).filter((e) => e.key);
      const quantity = Number(row.MatlWrhsStkQtyInMatlBaseUnit) + Number(item.QuantityInBaseUnit);
      await UPDATE(stock)
        .set({ MatlWrhsStkQtyInMatlBaseUnit: quantity })
        .where(Object.fromEntries(keys.map((element) => [element.name, row[element.name]])));
    }
  });

  services.MirrorAdminService.on("reset", async (request) => {
    const t0 = scenarioStart(request.data.t0);
    if (!t0) return request.reject(400, "t0 must be an ISO-8601 timestamp");
    Object.assign(fault, { skip: 0, count: 0, status: 500 });
    return reset(t0);
  });

  services.MirrorAdminService.on("fault", (request) => {
    const { skip = 0, count = 0, status = 500 } = request.data;
    if (![skip, count].every((n) => Number.isInteger(n) && n >= 0 && n <= 20)) {
      return request.reject(400, "skip and count must be integers from 0 to 20");
    }
    if (![429, 500, 502, 503, 504].includes(status)) {
      return request.reject(400, "status must be 429, 500, 502, 503 or 504");
    }
    Object.assign(fault, { skip, count, status });
    return { ...fault };
  });

  const [any] = await SELECT.from(`${s4}.A_PurchaseOrder`).limit(1);
  if (!any) await reset(scenarioStart() ?? new Date());
});

module.exports = cds.server;
