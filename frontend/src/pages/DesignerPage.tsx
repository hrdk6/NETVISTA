import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DEVICE, DEVICE_IMAGE } from "../components/devices";
import { api } from "../lib/api";
import { act, useStore } from "../lib/store";
import type { NodeType } from "../lib/types";

// The topology designer: draw a network, check it, then boot it as real Linux routers.
// Everything here edits a plain topology JSON (the same format as topologies/*.json); the
// backend validates it with the same rules the emulation enforces (POST /api/topologies/check).

interface DNode {
  id: string;
  type: NodeType;
  label: string;
  x: number;
  y: number;
}
interface DLink {
  a: string;
  b: string;
  bw_mbps: number;
  delay_ms: number;
  jitter_ms?: number;
  loss_pct?: number;
  queue_pkts?: number;
}
interface DTraffic {
  src: string;
  dst: string;
  rate_mbps: number;
}
interface Design {
  name: string;
  description: string;
  nodes: DNode[];
  links: DLink[];
  traffic: DTraffic[];
}
interface LibItem {
  file: string;
  user: boolean;
  current: boolean;
  name: string;
  description: string;
  nodes?: number;
  links?: number;
  routers?: number;
  hosts?: number;
  flows?: number;
  error?: string;
}
interface Report {
  ok: boolean;
  errors: string[];
  warnings: string[];
  name?: string;
  stats?: Record<string, number>;
  flows?: { pair: string; candidates: number; disjoint_paths: number | null; design_rtt_ms: number | null; shortest: string[] | null; bottleneck_mbps: number | null }[];
  spofs?: { scenario: string; label: string; pairs: string[]; edge: boolean }[];
  subnets?: { cidr: string; kind: string; routers: Record<string, string>; hosts: Record<string, string> }[];
}

type Sel = { kind: "node"; id: string } | { kind: "link"; i: number } | null;
type View = { x: number; y: number; w: number; h: number };

const PREFIX: Record<NodeType, string> = { router: "r", switch: "sw", client: "c", server: "srv" };
const TYPE_LABEL: Record<NodeType, string> = { router: "Router", switch: "Switch (Open vSwitch)", client: "Client", server: "Server" };
const isAccess = (d: Design, l: DLink) => {
  const t = (id: string) => d.nodes.find((n) => n.id === id)?.type;
  return !(t(l.a) === "router" && t(l.b) === "router");
};

function nextId(d: Design, type: NodeType): string {
  const p = PREFIX[type];
  const used = new Set(d.nodes.map((n) => n.id));
  for (let i = 1; i < 1000; i++) if (!used.has(`${p}${i}`)) return `${p}${i}`;
  return `${p}x`;
}

function fitView(d: Design): View {
  if (!d.nodes.length) return { x: -100, y: -100, w: 1200, h: 600 };
  const xs = d.nodes.map((n) => n.x);
  const ys = d.nodes.map((n) => n.y);
  const pad = 110;
  const x0 = Math.min(...xs) - pad;
  const y0 = Math.min(...ys) - pad;
  const w = Math.max(600, Math.max(...xs) - Math.min(...xs) + 2 * pad);
  const h = Math.max(360, Math.max(...ys) - Math.min(...ys) + 2 * pad + 30);
  return { x: x0, y: y0, w, h };
}

const BLANK: Design = {
  name: "my-network",
  description: "",
  nodes: [
    { id: "c1", type: "client", label: "Client 1", x: 0, y: 150 },
    { id: "r1", type: "router", label: "R1", x: 180, y: 150 },
    { id: "r2", type: "router", label: "R2", x: 400, y: 40 },
    { id: "r3", type: "router", label: "R3", x: 400, y: 260 },
    { id: "r4", type: "router", label: "R4", x: 620, y: 150 },
    { id: "srv1", type: "server", label: "Server 1", x: 800, y: 150 },
  ],
  links: [
    { a: "c1", b: "r1", bw_mbps: 100, delay_ms: 0.5 },
    { a: "r1", b: "r2", bw_mbps: 50, delay_ms: 5 },
    { a: "r1", b: "r3", bw_mbps: 50, delay_ms: 8 },
    { a: "r2", b: "r4", bw_mbps: 30, delay_ms: 5 },
    { a: "r3", b: "r4", bw_mbps: 50, delay_ms: 8 },
    { a: "r4", b: "srv1", bw_mbps: 100, delay_ms: 0.5 },
  ],
  traffic: [{ src: "c1", dst: "srv1", rate_mbps: 6 }],
};

function fromRaw(raw: Record<string, unknown>): Design {
  const r = raw as { name?: string; description?: string; nodes?: DNode[]; links?: DLink[]; traffic?: DTraffic[]; defaults?: Partial<DLink> };
  const def = r.defaults ?? {};
  return {
    name: r.name ?? "design",
    description: r.description ?? "",
    nodes: (r.nodes ?? []).map((n) => ({ id: n.id, type: n.type, label: n.label ?? n.id, x: n.x ?? 0, y: n.y ?? 0 })),
    links: (r.links ?? []).map((l) => ({
      a: l.a,
      b: l.b,
      bw_mbps: l.bw_mbps ?? def.bw_mbps ?? 100,
      delay_ms: l.delay_ms ?? def.delay_ms ?? 1,
      ...(l.jitter_ms ? { jitter_ms: l.jitter_ms } : {}),
      ...(l.loss_pct ? { loss_pct: l.loss_pct } : {}),
      ...(l.queue_pkts ?? def.queue_pkts ? { queue_pkts: l.queue_pkts ?? def.queue_pkts } : {}),
    })),
    traffic: r.traffic ?? [],
  };
}

export default function DesignerPage() {
  const topologyFile = useStore((s) => s.topologyFile);
  const [lib, setLib] = useState<LibItem[]>([]);
  const [design, setDesignRaw] = useState<Design>(BLANK);
  const [history, setHistory] = useState<Design[]>([]);
  const [source, setSource] = useState<string | null>(null);
  const [sel, setSel] = useState<Sel>(null);
  const [mode, setMode] = useState<"select" | "connect">("select");
  const [from, setFrom] = useState<string | null>(null);
  const [view, setView] = useState<View>(fitView(BLANK));
  const [report, setReport] = useState<Report | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const svg = useRef<SVGSVGElement>(null);
  const drag = useRef<{ kind: "node"; id: string; dx: number; dy: number; moved: boolean } | { kind: "pan"; x: number; y: number; v: View } | null>(null);

  const setDesign = useCallback((next: Design | ((d: Design) => Design), record = true) => {
    setDesignRaw((cur) => {
      const n = typeof next === "function" ? (next as (d: Design) => Design)(cur) : next;
      if (record && n !== cur) setHistory((h) => [...h.slice(-49), cur]);
      return n;
    });
  }, []);

  const loadLib = () => void api.get<LibItem[]>("/api/topologies").then(setLib).catch(() => {});
  useEffect(loadLib, []);

  const open = async (file: string) => {
    const raw = await act(() => api.get<Record<string, unknown>>(`/api/topologies/file?file=${encodeURIComponent(file)}`));
    if (!raw) return;
    const d = fromRaw(raw);
    setDesign(d);
    setSource(file);
    setSel(null);
    setView(fitView(d));
  };
  // start with the deployed topology, so the first thing on screen is the network that is running
  useEffect(() => {
    if (topologyFile) void open(topologyFile);
  }, [topologyFile]); // eslint-disable-line react-hooks/exhaustive-deps

  // design checks: debounced, on every edit
  useEffect(() => {
    const id = setTimeout(() => {
      api
        .post<Report>("/api/topologies/check", { topology: design })
        .then(setReport)
        .catch((e) => setReport({ ok: false, errors: [e instanceof Error ? e.message : String(e)], warnings: [] }));
    }, 350);
    return () => clearTimeout(id);
  }, [design]);

  // ---------------------------------------------------------------- pointer handling
  const toModel = (e: { clientX: number; clientY: number }) => {
    const el = svg.current!;
    const pt = el.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    const m = el.getScreenCTM();
    const p = m ? pt.matrixTransform(m.inverse()) : pt;
    return { x: p.x, y: p.y };
  };
  const onNodeDown = (e: React.PointerEvent, id: string) => {
    e.stopPropagation();
    if (mode === "connect") {
      if (!from) setFrom(id);
      else if (from !== id) {
        const exists = design.links.some((l) => (l.a === from && l.b === id) || (l.a === id && l.b === from));
        if (!exists) {
          const types = [design.nodes.find((n) => n.id === from)!.type, design.nodes.find((n) => n.id === id)!.type];
          const core = types[0] === "router" && types[1] === "router";
          setDesign((d) => ({ ...d, links: [...d.links, { a: from, b: id, bw_mbps: core ? 50 : 100, delay_ms: core ? 5 : 0.5 }] }));
          setSel({ kind: "link", i: design.links.length });
        }
        setFrom(null);
      }
      return;
    }
    const p = toModel(e);
    const n = design.nodes.find((x) => x.id === id)!;
    drag.current = { kind: "node", id, dx: p.x - n.x, dy: p.y - n.y, moved: false };
    setHistory((h) => [...h.slice(-49), design]);
    (e.target as Element).setPointerCapture?.(e.pointerId);
    setSel({ kind: "node", id });
  };
  const onBgDown = (e: React.PointerEvent) => {
    drag.current = { kind: "pan", x: e.clientX, y: e.clientY, v: view };
    setSel(null);
    setFrom(null);
  };
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    if (d.kind === "node") {
      const p = toModel(e);
      d.moved = true;
      const x = Math.round((p.x - d.dx) / 10) * 10;
      const y = Math.round((p.y - d.dy) / 10) * 10;
      setDesign((cur) => ({ ...cur, nodes: cur.nodes.map((n) => (n.id === d.id ? { ...n, x, y } : n)) }), false);
    } else {
      const el = svg.current!;
      const k = d.v.w / el.clientWidth;
      setView({ ...d.v, x: d.v.x - (e.clientX - d.x) * k, y: d.v.y - (e.clientY - d.y) * k });
    }
  };
  const onUp = () => {
    drag.current = null;
  };
  const onWheel = (e: React.WheelEvent) => {
    const p = toModel(e);
    const f = e.deltaY > 0 ? 1.1 : 1 / 1.1;
    setView((v) => ({ x: p.x - (p.x - v.x) * f, y: p.y - (p.y - v.y) * f, w: v.w * f, h: v.h * f }));
  };

  // ---------------------------------------------------------------- edits
  const add = (type: NodeType) => {
    const id = nextId(design, type);
    const n: DNode = { id, type, label: type === "router" ? id.toUpperCase() : `${TYPE_LABEL[type].split(" ")[0]} ${id.replace(/\D/g, "")}`, x: Math.round(view.x + view.w / 2), y: Math.round(view.y + view.h / 2) };
    setDesign((d) => ({ ...d, nodes: [...d.nodes, n] }));
    setSel({ kind: "node", id });
  };
  const remove = useCallback(() => {
    if (!sel) return;
    if (sel.kind === "node") {
      setDesign((d) => ({
        ...d,
        nodes: d.nodes.filter((n) => n.id !== sel.id),
        links: d.links.filter((l) => l.a !== sel.id && l.b !== sel.id),
        traffic: d.traffic.filter((t) => t.src !== sel.id && t.dst !== sel.id),
      }));
    } else {
      setDesign((d) => ({ ...d, links: d.links.filter((_, i) => i !== sel.i) }));
    }
    setSel(null);
  }, [sel, setDesign]);
  const undo = useCallback(() => {
    setHistory((h) => {
      if (!h.length) return h;
      setDesignRaw(h[h.length - 1]);
      return h.slice(0, -1);
    });
    setSel(null);
  }, []);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if ((e.key === "Delete" || e.key === "Backspace") && sel) {
        e.preventDefault();
        remove();
      } else if (e.key === "Escape") {
        setFrom(null);
        setMode("select");
      } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        undo();
      }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [sel, remove, undo]);

  const save = async () => {
    setBusy(true);
    const r = await act(() => api.post<{ file: string }>("/api/topologies/save", { topology: design }), `Saved to topologies/user/`);
    setBusy(false);
    if (r) {
      setSource(r.file);
      loadLib();
    }
  };
  const deploy = async () => {
    setBusy(true);
    const r = await act(() => api.post<{ file: string }>("/api/topologies/deploy", { topology: design }), "Deploying: the new network is booting in Mininet");
    setBusy(false);
    setConfirm(false);
    if (r) location.hash = "#live";
  };

  const nodeById = useMemo(() => Object.fromEntries(design.nodes.map((n) => [n.id, n])), [design.nodes]);
  const selNode = sel?.kind === "node" ? nodeById[sel.id] : null;
  const selLink = sel?.kind === "link" ? design.links[sel.i] : null;
  const clients = design.nodes.filter((n) => n.type === "client");
  const servers = design.nodes.filter((n) => n.type === "server");
  const scale = view.w / 1100;

  return (
    <div className="grid min-h-full gap-3 p-3 xl:grid-cols-[280px_minmax(0,1fr)_360px]">
      {/* ------------------------------------------------ library */}
      <aside className="flex min-w-0 flex-col gap-3">
        <section className="panel p-4">
          <h1 className="panel-title">Topology designer</h1>
          <p className="hint mt-1">
            Draw a network, check it, then deploy it: every router becomes a Linux network namespace, every switch an Open vSwitch bridge, every cable a veth pair
            shaped by tc. Not a simulation of a network: a real one.
          </p>
          <button
            className="btn mt-3 w-full justify-center"
            onClick={() => {
              setDesign(BLANK);
              setSource(null);
              setView(fitView(BLANK));
            }}
          >
            New design
          </button>
        </section>
        <section className="panel">
          <h2 className="panel-title border-b border-line px-4 py-3">Library</h2>
          <ul className="divide-y divide-line">
            {lib.map((t) => (
              <li key={t.file} className={`px-4 py-2.5 ${source === t.file ? "bg-[#243142]" : ""}`}>
                <div className="flex items-baseline justify-between gap-2">
                  <span className="font-cond text-[14px] font-semibold">{t.name}</span>
                  <span className="text-[11px] text-ink-3">{t.current ? "running now" : t.user ? "saved" : "built-in"}</span>
                </div>
                {t.error ? (
                  <p className="text-[12px] text-[#ff9d94]">{t.error}</p>
                ) : (
                  <p className="text-[12px] text-ink-3">
                    {t.routers} routers · {t.hosts} hosts · {t.links} links · {t.flows} flows
                  </p>
                )}
                <div className="mt-1.5 flex gap-2">
                  <button className="btn btn-sm" onClick={() => void open(t.file)}>
                    Open
                  </button>
                  {!t.current && !t.error && (
                    <button
                      className="btn btn-sm"
                      onClick={() =>
                        act(() => api.post("/api/topologies/deploy", { file: t.file }), `Deploying ${t.name}: booting it in Mininet`).then((r) => {
                          if (r) location.hash = "#live";
                        })
                      }
                    >
                      Deploy
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
      </aside>

      {/* ------------------------------------------------ canvas */}
      <section className="panel flex min-h-[640px] min-w-0 flex-col overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
          {(["router", "switch", "client", "server"] as NodeType[]).map((t) => (
            <button key={t} className="btn btn-sm" onClick={() => add(t)} title={`Add a ${TYPE_LABEL[t].toLowerCase()}`}>
              + {t === "switch" ? "Switch" : t[0].toUpperCase() + t.slice(1)}
            </button>
          ))}
          <span className="mx-1 h-5 w-px bg-line-strong" />
          <div className="seg" role="group" aria-label="Editing mode">
            <button aria-pressed={mode === "select"} onClick={() => { setMode("select"); setFrom(null); }}>
              Move
            </button>
            <button aria-pressed={mode === "connect"} onClick={() => setMode("connect")} title="Click two devices to cable them">
              Connect
            </button>
          </div>
          <button className="btn btn-sm" onClick={remove} disabled={!sel} title="Delete the selection (Del)">
            Delete
          </button>
          <button className="btn btn-sm" onClick={undo} disabled={!history.length} title="Undo (Ctrl+Z)">
            Undo
          </button>
          <button className="btn btn-sm" onClick={() => setView(fitView(design))}>
            Fit
          </button>
          <span className="hint ml-auto">
            {mode === "connect" ? (from ? `Cable from ${from}: click the other end (Esc cancels)` : "Click the first device to cable") : "Drag devices; drag the floor to pan; scroll to zoom"}
          </span>
        </div>
        <svg
          ref={svg}
          className={`min-h-0 flex-1 touch-none bg-[#151e28] select-none ${mode === "connect" ? "cursor-crosshair" : "cursor-grab"}`}
          viewBox={`${view.x} ${view.y} ${view.w} ${view.h}`}
          preserveAspectRatio="xMidYMid meet"
          onPointerDown={onBgDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerLeave={onUp}
          onWheel={onWheel}
          role="application"
          aria-label="Topology editor canvas"
        >
          <defs>
            <pattern id="dgrid" width="24" height="24" patternUnits="userSpaceOnUse">
              <circle cx="12" cy="12" r="1.1" fill="#233141" />
            </pattern>
          </defs>
          <rect x={view.x - view.w * 4} y={view.y - view.h * 4} width={view.w * 9} height={view.h * 9} fill="#151e28" />
          <rect x={view.x - view.w * 4} y={view.y - view.h * 4} width={view.w * 9} height={view.h * 9} fill="url(#dgrid)" />
          {design.links.map((l, i) => {
            const A = nodeById[l.a];
            const B = nodeById[l.b];
            if (!A || !B) return null;
            const on = sel?.kind === "link" && sel.i === i;
            const w = 2 + Math.min(6, l.bw_mbps / 20);
            const mx = (A.x + B.x) / 2;
            const my = (A.y + B.y) / 2;
            const label = `${l.bw_mbps}M · ${l.delay_ms}ms`;
            return (
              <g
                key={`${l.a}-${l.b}-${i}`}
                onPointerDown={(e) => {
                  e.stopPropagation();
                  setSel({ kind: "link", i });
                }}
                className="cursor-pointer"
              >
                <line x1={A.x} y1={A.y} x2={B.x} y2={B.y} stroke="transparent" strokeWidth={18} />
                <line x1={A.x} y1={A.y} x2={B.x} y2={B.y} stroke="#0a1017" strokeWidth={w + 4} strokeLinecap="round" />
                <line x1={A.x} y1={A.y} x2={B.x} y2={B.y} stroke={on ? "#e6e4df" : isAccess(design, l) ? "#4a5b6e" : "#7e93a8"} strokeWidth={w} strokeLinecap="round" />
                {!isAccess(design, l) && (
                  <g transform={`translate(${mx},${my})`}>
                    <rect x={-label.length * 2.9 - 5} y={-9} width={label.length * 5.8 + 10} height={17} rx={4} fill="#121a23" stroke={on ? "#e6e4df" : "#2f3c4a"} />
                    <text textAnchor="middle" y={3.5} fontSize={10.5} fill="#b4b9bf" fontFamily="Barlow, sans-serif">
                      {label}
                    </text>
                  </g>
                )}
              </g>
            );
          })}
          {design.nodes.map((n) => {
            const sz = DEVICE[n.type].size;
            const on = sel?.kind === "node" && sel.id === n.id;
            const pending = from === n.id;
            return (
              <g key={n.id} transform={`translate(${n.x},${n.y})`} onPointerDown={(e) => onNodeDown(e, n.id)} className={mode === "connect" ? "cursor-crosshair" : "cursor-move"}>
                {(on || pending) && (
                  <ellipse rx={sz[0] / 2 + 12} ry={sz[1] / 2 + 10} cy={2} fill="none" stroke={pending ? "#8fb7d9" : "#e6e4df"} strokeWidth={1.5} strokeDasharray="5 5" />
                )}
                <image href={DEVICE_IMAGE[n.type]} x={-sz[0] / 2} y={-sz[1] / 2} width={sz[0]} height={sz[1]} />
                <text y={sz[1] / 2 + 16} textAnchor="middle" fontSize={13 * Math.max(0.9, Math.min(1.4, scale))} fontWeight={600} fill="#e6e4df" fontFamily="'Barlow Semi Condensed', Barlow, sans-serif" stroke="#151e28" strokeWidth={4} paintOrder="stroke">
                  {n.label}
                </text>
                <text y={sz[1] / 2 + 30} textAnchor="middle" fontSize={11 * Math.max(0.9, Math.min(1.4, scale))} fill="#7f8b97" fontFamily="Barlow, sans-serif">
                  {n.id}
                </text>
              </g>
            );
          })}
        </svg>
        <div className="flex flex-wrap items-center gap-3 border-t border-line px-3 py-2">
          <label className="flex items-center gap-2 text-[13px]">
            <span className="text-ink-3">Name</span>
            <input className="field w-48" value={design.name} maxLength={40} onChange={(e) => setDesign((d) => ({ ...d, name: e.target.value }), false)} />
          </label>
          <input className="field min-w-0 flex-1" placeholder="Description (optional)" value={design.description} onChange={(e) => setDesign((d) => ({ ...d, description: e.target.value }), false)} />
          <button className="btn" onClick={save} disabled={busy || !report?.ok}>
            Save
          </button>
          {!confirm ? (
            <button className="btn btn-primary" onClick={() => setConfirm(true)} disabled={busy || !report?.ok} title={report?.ok ? "Stop the running network and boot this design" : "Fix the errors first"}>
              Deploy…
            </button>
          ) : (
            <span className="flex items-center gap-2 rounded border border-[#6e5a30] bg-[#2d2a1e] px-2 py-1 text-[12.5px]">
              Stops the running network (faults, traffic and plans are cleared) and boots “{design.name}”.
              <button className="btn btn-sm btn-primary" onClick={deploy} disabled={busy}>
                Deploy now
              </button>
              <button className="btn btn-sm" onClick={() => setConfirm(false)}>
                Cancel
              </button>
            </span>
          )}
        </div>
      </section>

      {/* ------------------------------------------------ inspector + checks */}
      <aside className="flex min-w-0 flex-col gap-3">
        <section className="panel p-4">
          <h2 className="panel-title">{selNode ? `${TYPE_LABEL[selNode.type]} ${selNode.id}` : selLink ? `Cable ${selLink.a} – ${selLink.b}` : "Selection"}</h2>
          {!selNode && !selLink && <p className="hint mt-1">Click a device or a cable to edit it. Use + Router / Switch / Client / Server to add equipment and Connect to cable it.</p>}
          {selNode && (
            <div className="mt-2 grid gap-2 text-[13px]">
              <label className="flex flex-col gap-1">
                <span className="text-ink-3">Label</span>
                <input className="field" value={selNode.label} maxLength={40} onChange={(e) => setDesign((d) => ({ ...d, nodes: d.nodes.map((n) => (n.id === selNode.id ? { ...n, label: e.target.value } : n)) }), false)} />
              </label>
              <p className="hint">
                Id <span className="text-ink-2">{selNode.id}</span> names its interfaces ({selNode.id}-eth0, …).
                {selNode.type === "client" || selNode.type === "server" ? " A host needs exactly one cable, to a router or a switch." : ""}
                {selNode.type === "switch" ? " A switch is one LAN: exactly one router (its gateway), any number of hosts." : ""}
              </p>
            </div>
          )}
          {selLink && sel?.kind === "link" && (
            <div className="mt-2 grid grid-cols-2 gap-2 text-[13px]">
              {(
                [
                  ["bw_mbps", "Capacity (Mbit/s)", 0.1, 1000, 1],
                  ["delay_ms", "Delay one way (ms)", 0, 1000, 0.5],
                  ["jitter_ms", "Jitter (ms)", 0, 500, 0.5],
                  ["loss_pct", "Loss (%)", 0, 100, 0.1],
                  ["queue_pkts", "Buffer (packets)", 10, 100000, 100],
                ] as const
              ).map(([k, label, min, max, step]) => (
                <label key={k} className="flex flex-col gap-1">
                  <span className="text-ink-3">{label}</span>
                  <input
                    className="field"
                    type="number"
                    min={min}
                    max={max}
                    step={step}
                    value={(selLink[k] as number | undefined) ?? (k === "queue_pkts" ? 1000 : 0)}
                    onChange={(e) => {
                      const v = +e.target.value;
                      setDesign((d) => ({ ...d, links: d.links.map((l, i) => (i === sel.i ? { ...l, [k]: v } : l)) }), false);
                    }}
                  />
                </label>
              ))}
            </div>
          )}
        </section>

        <section className="panel p-4">
          <div className="flex items-center justify-between">
            <h2 className="panel-title">Default traffic</h2>
            <button
              className="btn btn-sm"
              disabled={!clients.length || !servers.length}
              onClick={() => setDesign((d) => ({ ...d, traffic: [...d.traffic, { src: clients[0].id, dst: servers[0].id, rate_mbps: 6 }] }))}
            >
              + Flow
            </button>
          </div>
          <p className="hint">iperf3 UDP flows that Start traffic launches. Every client → server pair is a managed, probed flow either way.</p>
          <ul className="mt-2 space-y-1.5">
            {design.traffic.map((t, i) => (
              <li key={i} className="flex items-center gap-1.5 text-[13px]">
                <select className="field" value={t.src} onChange={(e) => setDesign((d) => ({ ...d, traffic: d.traffic.map((x, j) => (j === i ? { ...x, src: e.target.value } : x)) }))}>
                  {clients.map((c) => (
                    <option key={c.id}>{c.id}</option>
                  ))}
                </select>
                →
                <select className="field" value={t.dst} onChange={(e) => setDesign((d) => ({ ...d, traffic: d.traffic.map((x, j) => (j === i ? { ...x, dst: e.target.value } : x)) }))}>
                  {servers.map((s) => (
                    <option key={s.id}>{s.id}</option>
                  ))}
                </select>
                <input className="field w-16" type="number" min={0.1} step={0.5} value={t.rate_mbps} onChange={(e) => setDesign((d) => ({ ...d, traffic: d.traffic.map((x, j) => (j === i ? { ...x, rate_mbps: +e.target.value } : x)) }), false)} />
                <span className="hint">Mbit/s</span>
                <button className="btn btn-sm ml-auto" aria-label="Remove flow" onClick={() => setDesign((d) => ({ ...d, traffic: d.traffic.filter((_, j) => j !== i) }))}>
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </section>

        <DesignChecks report={report} />
      </aside>
    </div>
  );
}

function DesignChecks({ report }: { report: Report | null }) {
  if (!report) return null;
  return (
    <section className="panel p-4">
      <div className="flex items-baseline justify-between">
        <h2 className="panel-title">Design checks</h2>
        <span className={`font-cond text-[13px] font-semibold ${report.ok ? (report.warnings.length ? "text-[#ffd27a]" : "text-[#7fd67f]") : "text-[#ff9d94]"}`}>
          {report.ok ? (report.warnings.length ? `${report.warnings.length} warning${report.warnings.length > 1 ? "s" : ""}` : "ready to deploy") : "cannot boot"}
        </span>
      </div>
      {report.errors.map((e) => (
        <p key={e} className="mt-1.5 rounded border border-[#7a3434] bg-[#3a2326] px-2 py-1 text-[12.5px]">
          {e}
        </p>
      ))}
      {report.warnings.map((w) => (
        <p key={w} className="mt-1.5 text-[12.5px] text-[#ffd27a]">
          ⚠ {w}
        </p>
      ))}
      {report.stats && (
        <p className="mt-2 text-[12.5px] text-ink-2">
          {report.stats.routers} routers, {report.stats.switches} switches, {report.stats.clients} clients, {report.stats.servers} servers · {report.stats.flows} managed flows ·{" "}
          {report.stats.probe_streams} probe streams
        </p>
      )}
      {report.flows && report.flows.length > 0 && (
        <table className="data mt-2">
          <thead>
            <tr>
              <th>Flow</th>
              <th className="text-right" title="candidate paths (Yen, K = 8)">Paths</th>
              <th className="text-right" title="link-disjoint paths between the sites' gateway routers">Disjoint</th>
              <th className="text-right" title="round trip on the shortest path, by design">RTT</th>
            </tr>
          </thead>
          <tbody>
            {report.flows.map((f) => (
              <tr key={f.pair}>
                <td>{f.pair.replace(">", " → ")}</td>
                <td className="text-right">{f.candidates}</td>
                <td className={`text-right ${f.disjoint_paths === 1 ? "text-[#ffd27a]" : ""}`}>{f.disjoint_paths ?? "same router"}</td>
                <td className="text-right">{f.design_rtt_ms != null ? `${f.design_rtt_ms.toFixed(1)} ms` : "–"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {report.spofs && report.spofs.length > 0 && (
        <p className="mt-2 text-[12.5px] text-ink-3">
          Single points of failure: {report.spofs.map((s) => s.scenario.replace("node:", "router ").replace("link:", "link ")).join(", ")}
          {report.spofs.every((s) => s.edge) && " (site gateways: a host has one uplink by design)"}
        </p>
      )}
    </section>
  );
}
