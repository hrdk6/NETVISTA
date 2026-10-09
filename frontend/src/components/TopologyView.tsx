import cytoscape, { type Core } from "cytoscape";
import { useEffect, useMemo, useRef, useState } from "react";
import { FIBRE, STATUS, STATUS_LABEL, flowStyle } from "../lib/colors";
import { useStore, type Selection } from "../lib/store";
import type { Health, NodeType, Topology } from "../lib/types";
import { DEVICE, DEVICE_IMAGE, ledOffset } from "./devices";

/*
 * The live network map. Four layers, bottom to top:
 *   1. floor canvas   dot-grid floor plan, site zones, cables, flow fibres, packet comets
 *   2. cytoscape      the equipment artwork (pan / zoom / click / hover hit-testing)
 *   3. overlay canvas LEDs, labels, health rings, cable breaks, selection, effects,
 *                     AI markers (dotted = inferred by the AI layer, never a raw status)
 *   4. HTML           hover card, packet scale
 * Packets travel *under* the equipment, so they visibly enter and leave each device.
 * Everything that moves or lights up is driven by a measurement: comet rate = interface
 * packets/s, comet speed = measured link latency, activity LED = node packets/s,
 * status LED / rings / breaks = probe-measured health. Static art never carries state.
 */

export interface LinkVis {
  util: number;
  health: Health;
  rtt?: number | null;
  expected?: number;
  admin_up?: boolean;
  /** measured packets/s per direction (live only) - drives the comets */
  ab_pps?: number;
  ba_pps?: number;
  /** what the learned-baseline detector finds unusual on this cable (live only) */
  anomalies?: string[];
}

/** The AI layer's most likely root cause, pinned to its element on the map. */
export interface MapCause {
  kind: "node" | "link";
  id: string;
  title: string;
  confidence: string;
}

/** Predicted impact if this element fails (Assure failure analysis). Keys: "link:<id>" / "node:<id>". */
export interface RiskVis {
  /** 0..1, the worst failure scores 1 */
  norm: number;
  /** the failure cuts some flow off whatever the routing does (single point of failure) */
  spof: boolean;
  /** intents predicted to break */
  breaks: string[];
  /** of those, the ones a better routing would have kept */
  avoidable: string[];
}

export interface NodeVis {
  health: Health;
  /** measured packets/s through the node (live only) - drives the activity LED */
  pps?: number;
}

export interface StrandVis {
  pair: string;
  path: string[] | null;
  offered_mbps: number;
}

interface Props {
  topology: Topology;
  pairs: string[];
  links: Record<string, LinkVis>;
  nodes: Record<string, NodeVis>;
  strands: StrandVis[];
  colorMode?: "load" | "latency" | "risk";
  /** predicted failure impact per element, drawn in "risk" colour mode */
  risk?: Record<string, RiskVis> | null;
  packets?: boolean;
  highlight?: string[] | null;
  selected?: Selection;
  onSelect?: (s: Selection) => void;
  variant?: "live" | "sim";
  className?: string;
  /** flow to emphasise (legend hover); others are dimmed */
  focusPair?: string | null;
  /** draw fibres for probe-only pairs too */
  showIdle?: boolean;
  /** AI diagnosis to pin on the map */
  cause?: MapCause | null;
}

interface Comet {
  lid: string;
  from: string;
  to: string;
  t0: number;
  dur: number;
  pair: string | null;
}

interface Fx {
  kind: "cut" | "heal" | "node-down" | "anomaly";
  id: string;
  t0: number;
}

type Hover = { kind: "node" | "link"; id: string; x: number; y: number } | null;

const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
const DRAW_ON_MS = 950;
const FADE_MS = 700;

function hex(c: string): [number, number, number] {
  const h = c.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function mix(a: string, b: string, t: number): string {
  const A = hex(a);
  const B = hex(b);
  const k = Math.max(0, Math.min(1, t));
  return `rgb(${A.map((v, i) => Math.round(v + (B[i] - v) * k)).join(",")})`;
}
function rgba(c: string, a: number): string {
  const [r, g, b] = hex(c);
  return `rgba(${r},${g},${b},${a})`;
}

function splitLabel(label: string, type: NodeType, ip?: string): [string, string] {
  const m = label.match(/^(.*?)\s*\((.*)\)\s*$/);
  const name = m ? m[1] : label;
  if (type === "client" || type === "server") return [name, ip ?? type];
  if (m) return [name, m[2]];
  return [name, type === "router" ? "router" : "Open vSwitch"];
}

export default function TopologyView(props: Props) {
  const { topology, packets = true, variant = "live" } = props;
  const plan = useStore((s) => s.plan);
  const wrap = useRef<HTMLDivElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const floor = useRef<HTMLCanvasElement>(null);
  const over = useRef<HTMLCanvasElement>(null);
  const cyRef = useRef<Core | null>(null);
  const fitRef = useRef<() => void>(() => {});
  const live = useRef(props);
  live.current = props;
  const comets = useRef<Comet[]>([]);
  const acc = useRef<Record<string, number>>({});
  const perDot = useRef(10);
  const fx = useRef<Fx[]>([]);
  const drawOn = useRef(new Map<string, number>());
  const fades = useRef<{ pair: string; path: string[]; t0: number }[]>([]);
  const prevPath = useRef(new Map<string, string>());
  const prevHealth = useRef(new Map<string, Health>());
  const scaleEl = useRef<HTMLSpanElement>(null);
  const [hover, setHover] = useState<Hover>(null);
  const reduced = useRef(typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);

  const nodeById = useMemo(() => Object.fromEntries(topology.nodes.map((n) => [n.id, n])), [topology]);
  const linkById = useMemo(() => Object.fromEntries(topology.links.map((l) => [l.id, l])), [topology]);
  const linkByPair = useMemo(() => {
    const m: Record<string, string> = {};
    for (const l of topology.links) {
      m[`${l.a}|${l.b}`] = l.id;
      m[`${l.b}|${l.a}`] = l.id;
    }
    return m;
  }, [topology]);

  // labels and zones derived once per topology
  const labels = useMemo(
    () => Object.fromEntries(topology.nodes.map((n) => [n.id, splitLabel(n.label, n.type, plan?.hosts[n.id]?.ip)])),
    [topology, plan],
  );
  const zones = useMemo(() => {
    const out: { title: string; sub: string; x0: number; y0: number; x1: number; y1: number }[] = [];
    // generous top padding leaves room for the caption, bottom padding for device labels
    const bbox = (ids: string[], px: number, top: number, bottom: number) => {
      const ns = ids.map((i) => nodeById[i]);
      return {
        x0: Math.min(...ns.map((n) => n.x)) - px,
        x1: Math.max(...ns.map((n) => n.x)) + px,
        y0: Math.min(...ns.map((n) => n.y)) - top,
        y1: Math.max(...ns.map((n) => n.y)) + bottom,
      };
    };
    for (const sw of topology.nodes.filter((n) => n.type === "switch")) {
      const members = [sw.id];
      for (const l of topology.links) {
        const other = l.a === sw.id ? l.b : l.b === sw.id ? l.a : null;
        if (other && nodeById[other].type !== "router") members.push(other);
      }
      const subnet = plan?.subnets.find((s) => s.kind === "lan" && s.links.some((lid) => linkById[lid]?.a === sw.id || linkById[lid]?.b === sw.id));
      out.push({ title: sw.label.replace(/\s*switch$/i, ""), sub: subnet ? `LAN ${subnet.cidr}` : "LAN", ...bbox(members, 60, 84, 68) });
    }
    const routers = topology.nodes.filter((n) => n.type === "router").map((n) => n.id);
    if (routers.length) out.push({ title: "Routed core", sub: `${routers.length} routers, policy routing`, ...bbox(routers, 62, 84, 70) });
    return out;
  }, [topology, plan, nodeById, linkById]);

  // ---------------------------------------------------------------- cytoscape: equipment + hit testing
  useEffect(() => {
    if (!host.current) return;
    const cy = cytoscape({
      container: host.current,
      elements: [
        ...topology.nodes.map((n) => ({ data: { id: n.id, type: n.type, op: 1 }, position: { x: n.x, y: n.y } })),
        ...topology.links.map((l) => ({ data: { id: l.id, source: l.a, target: l.b } })),
      ],
      layout: { name: "preset" },
      minZoom: 0.3,
      maxZoom: 3,
      boxSelectionEnabled: false,
      autoungrabify: true,
      autounselectify: true,
      style: [
        { selector: "core", style: { "active-bg-opacity": 0 } as unknown as cytoscape.Css.Core },
        {
          selector: "node",
          style: {
            "background-opacity": 0,
            "border-width": 0,
            "background-image": (e: cytoscape.NodeSingular) => DEVICE_IMAGE[e.data("type") as NodeType],
            "background-fit": "contain",
            "background-clip": "none",
            "background-image-opacity": "data(op)",
            "background-image-containment": "over",
            width: (e: cytoscape.NodeSingular) => DEVICE[e.data("type") as NodeType].size[0],
            height: (e: cytoscape.NodeSingular) => DEVICE[e.data("type") as NodeType].size[1],
            shape: "round-rectangle",
            "overlay-opacity": 0,
            label: "",
          } as unknown as cytoscape.Css.Node,
        },
        // edges are invisible hit areas; the cables themselves are painted on the floor canvas
        { selector: "edge", style: { width: 18, "line-opacity": 0, "curve-style": "straight", "overlay-opacity": 0 } as unknown as cytoscape.Css.Edge },
      ],
    });
    const pointer = (on: boolean) => {
      if (host.current) host.current.style.cursor = on ? "pointer" : "";
    };
    cy.on("tap", "node", (e) => live.current.onSelect?.({ kind: "node", id: e.target.id() }));
    cy.on("tap", "edge", (e) => live.current.onSelect?.({ kind: "link", id: e.target.id() }));
    cy.on("tap", (e) => {
      if (e.target === cy) live.current.onSelect?.(null);
    });
    cy.on("mouseover", "node, edge", (e) => {
      pointer(true);
      setHover({ kind: e.target.isNode() ? "node" : "link", id: e.target.id(), x: e.renderedPosition.x, y: e.renderedPosition.y });
    });
    cy.on("mousemove", "node, edge", (e) => setHover((h) => (h ? { ...h, x: e.renderedPosition.x, y: e.renderedPosition.y } : h)));
    cy.on("mouseout", "node, edge", () => {
      pointer(false);
      setHover(null);
    });
    cy.on("viewport", () => setHover(null));
    const fitAll = () => {
      const W = cy.width();
      const H = cy.height();
      if (!W || !H) return;
      // frame devices plus the zone margins around them (zones reach ~90 px beyond the devices)
      const xs = topology.nodes.map((n) => n.x);
      const ys = topology.nodes.map((n) => n.y);
      const b = { x0: Math.min(...xs) - 70, x1: Math.max(...xs) + 70, y0: Math.min(...ys) - 92, y1: Math.max(...ys) + 78 };
      const z = Math.max(0.3, Math.min(1.5, (W - 24) / (b.x1 - b.x0), (H - 24) / (b.y1 - b.y0)));
      cy.zoom(z);
      cy.pan({ x: W / 2 - ((b.x0 + b.x1) / 2) * z, y: H / 2 - ((b.y0 + b.y1) / 2) * z });
    };
    fitRef.current = fitAll;
    fitAll();
    cyRef.current = cy;
    const ro = new ResizeObserver(() => {
      cy.resize();
      fitAll();
    });
    ro.observe(host.current);
    return () => {
      ro.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, [topology, variant]);

  // ---------------------------------------------------------------- state changes -> styles + one-shot effects
  useEffect(() => {
    const cy = cyRef.current;
    const now = performance.now();
    const hl = props.highlight && props.highlight.length > 1 ? new Set(props.highlight) : null;
    if (cy) {
      cy.batch(() => {
        for (const n of topology.nodes) {
          const h = props.nodes[n.id]?.health ?? "unknown";
          const op = h === "down" ? 0.38 : hl && !hl.has(n.id) ? 0.32 : 1;
          cy.getElementById(n.id).data("op", op);
        }
      });
    }
    // cable cut / heal and node crash effects (only on transitions, never on first sight)
    for (const [id, l] of Object.entries(props.links)) {
      const prev = prevHealth.current.get(`l:${id}`);
      if (prev && prev !== "down" && l.health === "down") fx.current.push({ kind: "cut", id, t0: now });
      if (prev === "down" && l.health !== "down" && l.health !== "unknown") fx.current.push({ kind: "heal", id, t0: now });
      prevHealth.current.set(`l:${id}`, l.health);
      // the AI layer starting to find this cable unusual: one ring, then a steady marker
      const had = prevHealth.current.get(`a:${id}`) === "degraded";
      const has = !!l.anomalies?.length;
      if (has && !had) fx.current.push({ kind: "anomaly", id, t0: now });
      prevHealth.current.set(`a:${id}`, has ? "degraded" : "ok");
    }
    for (const [id, n] of Object.entries(props.nodes)) {
      const prev = prevHealth.current.get(`n:${id}`);
      if (prev && prev !== "down" && n.health === "down") fx.current.push({ kind: "node-down", id, t0: now });
      prevHealth.current.set(`n:${id}`, n.health);
    }
    // a flow drawing itself in: on first appearance (the page's one orchestrated moment) and on every reroute
    for (const s of props.strands) {
      const key = s.path ? s.path.join("|") : "";
      const prev = prevPath.current.get(s.pair);
      if (key && prev !== key && (s.offered_mbps > 0 || prev !== undefined)) {
        drawOn.current.set(s.pair, now);
        if (prev) fades.current.push({ pair: s.pair, path: prev.split("|"), t0: now });
      }
      prevPath.current.set(s.pair, key);
    }
  }, [props.links, props.nodes, props.strands, props.highlight, topology]);

  // ---------------------------------------------------------------- render loop
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    let gridKey = "";
    let gridPattern: CanvasPattern | null = null;

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const cy = cyRef.current;
      const fc = floor.current;
      const oc = over.current;
      const box = wrap.current;
      if (!cy || !fc || !oc || !box) return;
      const p = live.current;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const dpr = window.devicePixelRatio || 1;
      const W = box.clientWidth;
      const H = box.clientHeight;
      for (const cv of [fc, oc]) {
        if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
          cv.width = Math.round(W * dpr);
          cv.height = Math.round(H * dpr);
          cv.style.width = `${W}px`;
          cv.style.height = `${H}px`;
        }
      }
      const f = fc.getContext("2d");
      const o = oc.getContext("2d");
      if (!f || !o) return;
      f.setTransform(dpr, 0, 0, dpr, 0, 0);
      o.setTransform(dpr, 0, 0, dpr, 0, 0);
      o.clearRect(0, 0, W, H);

      const sim = p.variant === "sim";
      const reduce = !!reduced.current;
      const zoom = cy.zoom();
      const pan = cy.pan();
      const zs = Math.max(0.6, Math.min(1.7, zoom));
      const P = (id: string) => {
        const n = nodeById[id];
        return { x: n.x * zoom + pan.x, y: n.y * zoom + pan.y };
      };
      const hl = p.highlight && p.highlight.length > 1 ? p.highlight : null;
      const hlLinks = new Set<string>();
      if (hl) for (let i = 0; i < hl.length - 1; i++) hlLinks.add(linkByPair[`${hl[i]}|${hl[i + 1]}`]);
      const hlNodes = hl ? new Set(hl) : null;

      // ---------------- floor: plan, grid, zones
      f.fillStyle = sim ? "#17263a" : "#151e28";
      f.fillRect(0, 0, W, H);
      const step = 24 * zoom;
      const key = `${step.toFixed(2)}|${sim}`;
      if (key !== gridKey) {
        const t = document.createElement("canvas");
        const s = Math.max(6, Math.round(step * dpr));
        t.width = s;
        t.height = s;
        const tc = t.getContext("2d")!;
        tc.fillStyle = sim ? "#25405c" : "#233141";
        tc.beginPath();
        tc.arc(s / 2, s / 2, Math.max(0.8, 1.05 * dpr), 0, Math.PI * 2);
        tc.fill();
        gridPattern = f.createPattern(t, "repeat");
        gridKey = key;
      }
      if (gridPattern && step >= 7) {
        f.save();
        f.translate(pan.x % step, pan.y % step);
        f.scale(1 / dpr, 1 / dpr);
        f.fillStyle = gridPattern;
        f.fillRect(-step * dpr * 2, -step * dpr * 2, (W + step * 4) * dpr, (H + step * 4) * dpr);
        f.restore();
      }
      for (const z of zones) {
        const x = z.x0 * zoom + pan.x;
        const y = z.y0 * zoom + pan.y;
        const w = (z.x1 - z.x0) * zoom;
        const h = (z.y1 - z.y0) * zoom;
        f.beginPath();
        f.roundRect(x, y, w, h, 16 * zoom);
        f.fillStyle = sim ? "rgba(143,183,217,0.035)" : "rgba(160,190,220,0.028)";
        f.fill();
        f.lineWidth = 1;
        f.setLineDash(sim ? [4, 4] : []);
        f.strokeStyle = sim ? "rgba(143,183,217,0.28)" : "rgba(150,175,200,0.16)";
        f.stroke();
        f.setLineDash([]);
        if (zoom > 0.45) {
          f.font = `600 ${Math.round(12.5 * Math.min(1.15, Math.max(0.85, zoom)))}px "Barlow Semi Condensed", Barlow, sans-serif`;
          f.fillStyle = "#9aa6b2";
          f.fillText(z.title, x + 14 * zoom, y + 20 * zoom);
          f.font = `500 ${Math.round(11 * Math.min(1.1, Math.max(0.85, zoom)))}px Barlow, sans-serif`;
          f.fillStyle = "#63707d";
          f.fillText(z.sub, x + 14 * zoom, y + 20 * zoom + 14);
        }
      }
      // soft vignette, keeps the eye on the middle of the plan
      const vg = f.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.35, W / 2, H / 2, Math.max(W, H) * 0.75);
      vg.addColorStop(0, "rgba(0,0,0,0)");
      vg.addColorStop(1, "rgba(0,0,0,0.28)");
      f.fillStyle = vg;
      f.fillRect(0, 0, W, H);

      // ---------------- lanes (which flows run along which cable, in a stable order)
      const shown = p.strands
        .filter((s) => s.path && s.path.length > 1 && (s.offered_mbps > 0 || p.showIdle || p.focusPair === s.pair))
        .sort((a, b) => p.pairs.indexOf(a.pair) - p.pairs.indexOf(b.pair));
      const lanes: Record<string, string[]> = {};
      for (const s of shown) {
        for (let i = 0; i < s.path!.length - 1; i++) {
          const lid = linkByPair[`${s.path![i]}|${s.path![i + 1]}`];
          if (!lid) continue;
          (lanes[lid] ??= []).includes(s.pair) || lanes[lid].push(s.pair);
        }
      }
      const capW = (lid: string) => (2.2 + Math.min(5, (linkById[lid]?.bw_mbps ?? 50) / 20)) * zs;
      const laneOff = (lid: string, pair: string) => {
        const i = lanes[lid]?.indexOf(pair) ?? -1;
        if (i < 0) return 0;
        const side = i % 2 === 0 ? 1 : -1;
        return side * (capW(lid) / 2 + 2 * zs + (2.4 + Math.floor(i / 2) * 3.4) * zs);
      };
      // a point on link `lid` going from node u towards v, at fraction t, shifted `off` px in the
      // link's canonical frame (so a lane stays on the same physical side in both directions)
      const at = (lid: string, u: string, v: string, t: number, off: number) => {
        const [ca, cb] = [linkById[lid].a, linkById[lid].b];
        const A = P(ca);
        const B = P(cb);
        const len = Math.hypot(B.x - A.x, B.y - A.y) || 1;
        const nx = -(B.y - A.y) / len;
        const ny = (B.x - A.x) / len;
        const U = P(u);
        const V = P(v);
        return { x: U.x + (V.x - U.x) * t + nx * off, y: U.y + (V.y - U.y) * t + ny * off, len };
      };

      // ---------------- cables
      for (const l of topology.links) {
        const v = p.links[l.id];
        if (!v) continue;
        const A = P(l.a);
        const B = P(l.b);
        const down = v.health === "down" || v.admin_up === false;
        const dim = hl && !hlLinks.has(l.id) ? 0.25 : 1;
        const w = capW(l.id);
        const u = Math.max(0, Math.min(1, v.util));
        f.save();
        f.globalAlpha = dim;
        f.lineCap = "round";
        if (p.selected?.kind === "link" && p.selected.id === l.id) {
          f.strokeStyle = "rgba(230,228,223,0.13)";
          f.lineWidth = w + 26 * zs;
          line(f, A, B);
          f.strokeStyle = "rgba(230,228,223,0.5)";
          f.lineWidth = w + 10 * zs;
          f.setLineDash([2 * zs, 5 * zs]);
          f.lineDashOffset = reduced.current ? 0 : -now / 50;
          line(f, A, B);
          f.setLineDash([]);
        }
        // jacket
        f.strokeStyle = "#0a1017";
        f.lineWidth = w + 4.5 * zs;
        line(f, A, B);
        if (down) {
          const mx = (A.x + B.x) / 2;
          const my = (A.y + B.y) / 2;
          const len = Math.hypot(B.x - A.x, B.y - A.y) || 1;
          const gx = ((B.x - A.x) / len) * 9 * zs;
          const gy = ((B.y - A.y) / len) * 9 * zs;
          f.strokeStyle = STATUS.down;
          f.lineWidth = Math.max(2, w * 0.6);
          f.setLineDash([6 * zs, 5 * zs]);
          line(f, A, { x: mx - gx, y: my - gy });
          line(f, { x: mx + gx, y: my + gy }, B);
          f.setLineDash([]);
        } else {
          let core: string;
          let glow = u;
          const rk = p.colorMode === "risk" ? p.risk?.[`link:${l.id}`] : undefined;
          if (p.colorMode === "risk") {
            const n = rk ? Math.max(0, Math.min(1, rk.norm)) : 0;
            core = rk ? mix("#3b4a59", "#ff7a59", Math.pow(n, 0.7)) : "#2c3946";
            glow = n * 0.85;
            if (rk?.spof) {
              f.strokeStyle = "rgba(255,122,89,0.55)";
              f.lineWidth = w + 9 * zs;
              f.setLineDash([3 * zs, 4 * zs]);
              line(f, A, B);
              f.setLineDash([]);
            }
          } else if (p.colorMode === "latency") {
            const over = v.rtt != null && v.expected ? Math.max(0, v.rtt - v.expected) / Math.max(4, v.expected) : 0;
            core = mix("#3a4b5e", "#ffd9a0", Math.min(1, Math.pow(over, 0.6)));
            glow = Math.min(1, over) * 0.8;
          } else {
            core = sim ? mix("#2f4c6a", "#cfe7ff", Math.pow(u, 0.6)) : mix("#36485c", "#eef4fa", Math.pow(u, 0.6));
          }
          if (v.health === "degraded" && p.colorMode !== "risk") core = mix("#6e5a30", STATUS.degraded, 0.45 + 0.45 * u);
          f.strokeStyle = core;
          f.lineWidth = w;
          if (glow > 0.03) {
            f.shadowColor =
              p.colorMode === "risk" ? `rgba(255,122,89,${0.6 * glow})` : p.colorMode === "latency" ? `rgba(255,200,140,${0.6 * glow})` : `rgba(200,225,255,${0.55 * glow})`;
            f.shadowBlur = (4 + 18 * glow) * zs;
          }
          line(f, A, B);
          f.shadowBlur = 0;
          // a bright filament along the core when loaded
          if (u > 0.05 && p.colorMode === "load") {
            f.strokeStyle = `rgba(255,255,255,${0.15 + 0.35 * u})`;
            f.lineWidth = Math.max(0.8, w * 0.22);
            line(f, A, B);
          }
        }
        f.restore();
      }

      // ---------------- flow fibres (with draw-on after a reroute)
      f.save();
      f.lineCap = "round";
      f.lineJoin = "round";
      for (const s of shown) {
        const st = flowStyle(p.pairs, s.pair);
        const path = s.path!;
        const focusDim = (p.focusPair && p.focusPair !== s.pair ? 0.15 : 1) * (p.colorMode === "risk" ? 0.3 : 1);
        const t0 = drawOn.current.get(s.pair);
        const prog = reduce || t0 === undefined ? 1 : Math.min(1, (now - t0) / DRAW_ON_MS);
        if (prog >= 1 && t0 !== undefined) drawOn.current.delete(s.pair);
        const segs: { a: { x: number; y: number }; b: { x: number; y: number }; len: number }[] = [];
        let total = 0;
        for (let i = 0; i < path.length - 1; i++) {
          const lid = linkByPair[`${path[i]}|${path[i + 1]}`];
          if (!lid) continue;
          const off = laneOff(lid, s.pair);
          const a = at(lid, path[i], path[i + 1], 0, off);
          const b = at(lid, path[i], path[i + 1], 1, off);
          const len = Math.hypot(b.x - a.x, b.y - a.y);
          segs.push({ a, b, len });
          total += len;
        }
        let budget = total * easeOut(prog);
        let tip: { x: number; y: number } | null = null;
        f.globalAlpha = focusDim * (s.offered_mbps > 0 ? 0.95 : 0.55);
        f.strokeStyle = st.hex;
        f.lineWidth = (s.offered_mbps > 0 ? 1.9 : 1.3) * zs;
        f.shadowColor = st.hex;
        f.shadowBlur = 6 * zs;
        f.beginPath();
        for (const sg of segs) {
          if (budget <= 0) break;
          const k = Math.min(1, budget / (sg.len || 1));
          f.moveTo(sg.a.x, sg.a.y);
          const e = { x: sg.a.x + (sg.b.x - sg.a.x) * k, y: sg.a.y + (sg.b.y - sg.a.y) * k };
          f.lineTo(e.x, e.y);
          tip = e;
          budget -= sg.len;
        }
        f.stroke();
        f.shadowBlur = 0;
        if (prog < 1 && tip) {
          const g = f.createRadialGradient(tip.x, tip.y, 0, tip.x, tip.y, 9 * zs);
          g.addColorStop(0, "rgba(255,255,255,0.95)");
          g.addColorStop(0.35, rgba(st.hex, 0.8));
          g.addColorStop(1, rgba(st.hex, 0));
          f.fillStyle = g;
          f.beginPath();
          f.arc(tip.x, tip.y, 9 * zs, 0, Math.PI * 2);
          f.fill();
        }
      }
      // the old route fading out
      fades.current = fades.current.filter((fd) => now - fd.t0 < FADE_MS && !reduce);
      for (const fd of fades.current) {
        const st = flowStyle(p.pairs, fd.pair);
        f.globalAlpha = 0.8 * (1 - (now - fd.t0) / FADE_MS);
        f.strokeStyle = st.hex;
        f.lineWidth = 1.6 * zs;
        f.setLineDash([3 * zs, 4 * zs]);
        f.beginPath();
        for (let i = 0; i < fd.path.length - 1; i++) {
          const lid = linkByPair[`${fd.path[i]}|${fd.path[i + 1]}`];
          if (!lid) continue;
          const a = at(lid, fd.path[i], fd.path[i + 1], 0, 0);
          const b = at(lid, fd.path[i], fd.path[i + 1], 1, 0);
          f.moveTo(a.x, a.y);
          f.lineTo(b.x, b.y);
        }
        f.stroke();
        f.setLineDash([]);
      }
      f.restore();

      // ---------------- packets: comets whose rate is the measured packets/s of each direction
      if (packets && !sim && !reduce) {
        let maxPps = 1;
        for (const v of Object.values(p.links)) maxPps = Math.max(maxPps, v.ab_pps ?? 0, v.ba_pps ?? 0);
        perDot.current = NICE.find((n) => n >= maxPps / 16) ?? 5000;
        if (scaleEl.current) scaleEl.current.textContent = String(perDot.current);
        for (const l of topology.links) {
          const v = p.links[l.id];
          if (!v || v.health === "down") continue;
          for (const [from, to, pps] of [
            [l.a, l.b, v.ab_pps ?? 0],
            [l.b, l.a, v.ba_pps ?? 0],
          ] as const) {
            if (pps <= 0) continue;
            const k = `${l.id}|${from}`;
            acc.current[k] = (acc.current[k] ?? Math.random()) + (pps / perDot.current) * dt;
            while (acc.current[k] >= 1) {
              acc.current[k] -= 1;
              // attribute the comet to a flow in proportion to the flows' offered rates on this hop;
              // whatever the counters show beyond that (probes, control traffic) stays a grey speck
              const crossing = shown.filter((s) => s.offered_mbps > 0 && hopIn(s.path!, from, to));
              const w = crossing.map((s) => (s.offered_mbps * 1e6) / (1200 * 8));
              const sum = w.reduce((x, y) => x + y, 0);
              let r = Math.random() * (sum + Math.max(0, pps - sum));
              let pair: string | null = null;
              for (let i = 0; i < crossing.length; i++) {
                if (r < w[i]) {
                  pair = crossing[i].pair;
                  break;
                }
                r -= w[i];
              }
              const lat = v.rtt != null ? v.rtt / 2 : 2;
              comets.current.push({ lid: l.id, from, to, t0: now, dur: Math.max(520, Math.min(2600, 520 + lat * 15)), pair });
            }
          }
        }
        if (comets.current.length > 1600) comets.current.splice(0, comets.current.length - 1600);
        f.save();
        f.globalCompositeOperation = "lighter";
        const keep: Comet[] = [];
        for (const c of comets.current) {
          const t = (now - c.t0) / c.dur;
          if (t >= 1) continue;
          keep.push(c);
          const focusDim = p.focusPair && c.pair !== p.focusPair ? 0.2 : 1;
          const linkDim = hl && !hlLinks.has(c.lid) ? 0.25 : 1;
          if (!c.pair) {
            const pt = at(c.lid, c.from, c.to, t, 0);
            f.globalAlpha = 0.5 * linkDim * (p.focusPair ? 0.4 : 1);
            f.fillStyle = "#b8c6d4";
            f.beginPath();
            f.arc(pt.x, pt.y, 1.4 * zs, 0, Math.PI * 2);
            f.fill();
            continue;
          }
          const off = laneOff(c.lid, c.pair);
          const head = at(c.lid, c.from, c.to, t, off);
          const tailT = Math.max(0, t - (26 * zs) / head.len);
          const tail = at(c.lid, c.from, c.to, tailT, off);
          const col = flowStyle(p.pairs, c.pair).hex;
          f.globalAlpha = focusDim * linkDim;
          const g = f.createLinearGradient(tail.x, tail.y, head.x, head.y);
          g.addColorStop(0, rgba(col, 0));
          g.addColorStop(1, rgba(col, 0.95));
          f.strokeStyle = g;
          f.lineWidth = 3 * zs;
          f.lineCap = "round";
          f.beginPath();
          f.moveTo(tail.x, tail.y);
          f.lineTo(head.x, head.y);
          f.stroke();
          f.fillStyle = "rgba(255,255,255,0.9)";
          f.beginPath();
          f.arc(head.x, head.y, 2 * zs, 0, Math.PI * 2);
          f.fill();
        }
        comets.current = keep;
        f.restore();
      } else {
        comets.current = [];
      }

      // ---------------- overlay: effects on cables
      fx.current = fx.current.filter((e) => e.kind === "cut" ? p.links[e.id]?.health === "down" || now - e.t0 < 1200 : now - e.t0 < 1400);
      for (const l of topology.links) {
        const v = p.links[l.id];
        if (!v || !(v.health === "down" || v.admin_up === false)) continue;
        const A = P(l.a);
        const B = P(l.b);
        const mx = (A.x + B.x) / 2;
        const my = (A.y + B.y) / 2;
        const pulse = reduce ? 0.5 : 0.5 + 0.5 * Math.sin(now / 230);
        const g = o.createRadialGradient(mx, my, 0, mx, my, (14 + 6 * pulse) * zs);
        g.addColorStop(0, rgba(STATUS.down, 0.55 + 0.3 * pulse));
        g.addColorStop(1, rgba(STATUS.down, 0));
        o.fillStyle = g;
        o.beginPath();
        o.arc(mx, my, (14 + 6 * pulse) * zs, 0, Math.PI * 2);
        o.fill();
        // the break: two short bars across the cable
        const len = Math.hypot(B.x - A.x, B.y - A.y) || 1;
        const ux = (B.x - A.x) / len;
        const uy = (B.y - A.y) / len;
        o.strokeStyle = "#ffb3ab";
        o.lineWidth = 2 * zs;
        o.lineCap = "round";
        for (const s of [-1, 1]) {
          const cx = mx + ux * 6 * zs * s;
          const cyy = my + uy * 6 * zs * s;
          o.beginPath();
          o.moveTo(cx - uy * 6 * zs + ux * 2 * zs * s, cyy + ux * 6 * zs + uy * 2 * zs * s);
          o.lineTo(cx + uy * 6 * zs - ux * 2 * zs * s, cyy - ux * 6 * zs - uy * 2 * zs * s);
          o.stroke();
        }
      }
      for (const e of fx.current) {
        if (reduce) break;
        const age = now - e.t0;
        if (e.kind === "cut" && age < 1200) {
          const l = linkById[e.id];
          const A = P(l.a);
          const B = P(l.b);
          ripple(o, (A.x + B.x) / 2, (A.y + B.y) / 2, age / 1200, STATUS.down, 46 * zs);
        } else if (e.kind === "heal") {
          const l = linkById[e.id];
          const t = Math.min(1, age / 1100);
          for (const [from, to] of [
            [l.a, l.b],
            [l.b, l.a],
          ]) {
            const pt = at(l.id, from, to, easeOut(t) * 0.5, 0);
            const g = o.createRadialGradient(pt.x, pt.y, 0, pt.x, pt.y, 12 * zs);
            g.addColorStop(0, rgba("#7cf0a6", 0.9 * (1 - t)));
            g.addColorStop(1, rgba("#7cf0a6", 0));
            o.fillStyle = g;
            o.beginPath();
            o.arc(pt.x, pt.y, 12 * zs, 0, Math.PI * 2);
            o.fill();
          }
        } else if (e.kind === "node-down" && age < 1400) {
          const c = P(e.id);
          ripple(o, c.x, c.y, age / 1400, STATUS.down, 70 * zs);
        } else if (e.kind === "anomaly" && age < 1400) {
          const l = linkById[e.id];
          const A = P(l.a);
          const B = P(l.b);
          ripple(o, (A.x + B.x) / 2, (A.y + B.y) / 2, age / 1400, "#e6e4df", 34 * zs);
        }
      }

      // ---------------- overlay: AI markers on cables the learned baselines find unusual
      for (const l of topology.links) {
        const v = p.links[l.id];
        if (!v?.anomalies?.length || v.health === "down") continue;
        const A = P(l.a);
        const B = P(l.b);
        const mx = (A.x + B.x) / 2;
        const my = (A.y + B.y) / 2;
        const r = 10 * zs;
        o.save();
        o.fillStyle = "rgba(18,26,35,0.88)";
        o.beginPath();
        o.arc(mx, my, r, 0, Math.PI * 2);
        o.fill();
        o.strokeStyle = "rgba(230,228,223,0.85)";
        o.lineWidth = 1.3 * zs;
        o.setLineDash([1.6 * zs, 2.4 * zs]);
        o.lineDashOffset = reduce ? 0 : -now / 140;
        o.beginPath();
        o.arc(mx, my, r, 0, Math.PI * 2);
        o.stroke();
        o.setLineDash([]);
        spark(o, mx, my, 5.2 * zs, "#e6e4df");
        o.restore();
      }

      // ---------------- overlay: devices (rings, selection, LEDs, labels)
      topology.nodes.forEach((n, idx) => {
        const c = P(n.id);
        const d = DEVICE[n.type];
        const hw = (d.size[0] / 2) * zoom;
        const hh = (d.size[1] / 2) * zoom;
        const nv = p.nodes[n.id];
        const health = nv?.health ?? "unknown";
        const dimmed = hlNodes ? !hlNodes.has(n.id) : false;
        o.save();
        o.globalAlpha = dimmed ? 0.35 : 1;
        if (health === "down" || (health === "degraded" && p.colorMode !== "risk")) {
          const col = STATUS[health];
          const period = health === "down" ? 1600 : 2600;
          const ph = reduce ? 0.35 : ((now + idx * 170) % period) / period;
          o.strokeStyle = rgba(col, health === "down" ? 0.85 : 0.6);
          o.lineWidth = 1.6 * zs;
          ellipse(o, c.x, c.y + 2 * zoom, hw + 7 * zs, hh + 6 * zs);
          o.strokeStyle = rgba(col, (1 - ph) * (health === "down" ? 0.7 : 0.45));
          o.lineWidth = 2 * zs;
          ellipse(o, c.x, c.y + 2 * zoom, hw + (7 + 16 * ph) * zs, hh + (6 + 13 * ph) * zs);
        }
        // predicted failure impact (dashed = a prediction, never a measurement)
        const rn = p.colorMode === "risk" ? p.risk?.[`node:${n.id}`] : undefined;
        if (rn && (rn.norm > 0.001 || rn.spof)) {
          const k = Math.max(0, Math.min(1, rn.norm));
          o.strokeStyle = rn.spof ? "#ff7a59" : mix("#7c8a98", "#ff7a59", Math.pow(k, 0.7));
          o.lineWidth = (1.4 + 2.2 * k) * zs;
          o.setLineDash([4 * zs, 3 * zs]);
          ellipse(o, c.x, c.y + 2 * zoom, hw + 9 * zs, hh + 8 * zs);
          o.setLineDash([]);
          if (zoom > 0.45) {
            const tag = rn.spof ? "single point of failure" : rn.breaks.length ? `if it fails: breaks ${rn.breaks.join(", ")}` : "";
            if (tag) {
              o.font = `600 ${Math.round(11 * Math.min(1.1, Math.max(0.85, zoom)))}px "Barlow Semi Condensed", Barlow, sans-serif`;
              o.textAlign = "center";
              const ty = c.y - hh - 14 * zs;
              const tw = o.measureText(tag).width + 12;
              o.fillStyle = "rgba(18,26,35,0.92)";
              o.beginPath();
              o.roundRect(c.x - tw / 2, ty - 10, tw, 17, 3);
              o.fill();
              o.setLineDash([2, 2]);
              o.strokeStyle = "rgba(255,122,89,0.75)";
              o.lineWidth = 1;
              o.stroke();
              o.setLineDash([]);
              o.fillStyle = "#ffc2b3";
              o.fillText(tag, c.x, ty + 2.5);
            }
          }
        }
        if (p.selected?.kind === "node" && p.selected.id === n.id) {
          o.strokeStyle = "rgba(230,228,223,0.9)";
          o.lineWidth = 1.4 * zs;
          o.setLineDash([5 * zs, 5 * zs]);
          o.lineDashOffset = reduce ? 0 : -now / 60;
          ellipse(o, c.x, c.y + 2 * zoom, hw + 11 * zs, hh + 10 * zs);
          o.setLineDash([]);
        }
        // LEDs (scaled with the artwork)
        const led = (which: "status" | "activity", color: string, on: number) => {
          const [dx, dy] = ledOffset(n.type, which);
          const x = c.x + dx * zoom;
          const y = c.y + dy * zoom;
          const r = Math.max(1.3, 2.1 * zoom);
          if (on > 0.02) {
            const g = o.createRadialGradient(x, y, 0, x, y, r * 4.2);
            g.addColorStop(0, rgba(color, 0.6 * on));
            g.addColorStop(1, rgba(color, 0));
            o.fillStyle = g;
            o.beginPath();
            o.arc(x, y, r * 4.2, 0, Math.PI * 2);
            o.fill();
          }
          o.fillStyle = on > 0.02 ? mix(color, "#ffffff", 0.35 * on) : "#1a2734";
          o.beginPath();
          o.arc(x, y, r, 0, Math.PI * 2);
          o.fill();
        };
        const stColor = STATUS[health];
        const stOn = health === "down" ? (reduce ? 1 : Math.sin(now / 160) > 0 ? 1 : 0.15) : health === "unknown" ? 0.25 : 1;
        led("status", stColor, stOn);
        const pps = nv?.pps ?? 0;
        if (!sim) {
          let on = 0;
          if (pps > 0 && health !== "down") {
            const hz = Math.min(14, 1.5 + 3 * Math.log10(1 + pps));
            on = reduce ? 0.8 : ((now / 1000) * hz + idx * 0.37) % 1 < 0.45 ? 1 : 0.12;
          }
          led("activity", "#8fd3ff", on);
        }
        // labels
        if (zoom > 0.38) {
          const [name, role] = labels[n.id] ?? [n.id, ""];
          const y = c.y + hh + 14;
          o.textAlign = "center";
          o.lineJoin = "round";
          o.font = `600 ${Math.round(13 * Math.min(1.1, Math.max(0.85, zoom)))}px "Barlow Semi Condensed", Barlow, sans-serif`;
          o.strokeStyle = sim ? "#17263a" : "#151e28";
          o.lineWidth = 4;
          o.strokeText(name, c.x, y);
          o.fillStyle = health === "down" ? "#ff9d94" : "#e6e4df";
          o.fillText(name, c.x, y);
          if (zoom > 0.55 && role) {
            o.font = `500 ${Math.round(11 * Math.min(1.1, Math.max(0.85, zoom)))}px Barlow, sans-serif`;
            o.strokeText(role, c.x, y + 14);
            o.fillStyle = "#7f8b97";
            o.fillText(role, c.x, y + 14);
          }
        }
        o.restore();
      });

      // ---------------- overlay: the AI's most likely root cause, pinned to its element
      const cz = p.cause;
      if (cz && !sim && (cz.kind === "link" ? linkById[cz.id] : nodeById[cz.id])) {
        let ax: number;
        let ay: number;
        if (cz.kind === "link") {
          const l = linkById[cz.id];
          const A = P(l.a);
          const B = P(l.b);
          ax = (A.x + B.x) / 2;
          ay = (A.y + B.y) / 2 - 12 * zs;
        } else {
          const c = P(cz.id);
          ax = c.x;
          ay = c.y - (DEVICE[nodeById[cz.id].type].size[1] / 2) * zoom - 8 * zs;
        }
        const text = cz.confidence === "high" ? "Likely cause" : `Possible cause (${cz.confidence})`;
        o.save();
        o.font = `600 ${Math.round(12 * Math.min(1.1, Math.max(0.85, zoom)))}px "Barlow Semi Condensed", Barlow, sans-serif`;
        const tw = o.measureText(text).width;
        const padX = 8;
        const w = tw + padX * 2 + 14;
        const h = 21;
        const bx = ax - w / 2;
        const by = ay - 22 - h;
        o.strokeStyle = "rgba(230,228,223,0.55)";
        o.lineWidth = 1;
        o.setLineDash([2, 3]);
        o.beginPath();
        o.moveTo(ax, by + h);
        o.lineTo(ax, ay);
        o.stroke();
        o.setLineDash([]);
        o.fillStyle = "rgba(18,26,35,0.94)";
        o.beginPath();
        o.roundRect(bx, by, w, h, 4);
        o.fill();
        o.setLineDash([1.5, 2.5]);
        o.strokeStyle = "rgba(230,228,223,0.8)";
        o.stroke();
        o.setLineDash([]);
        spark(o, bx + padX + 5, by + h / 2, 5, "#e6e4df");
        o.fillStyle = "#e6e4df";
        o.textAlign = "left";
        o.textBaseline = "middle";
        o.fillText(text, bx + padX + 14, by + h / 2 + 0.5);
        o.fillStyle = "#e6e4df";
        o.beginPath();
        o.arc(ax, ay, 2.2, 0, Math.PI * 2);
        o.fill();
        o.restore();
      }
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [packets, topology, nodeById, linkById, linkByPair, labels, zones]);

  const hoverCard = hover ? <HoverCard hover={hover} props={props} labels={labels} wrap={wrap.current} /> : null;

  return (
    <div ref={wrap} className={`relative overflow-hidden ${props.className ?? ""}`}>
      <canvas ref={floor} className="pointer-events-none absolute inset-0" aria-hidden="true" />
      {/* cytoscape forces position:relative on its container, so it lives inside an absolute wrapper */}
      <div className="absolute inset-0">
        <div ref={host} className="h-full w-full" role="img" aria-label={`Network map of ${topology.name}`} />
      </div>
      <canvas ref={over} className="pointer-events-none absolute inset-0" aria-hidden="true" />
      {hoverCard}
      <button
        type="button"
        onClick={() => fitRef.current()}
        className="absolute top-2.5 right-2.5 rounded border border-line-strong bg-[#151e28d9] px-2 py-1 text-[12px] text-ink-2 hover:text-ink"
        title="Pan and zoom back to the whole network (scroll to zoom, drag to pan)"
      >
        Fit to view
      </button>
      {packets && variant === "live" && (
        <div className="hint pointer-events-none absolute right-3 bottom-2 rounded bg-[#151e28d9] px-2 py-1">
          1 comet ≈ <span ref={scaleEl} className="num text-ink-2">10</span> packets, from interface counters
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ helpers
function line(c: CanvasRenderingContext2D, a: { x: number; y: number }, b: { x: number; y: number }) {
  c.beginPath();
  c.moveTo(a.x, a.y);
  c.lineTo(b.x, b.y);
  c.stroke();
}

function ellipse(c: CanvasRenderingContext2D, x: number, y: number, rx: number, ry: number) {
  c.beginPath();
  c.ellipse(x, y, Math.max(1, rx), Math.max(1, ry), 0, 0, Math.PI * 2);
  c.stroke();
}

function ripple(c: CanvasRenderingContext2D, x: number, y: number, t: number, color: string, maxR: number) {
  const k = easeOut(t);
  c.strokeStyle = rgba(color, 0.8 * (1 - t));
  c.lineWidth = 2.5 * (1 - t) + 0.5;
  c.beginPath();
  c.arc(x, y, 6 + maxR * k, 0, Math.PI * 2);
  c.stroke();
}

/** The AI layer's mark: a four-point spark (same shape as the copilot icon). */
function spark(c: CanvasRenderingContext2D, x: number, y: number, r: number, color: string) {
  const k = r * 0.22;
  c.fillStyle = color;
  c.beginPath();
  c.moveTo(x, y - r);
  c.lineTo(x + k, y - k);
  c.lineTo(x + r, y);
  c.lineTo(x + k, y + k);
  c.lineTo(x, y + r);
  c.lineTo(x - k, y + k);
  c.lineTo(x - r, y);
  c.lineTo(x - k, y - k);
  c.closePath();
  c.fill();
}

function easeOut(t: number) {
  return 1 - Math.pow(1 - Math.max(0, Math.min(1, t)), 3);
}

function hopIn(path: string[], from: string, to: string): boolean {
  for (let i = 0; i < path.length - 1; i++) if (path[i] === from && path[i + 1] === to) return true;
  return false;
}

function HoverCard({ hover, props, labels, wrap }: { hover: NonNullable<Hover>; props: Props; labels: Record<string, [string, string]>; wrap: HTMLDivElement | null }) {
  const W = wrap?.clientWidth ?? 800;
  const H = wrap?.clientHeight ?? 600;
  const left = Math.min(hover.x + 16, W - 236);
  const top = Math.min(hover.y + 16, H - 160);
  let title = "";
  let sub = "";
  let health: Health = "unknown";
  const rows: [string, string][] = [];
  let unusual: string[] = [];
  if (hover.kind === "link") {
    const spec = props.topology.links.find((l) => l.id === hover.id);
    const v = props.links[hover.id];
    if (!spec || !v) return null;
    title = `${labels[spec.a]?.[0] ?? spec.a} to ${labels[spec.b]?.[0] ?? spec.b}`;
    sub = `${spec.bw_mbps} Mbit/s, ${spec.delay_ms} ms each way by design`;
    health = v.health;
    rows.push(["Load", `${(v.util * 100).toFixed(1)}%`]);
    if (v.rtt != null) rows.push(["Round trip", `${v.rtt.toFixed(1)} ms`]);
    const flows = props.strands.filter((s) => s.offered_mbps > 0 && s.path && s.path.some((n, i) => i < s.path!.length - 1 && ((n === spec.a && s.path![i + 1] === spec.b) || (n === spec.b && s.path![i + 1] === spec.a))));
    if (flows.length) rows.push(["Carrying", flows.map((f) => f.pair.replace(">", " → ")).join(", ")]);
    unusual = v.anomalies ?? [];
    const rk = props.risk?.[`link:${hover.id}`];
    if (rk) rows.push(["If it fails", rk.spof ? "cuts flows off" : rk.breaks.length ? `breaks ${rk.breaks.join(", ")}` : "every intent holds"]);
  } else {
    const n = props.topology.nodes.find((x) => x.id === hover.id);
    const v = props.nodes[hover.id];
    if (!n) return null;
    [title, sub] = labels[n.id] ?? [n.id, n.type];
    health = v?.health ?? "unknown";
    if (v?.pps != null) rows.push(["Packets/s", v.pps.toFixed(0)]);
    const rk = props.risk?.[`node:${hover.id}`];
    if (rk) rows.push(["If it fails", rk.spof ? "cuts flows off" : rk.breaks.length ? `breaks ${rk.breaks.join(", ")}` : "every intent holds"]);
  }
  return (
    <div className="pointer-events-none absolute z-10 w-[220px] rounded-md border border-line-strong bg-[#121a23f2] px-3 py-2 text-[12.5px] shadow-[0_8px_24px_rgba(0,0,0,0.35)]" style={{ left, top }}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-cond text-[13.5px] font-semibold text-ink">{title}</span>
        <span className="inline-flex items-center gap-1 text-[11.5px] text-ink-2">
          <span className="h-1.5 w-1.5 rounded-full" style={{ background: STATUS[health] }} />
          {STATUS_LABEL[health]}
        </span>
      </div>
      <div className="text-[11.5px] text-ink-3">{sub}</div>
      {rows.length > 0 && (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          {rows.map(([k, val]) => (
            <div key={k} className="contents">
              <dt className="text-ink-3">{k}</dt>
              <dd className="num text-right text-ink">{val}</dd>
            </div>
          ))}
        </dl>
      )}
      {unusual.length > 0 && (
        <div className="mt-1.5 border-t border-dotted border-line-strong pt-1.5 text-[12px] text-ink-2">
          <span className="text-ink-3">Unusual vs learned normal: </span>
          {unusual.join("; ")}
        </div>
      )}
      <div className="mt-1 text-[11px] text-ink-3">{props.variant === "sim" ? "simulated" : "measured live"}, click for details</div>
    </div>
  );
}

// ------------------------------------------------------------------ legends
export function FlowLegend({
  pairs,
  strands,
  focus,
  onFocus,
}: {
  pairs: string[];
  strands: StrandVis[];
  focus?: string | null;
  onFocus?: (pair: string | null) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5" role="list" aria-label="Flows">
      {pairs.map((p, i) => {
        const s = strands.find((x) => x.pair === p);
        const st = FIBRE[i % FIBRE.length];
        const active = (s?.offered_mbps ?? 0) > 0;
        return (
          <button
            key={p}
            role="listitem"
            onMouseEnter={() => onFocus?.(p)}
            onMouseLeave={() => onFocus?.(null)}
            onFocus={() => onFocus?.(p)}
            onBlur={() => onFocus?.(null)}
            className={`inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-[12.5px] transition-colors ${
              focus === p ? "border-ink-2 bg-raised" : "border-transparent hover:border-line-strong"
            }`}
            title={active ? "Hover to follow this flow on the map" : "Probe traffic only; hover to show its path"}
          >
            <span className="relative h-[3px] w-5 rounded-full" style={{ background: st.hex, boxShadow: `0 0 6px ${st.hex}`, opacity: active ? 1 : 0.55 }} />
            <span className={active ? "text-ink" : "text-ink-3"}>{p.replace(">", " → ")}</span>
            <span className="num text-ink-3">{active ? `${s!.offered_mbps.toFixed(1)} Mbit/s` : "probes"}</span>
          </button>
        );
      })}
    </div>
  );
}

export function MapLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-ink-3">
      <span className="inline-flex items-center gap-1.5">
        <svg width="28" height="10" aria-hidden="true">
          <line x1="2" y1="5" x2="26" y2="5" stroke="#0a1017" strokeWidth="8" strokeLinecap="round" />
          <line x1="2" y1="5" x2="26" y2="5" stroke="#eef4fa" strokeWidth="3.5" strokeLinecap="round" />
        </svg>
        brighter cable carries more load, thicker has more capacity
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Led color={STATUS.ok} /> <Led color={STATUS.degraded} /> <Led color={STATUS.down} /> status LED
      </span>
      <span className="inline-flex items-center gap-1.5">
        <Led color="#8fd3ff" /> activity LED blinks with measured packets/s
      </span>
    </div>
  );
}

function Led({ color }: { color: string }) {
  return <span aria-hidden="true" className="inline-block h-2 w-2 rounded-full" style={{ background: color, boxShadow: `0 0 6px ${color}` }} />;
}
