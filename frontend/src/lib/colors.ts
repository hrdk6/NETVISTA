import type { Health } from "./types";

// Status colours are reserved for status. Flow identity uses TIA-598 fibre colours
// (#1 blue, #12 aqua, #10 violet, #4 brown), validated together on the dark panel:
// all pairs pass the normal-vision floor; CVD separation sits in the 6-8 warn band, so
// colour is never the only cue: charts add a dash pattern per flow, and on the map a flow
// keeps a fixed lane, is named in the hover card, and hovering its legend chip isolates it.
export const STATUS: Record<Health, string> = {
  ok: "#0ca30c",
  degraded: "#fab219",
  down: "#d03b3b",
  unknown: "#6b7785",
};

export const STATUS_LABEL: Record<Health, string> = {
  ok: "Healthy",
  degraded: "Degraded",
  down: "Down",
  unknown: "No data yet",
};

export const FIBRE = [
  { name: "blue", hex: "#3987e5", dash: [] as number[] },
  { name: "aqua", hex: "#199e70", dash: [7, 4] },
  { name: "violet", hex: "#b05ec4", dash: [2, 3] },
  { name: "brown", hex: "#a86b3c", dash: [9, 3, 2, 3] },
];

/** Colour follows the entity: a flow keeps its fibre for the whole session. */
export function flowStyle(pairs: string[], pair: string) {
  const i = Math.max(0, pairs.indexOf(pair));
  return FIBRE[i % FIBRE.length];
}
