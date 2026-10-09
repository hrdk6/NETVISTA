import { useMemo } from "react";
import type { Snapshot } from "../lib/types";
import type { LinkVis, StrandVis } from "./TopologyView";

/** Map a live snapshot onto what the topology view draws. Every number here is measured. */
export function useLiveVis(snap: Snapshot | null) {
  return useMemo(() => {
    const links: Record<string, LinkVis> = {};
    const nodes: Record<string, { health: Snapshot["nodes"][string]["health"] }> = {};
    const strands: StrandVis[] = [];
    if (!snap) return { links, nodes, strands };
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
    for (const [id, n] of Object.entries(snap.nodes)) nodes[id] = { health: n.health };
    for (const f of Object.values(snap.flows)) strands.push({ pair: f.pair, path: f.path, offered_mbps: f.offered_mbps });
    return { links, nodes, strands };
  }, [snap]);
}
