// Add twelve synthetic historical POs and receipts to the Mirror seed (FR-LRN-01).
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const seed = join(dirname(fileURLToPath(import.meta.url)), '..', 'db', 'seed');
const purchaseOrders = [];
const items = [];
const lines = [];
const headers = ['MaterialDocumentYear,MaterialDocument,PostingDate'];
const receipts = [
  'MaterialDocumentYear,MaterialDocument,MaterialDocumentItem,Material,Plant,StorageLocation,GoodsMovementType,PurchaseOrder,PurchaseOrderItem,MaterialBaseUnit,QuantityInBaseUnit,EntryUnit,QuantityInEntryUnit',
];
let document = 5100000000;
for (let index = 0; index < 12; index += 1) {
  const po = String(4500003000 + index);
  const offset = 14 * (index + 1);
  purchaseOrders.push(`${po},1010,NB,05,{T0-${offset + 7}d},1000234,1010,001,{T0-${offset + 7}d},USD,`);
  items.push(`${po},10,Historical brake caliper,1010,101A,100,PC,USD,42.500,1,true,0,MAT-48219`);
  lines.push(`${po},10,1,1,{T0-${offset}d},PC,100,{T0-${offset}d},100`);
  const parts = index % 4 === 3 ? [50, 50] : [100];
  for (const [part, quantity] of parts.entries()) {
    document += 1;
    const delay = index % 4 + part;
    const postingOffset = offset - delay;
    headers.push(`2026,${document},{T0-${postingOffset}d}`);
    receipts.push(`2026,${document},1,MAT-48219,1010,101A,101,${po},10,PC,${quantity},PC,${quantity}`);
  }
}
for (const [file, rows] of [
  ['aera.mirror.s4-A_PurchaseOrder.csv', purchaseOrders],
  ['aera.mirror.s4-A_PurchaseOrderItem.csv', items],
  ['aera.mirror.s4-A_PurchaseOrderScheduleLine.csv', lines],
]) {
  const target = join(seed, file);
  const prior = readFileSync(target, 'utf8');
  if (prior.includes('4500003000')) throw new Error(`${file}: historical rows already present`);
  writeFileSync(target, prior.trimEnd() + '\n' + rows.join('\n') + '\n');
}
writeFileSync(join(seed, 'aera.mirror.s4-A_MaterialDocumentHeader.csv'), headers.join('\n') + '\n');
writeFileSync(join(seed, 'aera.mirror.s4-A_MaterialDocumentItem.csv'), receipts.join('\n') + '\n');
