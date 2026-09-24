// Write db/seed/aera.mirror-MRPExceptionMessage.csv (SRD 6.6.3: 214 MRP messages, 6
// actionable). Deterministic: the same file on every run. Actionable means the proposed
// shift exceeds the default tolerance MRP_TOLERANCE_IN_DAYS=3 / _OUT_DAYS=15 (SRD 6.23).
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const OUTPUT = join(dirname(fileURLToPath(import.meta.url)), "..", "db", "seed", "aera.mirror-MRPExceptionMessage.csv");
export const TOTAL = 214;

// [material, PO, element date offset (days), rescheduling offset (days)] — all bring-forward.
export const ACTIONABLE = [
  ["MAT-48219", "4500001234", 0, -4],
  ["MAT-51002", "4500001240", 1, -4],
  ["MAT-33871", "4500001251", 2, -3],
  ["MAT-20114", "4500001262", 1, -5],
  ["MAT-60417", "4500001273", 3, -2],
  ["MAT-72055", "4500001284", 5, -1],
];

const TEXT = { 10: "Bring process forward", 15: "Postpone process" };

export function rows() {
  const lines = [];
  let seed = 20260924;
  const random = () => {
    seed = (seed * 1103515245 + 12345) % 2147483648;
    return seed / 2147483648;
  };
  const token = (days) => (days === 0 ? "{T0}" : `{T0${days > 0 ? "+" : ""}${days}d}`);
  const push = (material, plant, element, number, elementDays, shiftDays) =>
    lines.push([
      String(lines.length + 1).padStart(10, "0"), material, plant, element, "10", String(number),
      TEXT[number], token(elementDays), token(elementDays + shiftDays), "{T0-6h}",
    ].join(","));

  for (const [material, po, elementDays, rescheduleDays] of ACTIONABLE) {
    push(material, "1010", po, 10, elementDays, rescheduleDays - elementDays);
  }
  while (lines.length < TOTAL) {
    const index = lines.length;
    const outward = random() < 0.4;
    // Within tolerance: in-shift 1-3 days, out-shift 1-15 days.
    const shift = outward ? 1 + Math.floor(random() * 15) : -(1 + Math.floor(random() * 3));
    const material = `MAT-9${String(1000 + Math.floor(random() * 40)).padStart(4, "0")}`;
    const plant = random() < 0.7 ? "1010" : "1020";
    push(material, plant, String(4500002000 + index), outward ? 15 : 10, 2 + Math.floor(random() * 20), shift);
  }
  return [
    "MRPExceptionMessageID,Material,Plant,MRPElement,MRPElementItem,MRPExceptionNumber,MRPExceptionText,MRPElementDate,MRPReschedulingDate,CreationDateTime",
    ...lines,
  ].join("\n") + "\n";
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  writeFileSync(OUTPUT, rows());
  console.log(`wrote ${OUTPUT}`);
}
