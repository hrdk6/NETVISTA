import type { Health } from "./types";

// Status colours are reserved for status. Flow identity uses TIA-598 fibre colours
// (#1 blue, #12 aqua, #10 violet, #4 brown), validated together on the dark panel:
// all pairs pass the normal-vision floor; CVD separation sits in the 6-8 warn band, so
// every flow is ALSO identified by a dash pattern and a direct label (never colour alone).
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

export const INK = "#e6e4df";
export const INK_3 = "#848d97";
export const PANEL = "#202b37";
export const SIM = "#8fb7d9";

/** Link load: one neutral ramp (dim -> bright) plus width, so hue stays free for status/flows. */
export function loadInk(util: number): string {
  const u = Math.max(0, Math.min(1, util));
  const lo = [0x45, 0x55, 0x66];
  const hi = [0xf2, 0xf0, 0xea];
  const t = Math.pow(u, 0.6);
  const c = lo.map((l, i) => Math.round(l + (hi[i] - l) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

export function loadWidth(util: number): number {
  return 2.5 + 6 * Math.min(1, Math.max(0, util));
}

/** Latency mode: brightness by how far measured RTT exceeds the designed RTT. */
export function latencyInk(rtt: number | null, expected: number): string {
  if (rtt == null) return loadInk(0);
  const over = Math.max(0, rtt - expected) / Math.max(4, expected);
  return loadInk(Math.min(1, over));
}
