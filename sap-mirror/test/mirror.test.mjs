// SAP Mirror behaviour: OData V2 contract, CSRF, ETag, deep insert, reset (IR-02, FR-ADM-03).
import assert from "node:assert/strict";
import { after, before, describe, test } from "node:test";

import { client, startMirror } from "./support/server.mjs";

const T0 = "2026-10-05T08:00:00.000Z";
let mirror;
let http;
before(async () => {
  mirror = await startMirror({ SCENARIO_T0: T0 });
  http = client(mirror.url);
});
after(() => mirror?.stop());

const V2 = "/sap/opu/odata/sap";
const PO = `${V2}/API_PURCHASEORDER_PROCESS_SRV`;
const epoch = (iso) => `/Date(${Date.parse(iso)})/`;

async function csrf() {
  const response = await http.get(`${PO}/`, { "x-csrf-token": "Fetch" });
  assert.equal(response.status, 200);
  const token = response.header("x-csrf-token");
  const cookie = response.cookies();
  assert.ok(token && cookie);
  return { "x-csrf-token": token, cookie };
}

async function reset() {
  const headers = await csrf();
  const response = await http.post("/admin/reset", { t0: T0 }, headers);
  assert.equal(response.status, 200, JSON.stringify(response.data));
  return response.data;
}

describe("OData V2 contract (IR-02)", () => {
  test("serves the reference purchase order in the S/4HANA V2 envelope", async () => {
    await reset();
    const { status, data } = await http.get(
      `${PO}/A_PurchaseOrder('4500001234')?$expand=to_PurchaseOrderItem/to_ScheduleLine`,
    );

    assert.equal(status, 200);
    const po = data.d;
    // CAP names V2 entity types after the entity (S/4HANA appends "Type"); ADR-010.
    assert.equal(po.__metadata.type, "API_PURCHASEORDER_PROCESS_SRV.A_PurchaseOrder");
    assert.equal(po.Supplier, "1000234");
    assert.equal(po.CompanyCode, "1010");
    const item = po.to_PurchaseOrderItem.results[0];
    assert.equal(item.Material, "MAT-48219");
    assert.equal(item.Plant, "1010");
    assert.equal(item.OrderQuantity, "1600.000");
    const line = item.to_ScheduleLine.results[0];
    assert.equal(line.ScheduleLineDeliveryDate, epoch("2026-10-05T00:00:00.000Z"));
  });

  test("$metadata is OData V2 and lists every entity set of SRD 6.6.2", async () => {
    const services = {
      API_PURCHASEORDER_PROCESS_SRV: ["A_PurchaseOrder", "A_PurchaseOrderItem", "A_PurchaseOrderScheduleLine"],
      API_MATERIAL_STOCK_SRV: ["A_MatlStkInAcctMod"],
      API_SALES_ORDER_SRV: ["A_SalesOrderItem", "A_SalesOrderScheduleLine"],
      API_PRODUCTION_ORDER_2_SRV: ["A_ProductionOrder_2", "A_ProductionOrderComponent_2"],
      API_BUSINESS_PARTNER: ["A_Supplier", "A_BusinessPartner", "A_AddressEmailAddress"],
      API_MATERIAL_DOCUMENT_SRV: ["A_MaterialDocumentItem"],
    };
    for (const [service, sets] of Object.entries(services)) {
      const { status, data } = await http.get(`${V2}/${service}/$metadata`);
      assert.equal(status, 200, service);
      assert.match(data, /DataServiceVersion="2\.0"/, service);
      for (const set of sets) assert.match(data, new RegExp(`EntitySet Name="${set}"`), set);
    }
  });

  test("supplier correspondence language comes from SAP business-partner master", async () => {
    const bp = `${V2}/API_BUSINESS_PARTNER`;
    const german = await http.get(`${bp}/A_BusinessPartner('1000234')`);
    const indonesian = await http.get(`${bp}/A_BusinessPartner('1000871')`);
    assert.equal(german.status, 200);
    assert.equal(german.data.d.CorrespondenceLanguage, "DE");
    assert.equal(indonesian.status, 200);
    assert.equal(indonesian.data.d.CorrespondenceLanguage, "ID");
  });

  test("custom MRP and consumption entities and the compliance extension are served", async () => {
    const mrp = await http.get(`${V2}/ZAERA_MIRROR_SRV/MRPExceptionMessage?$inlinecount=allpages&$top=1`);
    assert.equal(mrp.status, 200);
    assert.equal(mrp.data.d.__count, "214");

    const rate = await http.get(
      `${V2}/ZAERA_MIRROR_SRV/MaterialConsumptionRate(Material='MAT-48219',Plant='1010')`,
    );
    assert.equal(rate.data.d.ConsumptionQuantityPerHour, "50.000");

    const bp = `${V2}/API_BUSINESS_PARTNER`;
    const krieger = await http.get(`${bp}/A_Supplier('1000234')`);
    const halim = await http.get(`${bp}/A_Supplier('1000871')`);
    assert.equal(krieger.data.d.ComplianceStatus, "APPROVED");
    assert.equal(halim.data.d.ComplianceStatus, "UNDER_REVIEW");
  });
});

describe("CSRF and ETag exactly as S/4HANA writes (IR-02)", () => {
  test("a write without a fetched CSRF token is refused", async () => {
    const response = await http.post(`${PO}/A_PurchaseOrder`, { PurchaseOrderType: "UB" });

    assert.equal(response.status, 403);
    assert.equal(response.header("x-csrf-token"), "Required");
  });

  test("a token from another session is refused", async () => {
    const mine = await csrf();
    const theirs = await csrf();
    const response = await http.post(`${PO}/A_PurchaseOrder`, { PurchaseOrderType: "UB" }, {
      "x-csrf-token": theirs["x-csrf-token"],
      cookie: mine.cookie,
    });

    assert.equal(response.status, 403);
  });

  test("a stock transport order is created by deep insert with a Mirror-assigned number", async () => {
    await reset();
    const headers = await csrf();
    const response = await http.post(`${PO}/A_PurchaseOrder`, {
      PurchaseOrderType: "UB",
      CompanyCode: "1010",
      SupplyingPlant: "1020",
      PurchasingOrganization: "1010",
      PurchasingGroup: "001",
      DocumentCurrency: "USD",
      to_PurchaseOrderItem: [
        {
          PurchaseOrderItem: "10",
          Material: "MAT-48219",
          Plant: "1010",
          OrderQuantity: "600",
          PurchaseOrderQuantityUnit: "PC",
          to_ScheduleLine: [
            {
              ScheduleLine: "1",
              ScheduleLineDeliveryDate: epoch("2026-10-05T00:00:00.000Z"),
              ScheduleLineOrderQuantity: "600",
            },
          ],
        },
      ],
    }, headers);

    assert.equal(response.status, 201, JSON.stringify(response.data));
    const created = response.data.d;
    assert.match(created.PurchaseOrder, /^45\d{8}$/);
    assert.notEqual(created.PurchaseOrder, "4500001234");
    const readBack = await http.get(
      `${PO}/A_PurchaseOrder('${created.PurchaseOrder}')/to_PurchaseOrderItem?$expand=to_ScheduleLine`,
    );
    const item = readBack.data.d.results[0];
    assert.equal(item.PurchaseOrder, created.PurchaseOrder);
    assert.equal(item.to_ScheduleLine.results[0].PurchasingDocument, created.PurchaseOrder);
    assert.equal(item.to_ScheduleLine.results[0].ScheduleLineOrderQuantity, "600.000");
  });

  test("a schedule-line PATCH needs If-Match and rejects a stale ETag", async () => {
    await reset();
    const headers = await csrf();
    const url = `${PO}/A_PurchaseOrderScheduleLine(PurchasingDocument='4500001234',PurchasingDocumentItem='10',ScheduleLine='1')`;
    const current = await http.get(url);
    const etag = current.data.d.__metadata.etag;
    assert.ok(etag, "schedule line carries an ETag");
    const change = { ScheduleLineDeliveryDate: epoch("2026-10-14T00:00:00.000Z") };

    const missing = await http.patch(url, change, headers);
    assert.equal(missing.status, 428);

    const ok = await http.patch(url, change, { ...headers, "if-match": etag });
    assert.equal(ok.status, 204, JSON.stringify(ok.data));

    const stale = await http.patch(url, change, { ...headers, "if-match": etag });
    assert.equal(stale.status, 412);

    const after = await http.get(url);
    assert.equal(after.data.d.ScheduleLineDeliveryDate, epoch("2026-10-14T00:00:00.000Z"));
  });

  test("a schedule-line split is a POST of a new line under the item", async () => {
    await reset();
    const headers = await csrf();
    const item = `${PO}/A_PurchaseOrderItem(PurchaseOrder='4500001234',PurchaseOrderItem='10')`;
    const response = await http.post(`${item}/to_ScheduleLine`, {
      ScheduleLine: "2",
      ScheduleLineDeliveryDate: epoch("2026-10-14T00:00:00.000Z"),
      ScheduleLineOrderQuantity: "960",
    }, headers);

    assert.equal(response.status, 201, JSON.stringify(response.data));
    const lines = await http.get(`${item}/to_ScheduleLine`);
    assert.equal(lines.data.d.results.length, 2);
  });

  test("a goods receipt can be posted as a material document item (movement 101)", async () => {
    await reset();
    const headers = await csrf();
    const response = await http.post(`${V2}/API_MATERIAL_DOCUMENT_SRV/A_MaterialDocumentItem`, {
      Material: "MAT-48219",
      Plant: "1010",
      GoodsMovementType: "101",
      PurchaseOrder: "4500001234",
      PurchaseOrderItem: "10",
      QuantityInEntryUnit: "640",
      EntryUnit: "PC",
    }, headers);

    assert.equal(response.status, 201, JSON.stringify(response.data));
    assert.match(response.data.d.MaterialDocument, /^49\d{8}$/);
  });
});

describe("Reset (FR-ADM-03)", () => {
  test("restores the reference scenario in under 10 seconds", async () => {
    const headers = await csrf();
    await http.delete(`${PO}/A_PurchaseOrder('4500001234')`, headers);

    const started = Date.now();
    const result = await reset();
    const elapsed = Date.now() - started;

    assert.ok(elapsed < 10_000, `reset took ${elapsed} ms`);
    assert.equal(result.value?.t0 ?? result.t0, T0);
    const po = await http.get(`${PO}/A_PurchaseOrder('4500001234')`);
    assert.equal(po.status, 200);
  });

  test("AT-08 fault injection fails the chosen writes only, and reset disarms it", async () => {
    await reset();
    const headers = await csrf();
    const armed = await http.post("/admin/fault", { skip: 1, count: 1, status: 500 }, headers);
    assert.equal(armed.status, 200, JSON.stringify(armed.data));
    const url = `${PO}/A_PurchaseOrderScheduleLine(PurchasingDocument='4500001234',PurchasingDocumentItem='10',ScheduleLine='1')`;
    const write = async () => {
      const current = await http.get(url);
      const change = { ScheduleLineDeliveryDate: epoch("2026-10-15T00:00:00.000Z") };
      return http.patch(url, change, { ...headers, "if-match": current.data.d.__metadata.etag });
    };

    assert.equal((await write()).status, 204);
    assert.equal((await write()).status, 500);
    assert.equal((await write()).status, 204);

    await http.post("/admin/fault", { skip: 0, count: 5, status: 503 }, headers);
    await reset();
    assert.equal((await write()).status, 204);
    const refused = await http.post("/admin/fault", { skip: 0, count: 1, status: 418 }, headers);
    assert.equal(refused.status, 400);
  });

  test("evaluation patches adjust the seed with T0 tokens and reset restores it", async () => {
    await reset();
    const headers = await csrf();
    const stock = `${V2}/API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod?$filter=Material eq 'MAT-48219' and Plant eq '1010'&$format=json`;
    const changes = [
      {
        entity: "A_MatlStkInAcctMod",
        where: { Material: "MAT-48219", Plant: "1010", InventoryStockType: "01" },
        set: { MatlWrhsStkQtyInMatlBaseUnit: "150" },
      },
      {
        entity: "A_Supplier",
        where: { Supplier: "1000871" },
        set: { ComplianceStatus: "APPROVED" },
      },
      {
        entity: "A_PurchaseOrderScheduleLine",
        where: { PurchasingDocument: "4500001234", PurchasingDocumentItem: "10", ScheduleLine: "1" },
        set: { ScheduleLineDeliveryDate: "{T0+3d}" },
      },
      {
        entity: "A_MatlStkInAcctMod",
        where: { Material: "MAT-51002", Plant: "1020", StorageLocation: "102A", InventoryStockType: "01" },
        set: { MatlWrhsStkQtyInMatlBaseUnit: "1200" },
        insert: { Material: "MAT-51002", Plant: "1020", StorageLocation: "102A",
          Batch: "", Supplier: "", Customer: "", WBSElementInternalID: "",
          SDDocument: "", SDDocumentItem: "", InventorySpecialStockType: "",
          InventoryStockType: "01", MaterialBaseUnit: "PC", MatlWrhsStkQtyInMatlBaseUnit: "1200" },
        upsert: true,
      },
    ];

    const applied = await http.post("/admin/patch", { changes: JSON.stringify(changes) }, headers);
    assert.equal(applied.status, 200, JSON.stringify(applied.data));
    const quantity = async () =>
      Number((await http.get(stock)).data.d.results[0].MatlWrhsStkQtyInMatlBaseUnit);
    assert.equal(await quantity(), 150);
    const donor = `${V2}/API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod?$filter=Material eq 'MAT-51002' and Plant eq '1020'&$format=json`;
    const donorRows = async () => (await http.get(donor)).data.d.results;
    assert.equal((await donorRows()).length, 1);
    const again = await http.post("/admin/patch", { changes: JSON.stringify([changes[3]]) }, headers);
    assert.equal(again.status, 200);
    assert.equal((await donorRows()).length, 1);
    const line = await http.get(
      `${PO}/A_PurchaseOrderScheduleLine(PurchasingDocument='4500001234',PurchasingDocumentItem='10',ScheduleLine='1')`,
    );
    assert.equal(line.data.d.ScheduleLineDeliveryDate, epoch("2026-10-08T00:00:00.000Z"));

    const unknown = await http.post(
      "/admin/patch",
      { changes: JSON.stringify([{ entity: "Nope", where: { A: "1" }, set: { B: "2" } }]) },
      headers,
    );
    assert.equal(unknown.status, 400);
    await reset();
    assert.equal(await quantity(), 310);
    assert.equal((await donorRows()).length, 0);
  });

  test("reset rejects a malformed scenario start", async () => {
    const headers = await csrf();
    const response = await http.post("/admin/reset", { t0: "yesterday" }, headers);

    assert.equal(response.status, 400);
  });
});
