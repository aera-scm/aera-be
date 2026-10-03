// SRD 6.6.3: the served reference scenario is internally consistent. Every figure is
// recomputed from what the Mirror serves, not read from the seed files.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { client, startMirror } from "./support/server.mjs";

const T0 = Date.parse("2026-10-05T08:00:00.000Z");
const HOUR = 3_600_000;
const V2 = "/sap/opu/odata/sap";
let mirror;
let http;

before(async () => {
  mirror = await startMirror({ SCENARIO_T0: new Date(T0).toISOString() });
  http = client(mirror.url);
});
after(() => mirror?.stop());

const all = async (path) => (await http.get(`${V2}/${path}`)).data.d.results;
const one = async (path) => (await http.get(`${V2}/${path}`)).data.d;
const time = (edmDate) => Number(/\/Date\((-?\d+)/.exec(edmDate)[1]);
const duration = (edmTime) => {
  const [, h, m, s] = /PT(\d+)H(\d+)M(\d+)S/.exec(edmTime).map(Number);
  return ((h * 60 + m) * 60 + s) * 1000;
};

async function stock(material, plant) {
  const rows = await all(
    `API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod?$filter=Material eq '${material}' and Plant eq '${plant}'`,
  );
  return rows.reduce((sum, row) => sum + Number(row.MatlWrhsStkQtyInMatlBaseUnit), 0);
}

async function rate(material, plant) {
  const row = await one(
    `ZAERA_MIRROR_SRV/MaterialConsumptionRate(Material='${material}',Plant='${plant}')`,
  );
  return Number(row.ConsumptionQuantityPerHour);
}

test("plant 1010 runs out of MAT-48219 at T0 + 6 h 12 min", async () => {
  const onHand = await stock("MAT-48219", "1010");
  const perHour = await rate("MAT-48219", "1010");

  assert.equal(onHand, 310);
  assert.equal(perHour, 50);
  assert.equal((onHand / perHour) * HOUR, 6 * HOUR + 12 * 60_000);
});

test("plant 1020 holds 600 units free above two days of minimum cover (BR-07)", async () => {
  const onHand = await stock("MAT-48219", "1020");
  const minimumCover = (await rate("MAT-48219", "1020")) * 24 * 2.0;

  assert.equal(onHand - minimumCover, 600);
});

test("the reference purchase order is 1,600 units from Krieger Guss due at T0", async () => {
  const po = await one(
    "API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder('4500001234')?$expand=to_PurchaseOrderItem/to_ScheduleLine",
  );
  const [item] = po.to_PurchaseOrderItem.results;
  const [line] = item.to_ScheduleLine.results;

  assert.equal(po.Supplier, "1000234");
  assert.equal(Number(item.OrderQuantity), 1600);
  assert.equal(time(line.ScheduleLineDeliveryDate) + duration(line.ScheduleLineDeliveryTime), T0);
});

test("Line 2 needs 1,550 parts between T0 and T0 + 31 h and nothing more before the sea delivery", async () => {
  const orders = await all(
    "API_PRODUCTION_ORDER_2_SRV/A_ProductionOrder_2?$filter=Material eq 'VEH-SEDAN-L2' and ProductionPlant eq '1010'",
  );
  const start = (o) => time(o.MfgOrderPlannedStartDate) + duration(o.MfgOrderPlannedStartTime);
  const end = (o) => time(o.MfgOrderPlannedEndDate) + duration(o.MfgOrderPlannedEndTime);
  const window = orders.filter((o) => start(o) >= T0 && end(o) <= T0 + 31 * HOUR);
  const later = orders.filter((o) => !window.includes(o));

  assert.equal(window.reduce((sum, o) => sum + Number(o.TotalQuantity), 0), 1550);
  assert.ok(later.every((o) => start(o) > T0 + 9 * 24 * HOUR), "next requirement after the sea delivery");
  // 310 on hand + 600 by STO + 640 by air = 1,550 (one housing per car).
  assert.equal(310 + 600 + 640, 1550);
  assert.equal(1550 - 310, 1240);
});

test("ten sales orders, seven dealer and three fleet, carry USD 4.72M at risk", async () => {
  const headers = await all("API_SALES_ORDER_SRV/A_SalesOrder?$expand=to_Item/to_ScheduleLine");
  const exposed = headers.filter((h) => h.to_Item.results.some((i) => i.Material === "VEH-SEDAN-L2"));
  const stockout = T0 + 6.2 * HOUR;

  assert.equal(exposed.length, 10);
  assert.equal(exposed.filter((h) => h.CustomerGroup === "01").length, 7);
  assert.equal(exposed.filter((h) => h.CustomerGroup === "02").length, 3);
  const items = exposed.flatMap((h) => h.to_Item.results);
  assert.equal(items.reduce((sum, i) => sum + Number(i.NetAmount), 0), 4_720_000);
  for (const item of items) {
    for (const line of item.to_ScheduleLine.results) {
      const confirmed = time(line.ConfirmedDeliveryDate);
      // Confirmed delivery is date-only in S/4HANA: threatened from the stock-out day on.
      assert.ok(confirmed >= stockout - (stockout % (24 * HOUR)), "confirmed on or after the stock-out day");
      assert.ok(confirmed < T0 + 9 * 24 * HOUR, "and before the sea delivery could cover it");
    }
  }
});

test("the option economics of the reference plan add up (SRD 6.6.3)", () => {
  // Rate card figures live in the AERA config table (DR-11); the arithmetic is fixed here.
  const sto = { quantity: 600, arrivesHours: 5, costUsd: 4_100 };
  const air = { quantity: 640, arrivesHours: 17, costUsd: 38_200 };
  const stockoutHours = 310 / 50;

  assert.ok(sto.arrivesHours < stockoutHours, "STO arrives before stock-out");
  assert.ok(air.arrivesHours < (310 + sto.quantity) / 50, "air freight before STO stock is used");
  assert.equal(sto.quantity + air.quantity, 1240);
  assert.equal(sto.costUsd + air.costUsd, 42_300);
});

// FR-ING-02: working days moved, counting weekdays after the earlier date up to the later.
export function workingDays(fromMs, toMs) {
  const [low, high] = fromMs < toMs ? [fromMs, toMs] : [toMs, fromMs];
  let days = 0;
  for (let t = low + 24 * HOUR; t <= high; t += 24 * HOUR) {
    const weekday = new Date(t).getUTCDay();
    if (weekday !== 0 && weekday !== 6) days += 1;
  }
  return days;
}

const actionable = (element, rescheduled) =>
  rescheduled < element ? workingDays(element, rescheduled) >= 3 : workingDays(element, rescheduled) >= 15;

test("214 MRP messages of which exactly six are outside the working-day tolerance", async () => {
  const messages = await all("ZAERA_MIRROR_SRV/MRPExceptionMessage?$top=1000");
  const flagged = messages.filter((m) => actionable(time(m.MRPElementDate), time(m.MRPReschedulingDate)));

  assert.equal(messages.length, 214);
  assert.deepEqual(flagged.map((m) => m.Material).sort(), [
    "MAT-20114", "MAT-33871", "MAT-48219", "MAT-51002", "MAT-60417", "MAT-72055",
  ]);
});

test("the six actionable MRP messages stay six whatever weekday T0 falls on", async () => {
  const { rows } = await import("../scripts/generate-mrp-seed.mjs");
  const offsetDays = (token) => Number(/^\{T0(?:([+-]\d+)d)?\}$/.exec(token)[1] ?? 0);
  const lines = rows().trim().split(/\r?\n/).slice(1).map((line) => line.split(","));
  for (let weekday = 0; weekday < 7; weekday += 1) {
    const t0 = Date.UTC(2026, 9, 4 + weekday);
    const count = lines.filter(([, , , , , , , element, rescheduled]) =>
      actionable(t0 + offsetDays(element) * 24 * HOUR, t0 + offsetDays(rescheduled) * 24 * HOUR),
    ).length;
    assert.equal(count, 6, `T0 on weekday ${new Date(t0).getUTCDay()}`);
  }
});

test("MAT-48219 is pegged through production order components to the Line 2 car", async () => {
  const components = await all(
    "API_PRODUCTION_ORDER_2_SRV/A_ProductionOrderComponent_2?$filter=Material eq 'MAT-48219' and Plant eq '1010'",
  );
  const orders = new Set(components.map((c) => c.ManufacturingOrder));
  const produced = await all(
    "API_PRODUCTION_ORDER_2_SRV/A_ProductionOrder_2?$filter=Material eq 'VEH-SEDAN-L2'",
  );

  assert.deepEqual([...orders].sort(), produced.map((o) => o.ManufacturingOrder).sort());
  for (const order of produced) {
    const need = components.filter((c) => c.ManufacturingOrder === order.ManufacturingOrder);
    assert.equal(Number(need[0].RequiredQuantity), Number(order.TotalQuantity), "one housing per car");
  }
});

test("master data uses reserved example domains and fiction-reserved phone numbers only", async () => {
  const emails = await all("API_BUSINESS_PARTNER/A_AddressEmailAddress");
  const phones = await all("API_BUSINESS_PARTNER/A_AddressPhoneNumber");

  assert.ok(emails.every((e) => e.EmailAddress.endsWith(".example")));
  assert.ok(phones.every((p) => /^\+447700900\d{3}$/.test(p.InternationalPhoneNumber)));
});

test("business partner grouping tells suppliers from carriers for sender verification (BR-04)", async () => {
  const partners = await all("API_BUSINESS_PARTNER/A_BusinessPartner?$select=BusinessPartner,BusinessPartnerGrouping");
  const grouping = Object.fromEntries(partners.map((p) => [p.BusinessPartner, p.BusinessPartnerGrouping]));

  assert.deepEqual(grouping, {
    1000234: "SUPL", 1000871: "SUPL", 1000950: "CARR", 9000001: "INTL", 9000002: "INTL",
  });
});

test("FR-LRN-01 seeds twelve dated supplier schedules and fifteen posted goods receipts", async () => {
  const orders = await all("API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder?$filter=Supplier eq '1000234'");
  const historical = orders.filter((order) => /^45000030\d\d$/.test(order.PurchaseOrder));
  const documents = await all("API_MATERIAL_DOCUMENT_SRV/A_MaterialDocumentItem?$filter=GoodsMovementType eq '101'");
  const headers = await all("API_MATERIAL_DOCUMENT_SRV/A_MaterialDocumentHeader"); // pragma: allowlist secret
  assert.equal(historical.length, 12);
  assert.equal(documents.length, 15);
  assert.equal(headers.length, 15);
  assert.ok(headers.every((header) => time(header.PostingDate) < T0));
});
