import { useMemo } from "react";
import type { Snapshot } from "../lib/types";
import { fmtSignal } from "./AIInsights";
import type { LinkVis, MapCause, NodeVis, StrandVis } from "./TopologyView";

/** Map a live snapshot onto what the topology view draws. Every number here is measured. */
export function useLiveVis(snap: Snapshot | null) {
  return useMemo(() => {
    const links: Record<string, LinkVis> = {};
    const nodes: Record<string, NodeVis> = {};
    const strands: StrandVis[] = [];
    let cause: MapCause | null = null;
    if (!snap) return { links, nodes, strands, cause };
    for (const [id, l] of Object.entries(snap.links)) {
      links[id] = {
        util: l.util,
        health: l.health,
        rtt: l.rtt_ms,
        expected: l.expected_rtt_ms,
        admin_up: l.admin_up,
        ab_pps: l.ab.pps,
        ba_pps: l.ba.pps,
      };
    }
    for (const [id, n] of Object.entries(snap.nodes)) {
      // packets/s through the node = what its interfaces send (each packet counted once per hop)
      const pps = n.interfaces.reduce((s, i) => s + i.tx_pps, 0);
      nodes[id] = { health: n.health, pps };
    }
    for (const f of Object.values(snap.flows)) strands.push({ pair: f.pair, path: f.path, offered_mbps: f.offered_mbps });
    // AI layer: anomalies sit on the cable they measure (an access-segment signal on the host's cable)
    for (const a of snap.ai?.anomalies ?? []) {
      let lid: string | undefined;
      if (a.kind === "link") lid = a.entity;
      else if (a.kind === "access") lid = Object.values(snap.links).find((l) => l.a === a.entity || l.b === a.entity)?.id;
      if (!lid || !links[lid]) continue;
      const what = { rtt: "round trip", loss: "probe loss", util: "load", data_loss: "data loss" }[a.metric];
      (links[lid].anomalies ??= []).push(`${what} ${fmtSignal(a.value, a.unit)}, normally ${fmtSignal(a.normal_mean, a.unit)}`);
    }
    const top = snap.ai?.diagnosis.causes[0];
    if (top) cause = { kind: top.element_kind === "link" ? "link" : "node", id: top.element, title: top.title, confidence: top.confidence };
    return { links, nodes, strands, cause };
  }, [snap]);
}
