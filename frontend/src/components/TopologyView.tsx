import cytoscape, { type Core } from "cytoscape";
import { useEffect, useMemo, useRef } from "react";
import { FIBRE, INK_3, STATUS, flowStyle, latencyInk, loadInk, loadWidth } from "../lib/colors";
import type { Selection } from "../lib/store";
import type { Health, Topology } from "../lib/types";
import { GLYPH } from "./icons";

export interface LinkVis {
  util: number;
  health: Health;
  rtt?: number | null;
  expected?: number;
  admin_up?: boolean;
  /** measured packets/s per direction (live only) - drives the packet dots */
  ab_pps?: number;
  ba_pps?: number;
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
  nodes: Record<string, { health: Health }>;
  strands: StrandVis[];
  colorMode?: "load" | "latency";
  packets?: boolean;
  highlight?: string[] | null;
  selected?: Selection;
  onSelect?: (s: Selection) => void;
  variant?: "live" | "sim";
  className?: string;
}

interface Dot {
  link: string;
  from: string;
  to: string;
  t0: number;
  dur: number;
  color: string;
  lane: number; // strand lane offset index (NaN = centre line)
}

const NODE_SIZE: Record<string, [number, number]> = {
  router: [38, 38],
  switch: [44, 26],
  client: [34, 30],
  server: [34, 30],
};

const NICE = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];

export default function TopologyView(props: Props) {
  const { topology, colorMode = "load", packets = true, variant = "live" } = props;
  const host = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const cyRef = useRef<Core | null>(null);
  const live = useRef(props);
  live.current = props;
  const dots = useRef<Dot[]>([]);
  const acc = useRef<Record<string, number>>({});
  const scale = useRef({ perDot: 10 });

  const linkEnds = useMemo(() => Object.fromEntries(topology.links.map((l) => [l.id, [l.a, l.b] as const])), [topology]);
  const linkByPair = useMemo(() => {
    const m: Record<string, string> = {};
    for (const l of topology.links) {
      m[`${l.a}|${l.b}`] = l.id;
      m[`${l.b}|${l.a}`] = l.id;
    }
    return m;
  }, [topology]);

  // ---------------------------------------------------------------- cytoscape init
  useEffect(() => {
    if (!host.current) return;
    const cy = cytoscape({
      container: host.current,
      elements: [
        ...topology.nodes.map((n) => ({
          data: { id: n.id, label: n.label, type: n.type, hcolor: STATUS.unknown },
          position: { x: n.x, y: n.y },
        })),
        ...topology.links.map((l) => ({
          data: { id: l.id, source: l.a, target: l.b, color: loadInk(0), w: loadWidth(0), ls: "solid" },
        })),
      ],
      layout: { name: "preset" },
      minZoom: 0.3,
      maxZoom: 3,
      wheelSensitivity: 0.25,
      boxSelectionEnabled: false,
      autoungrabify: true,
      style: [
        {
          selector: "node",
          style: {
            "background-color": variant === "sim" ? "#1f3042" : "#273444",
            "background-image": (e: cytoscape.NodeSingular) => GLYPH[e.data("type") as keyof typeof GLYPH],
            "background-fit": "contain",
            "background-clip": "none",
            "background-image-containment": "inside",
            "background-width": "62%",
            "background-height": "62%",
            "border-width": 2.5,
            "border-color": "data(hcolor)",
            "border-style": variant === "sim" ? "dashed" : "solid",
            label: "data(label)",
            color: "#b4b9bf",
            "font-family": "Barlow, sans-serif",
            "font-size": 11.5,
            "text-valign": "bottom",
            "text-margin-y": 5,
            "text-background-color": "#18212b",
            "text-background-opacity": 0.75,
            "text-background-padding": "2px",
            "text-background-shape": "roundrectangle",
            width: (e: cytoscape.NodeSingular) => NODE_SIZE[e.data("type")][0],
            height: (e: cytoscape.NodeSingular) => NODE_SIZE[e.data("type")][1],
            shape: (e: cytoscape.NodeSingular) => (e.data("type") === "router" ? "ellipse" : "round-rectangle"),
          } as cytoscape.Css.Node,
        },
        {
          selector: "edge",
          style: {
            width: "data(w)",
            "line-color": "data(color)",
            "line-style": "data(ls)" as unknown as cytoscape.Css.LineStyle,
            "line-dash-pattern": [6, 5],
            "curve-style": "straight",
            "line-cap": "round",
          } as cytoscape.Css.Edge,
        },
        { selector: "node.sel", style: { "underlay-color": "#e6e4df", "underlay-opacity": 0.22, "underlay-padding": 7, "underlay-shape": "ellipse" } as cytoscape.Css.Node },
        { selector: "edge.sel", style: { "underlay-color": "#e6e4df", "underlay-opacity": 0.2, "underlay-padding": 6 } as cytoscape.Css.Edge },
        { selector: "node.hl", style: { "border-width": 4, "border-color": "#e6e4df" } as cytoscape.Css.Node },
        { selector: "edge.hl", style: { "underlay-color": "#e6e4df", "underlay-opacity": 0.28, "underlay-padding": 5 } as cytoscape.Css.Edge },
        { selector: ".dim", style: { opacity: 0.35 } },
      ],
    });
    cy.on("tap", "node", (e) => live.current.onSelect?.({ kind: "node", id: e.target.id() }));
    cy.on("tap", "edge", (e) => live.current.onSelect?.({ kind: "link", id: e.target.id() }));
    cy.on("tap", (e) => {
      if (e.target === cy) live.current.onSelect?.(null);
    });
    cy.fit(undefined, 34);
    cyRef.current = cy;
    const ro = new ResizeObserver(() => {
      cy.resize();
      cy.fit(undefined, 34);
    });
    ro.observe(host.current);
    return () => {
      ro.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
  }, [topology, variant]);

  // ---------------------------------------------------------------- data -> styles
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      for (const l of topology.links) {
        const v = props.links[l.id];
        const e = cy.getElementById(l.id);
        if (!v) continue;
        const down = v.health === "down" || v.admin_up === false;
        const color = down ? STATUS.down : colorMode === "latency" ? latencyInk(v.rtt ?? null, v.expected ?? 0) : loadInk(v.util);
        e.data({ color, w: down ? 3 : loadWidth(colorMode === "latency" ? 0.25 : v.util), ls: down ? "dashed" : "solid" });
        e.toggleClass("sel", props.selected?.kind === "link" && props.selected.id === l.id);
      }
      for (const n of topology.nodes) {
        const h = props.nodes[n.id]?.health ?? "unknown";
        const e = cy.getElementById(n.id);
        e.data("hcolor", STATUS[h]);
        e.toggleClass("sel", props.selected?.kind === "node" && props.selected.id === n.id);
      }
      const hl = props.highlight;
      cy.elements().removeClass("hl dim");
      if (hl && hl.length > 1) {
        const ids = new Set<string>(hl);
        hl.slice(1).forEach((v, i) => {
          const lid = linkByPair[`${hl[i]}|${v}`];
          if (lid) ids.add(lid);
        });
        cy.elements().forEach((el) => {
          if (ids.has(el.id())) el.addClass("hl");
          else el.addClass("dim");
        });
      }
    });
  }, [props.links, props.nodes, props.selected, props.highlight, colorMode, topology, linkByPair]);

  // ---------------------------------------------------------------- overlay: strands + packets
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const draw = (now: number) => {
      raf = requestAnimationFrame(draw);
      const cy = cyRef.current;
      const cv = canvas.current;
      if (!cy || !cv || !host.current) return;
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const dpr = window.devicePixelRatio || 1;
      const w = host.current.clientWidth;
      const h = host.current.clientHeight;
      if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
        cv.width = Math.round(w * dpr);
        cv.height = Math.round(h * dpr);
        cv.style.width = `${w}px`;
        cv.style.height = `${h}px`;
      }
      const ctx = cv.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const p = live.current;
      const zoom = cy.zoom();
      const pos = (id: string) => cy.getElementById(id).renderedPosition();
      const laneGap = 3.4 * Math.max(0.6, zoom);
      const radius = (id: string) => (Math.max(...(NODE_SIZE[cy.getElementById(id).data("type")] ?? [30, 30])) / 2 + 4) * zoom;

      // geometry helper: point at fraction f along u->v, shifted `lane` lanes to the right
      const at = (u: string, v: string, f: number, lane: number) => {
        const a = pos(u);
        const b = pos(v);
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const len = Math.hypot(dx, dy) || 1;
        const ra = radius(u) / len;
        const rb = radius(v) / len;
        const ff = ra + (1 - ra - rb) * f;
        const nx = -dy / len;
        const ny = dx / len;
        const off = Number.isNaN(lane) ? 0 : lane * laneGap;
        return { x: a.x + dx * ff + nx * off, y: a.y + dy * ff + ny * off, len };
      };

      // lanes: a flow keeps its lane on every link (lane = fibre slot), centred around the link
      const laneOf = (pair: string) => p.pairs.indexOf(pair) - (p.pairs.length - 1) / 2 + 0.0;

      // 1. fibre strands for flows that carry traffic (or all, when highlighted)
      for (const s of p.strands) {
        if (!s.path || s.path.length < 2) continue;
        const st = flowStyle(p.pairs, s.pair);
        const active = s.offered_mbps > 0;
        ctx.strokeStyle = st.hex;
        ctx.globalAlpha = active ? 0.95 : 0.4;
        ctx.lineWidth = active ? 2.2 : 1.3;
        ctx.setLineDash(st.dash.map((d) => d * Math.max(0.8, zoom)));
        const lane = laneOf(s.pair);
        ctx.beginPath();
        for (let i = 0; i < s.path.length - 1; i++) {
          // draw relative to a canonical direction so lanes stay on a consistent side
          const u = s.path[i];
          const v = s.path[i + 1];
          const lid = linkByPair[`${u}|${v}`];
          const canonical = lid ? linkEnds[lid][0] === u : true;
          const ln = canonical ? lane : -lane;
          const a = at(u, v, 0, ln);
          const b = at(u, v, 1, ln);
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
        }
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      // 2. packet dots, rate = measured packets/s from interface counters
      if (packets && p.variant !== "sim") {
        let maxPps = 1;
        for (const v of Object.values(p.links)) maxPps = Math.max(maxPps, v.ab_pps ?? 0, v.ba_pps ?? 0);
        const target = maxPps / 14; // the busiest direction shows ~14 dots/s
        scale.current.perDot = NICE.find((n) => n >= target) ?? 1000;
        const perDot = scale.current.perDot;
        for (const [lid, v] of Object.entries(p.links)) {
          const [a, b] = linkEnds[lid] ?? [];
          if (!a) continue;
          for (const [from, to, pps] of [
            [a, b, v.ab_pps ?? 0],
            [b, a, v.ba_pps ?? 0],
          ] as const) {
            if (pps <= 0 || v.health === "down") continue;
            const key = `${lid}|${from}`;
            acc.current[key] = (acc.current[key] ?? Math.random()) + (pps / perDot) * dt;
            while (acc.current[key] >= 1) {
              acc.current[key] -= 1;
              // attribute the dot to a flow in proportion to the flows' known offered rates on
              // this directed hop; whatever the counters show beyond that (probes, control) is grey
              const crossing = p.strands.filter((s) => s.offered_mbps > 0 && s.path && hopIn(s.path, from, to));
              const flowPps = crossing.map((s) => (s.offered_mbps * 1e6) / (1200 * 8));
              const sum = flowPps.reduce((x, y) => x + y, 0);
              const other = Math.max(0, pps - sum);
              let r = Math.random() * (sum + other);
              let color = INK_3;
              let lane = NaN;
              for (let i = 0; i < crossing.length; i++) {
                if (r < flowPps[i]) {
                  color = flowStyle(p.pairs, crossing[i].pair).hex;
                  const canonical = linkEnds[lid][0] === from;
                  lane = canonical ? laneOf(crossing[i].pair) : -laneOf(crossing[i].pair);
                  break;
                }
                r -= flowPps[i];
              }
              const lat = v.rtt != null ? v.rtt / 2 : 2;
              dots.current.push({ link: lid, from, to, t0: now, dur: Math.min(2600, 650 + lat * 18), color, lane });
            }
          }
        }
        if (dots.current.length > 1500) dots.current.splice(0, dots.current.length - 1500);
        const keep: Dot[] = [];
        const r = 2.6 * Math.sqrt(Math.max(0.5, zoom));
        for (const d of dots.current) {
          const f = (now - d.t0) / d.dur;
          if (f >= 1) continue;
          keep.push(d);
          const pt = at(d.from, d.to, f, d.lane);
          ctx.beginPath();
          ctx.fillStyle = d.color;
          ctx.arc(pt.x, pt.y, r, 0, Math.PI * 2);
          ctx.fill();
          ctx.lineWidth = 1;
          ctx.strokeStyle = "#18212b";
          ctx.stroke();
        }
        dots.current = keep;
      }
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [packets, linkEnds, linkByPair]);

  return (
    <div className={`relative ${props.className ?? ""}`}>
      {/* cytoscape forces position:relative on its container, so it lives inside an absolute wrapper */}
      <div className="absolute inset-0">
        <div ref={host} className="h-full w-full" />
      </div>
      <canvas ref={canvas} className="pointer-events-none absolute inset-0" aria-hidden="true" />
      {packets && variant === "live" && (
        <PacketScale perDot={() => scale.current.perDot} />
      )}
    </div>
  );
}

function hopIn(path: string[], from: string, to: string): boolean {
  for (let i = 0; i < path.length - 1; i++) if (path[i] === from && path[i + 1] === to) return true;
  return false;
}

function PacketScale({ perDot }: { perDot: () => number }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const id = setInterval(() => {
      if (ref.current) ref.current.textContent = String(perDot());
    }, 500);
    return () => clearInterval(id);
  }, [perDot]);
  return (
    <div className="hint pointer-events-none absolute right-3 bottom-2 rounded bg-[#18212bcc] px-2 py-1">
      1 dot ≈ <span ref={ref} className="num text-ink-2">10</span> packets, from interface counters
    </div>
  );
}

export function FlowLegend({ pairs, strands }: { pairs: string[]; strands: StrandVis[] }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
      {pairs.map((p, i) => {
        const s = strands.find((x) => x.pair === p);
        const st = FIBRE[i % FIBRE.length];
        const active = (s?.offered_mbps ?? 0) > 0;
        return (
          <span key={p} className="inline-flex items-center gap-1.5 text-[12.5px]" style={{ opacity: active ? 1 : 0.6 }}>
            <svg width="26" height="8" aria-hidden="true">
              <line x1="1" y1="4" x2="25" y2="4" stroke={st.hex} strokeWidth="2.4" strokeDasharray={st.dash.join(" ")} />
            </svg>
            <span className="text-ink-2">{p.replace(">", " → ")}</span>
            <span className="num text-ink-3">{active ? `${s!.offered_mbps.toFixed(1)} Mbit/s` : "probes only"}</span>
          </span>
        );
      })}
    </div>
  );
}
