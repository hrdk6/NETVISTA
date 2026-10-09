export const dash = "–";

export function ms(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return dash;
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`;
  return `${v.toFixed(digits)} ms`;
}

export function pct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return `${v.toFixed(digits)}%`;
}

export function frac(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return `${(v * 100).toFixed(digits)}%`;
}

export function bps(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return dash;
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)} Mbit/s`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)} kbit/s`;
  return `${v.toFixed(0)} bit/s`;
}

export function mbps(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return `${v.toFixed(digits)} Mbit/s`;
}

export function num(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return dash;
  return v.toFixed(digits);
}

export function clock(t: number | null | undefined): string {
  if (!t) return dash;
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0").slice(0, 1);
}

export function ago(t: number | null | undefined, now = Date.now() / 1000): string {
  if (!t) return "never";
  const s = Math.max(0, now - t);
  if (s < 60) return `${s.toFixed(0)} s ago`;
  if (s < 3600) return `${(s / 60).toFixed(0)} min ago`;
  return `${(s / 3600).toFixed(1)} h ago`;
}

export const pairLabel = (pair: string) => pair.replace(">", " → ");
export const pathLabel = (p: string[] | null | undefined) => (p && p.length ? p.join(" – ") : dash);
/** Only the router part of a path: c1-sw1-r1-r2-r5-sw2-srv1 -> r1 r2 r5 */
export const corePath = (p: string[] | null | undefined) => (p ? p.filter((n) => /^r\d/.test(n)).join(" · ") : dash);
