// Network-equipment artwork for the topology map, in the vernacular engineers know from
// network diagrams: the router "puck" with its four-arrow emblem, a box switch with ports, a tower
// server and a laptop. The art is static; every light on it (status and activity LEDs) is drawn
// separately from live measurements, so nothing here can pretend to be state.

import type { NodeType } from "../lib/types";

export interface DeviceSpec {
  /** artwork viewBox */
  vb: [number, number];
  /** node size on the map, model px */
  size: [number, number];
  /** LED positions in viewBox units */
  status: [number, number];
  activity: [number, number];
}

export const DEVICE: Record<NodeType, DeviceSpec> = {
  router: { vb: [120, 84], size: [92, 64], status: [51, 58], activity: [65, 58] },
  switch: { vb: [130, 80], size: [98, 60], status: [92, 45], activity: [102, 45] },
  server: { vb: [72, 100], size: [55, 76], status: [38.5, 66], activity: [47.5, 66] },
  client: { vb: [110, 84], size: [82, 63], status: [50, 70.3], activity: [60, 70.3] },
};

/** Rendered offset of a viewBox point from the node centre, in model px. */
export function ledOffset(type: NodeType, which: "status" | "activity"): [number, number] {
  const d = DEVICE[type];
  const s = Math.min(d.size[0] / d.vb[0], d.size[1] / d.vb[1]);
  const [x, y] = d[which];
  return [(x - d.vb[0] / 2) * s, (y - d.vb[1] / 2) * s];
}

const uri = (svg: string) => "data:image/svg+xml;utf8," + encodeURIComponent(svg);

// arrow on a plane seen from above at an angle: compute in plane coords, then squash y
function planeArrow(cx: number, cy: number, squash: number, x1: number, y1: number, x2: number, y2: number, w: number): string {
  const L = Math.hypot(x2 - x1, y2 - y1);
  const ux = (x2 - x1) / L;
  const uy = (y2 - y1) / L;
  const bx = x2 - ux * 11;
  const by = y2 - uy * 11;
  const px = -uy * 7.5;
  const py = ux * 7.5;
  const P = (x: number, y: number) => `${(cx + x).toFixed(2)},${(cy + y * squash).toFixed(2)}`;
  return (
    `<polyline points="${P(x1, y1)} ${P(bx, by)}" stroke-width="${w}"/>` +
    `<polygon points="${P(x2, y2)} ${P(bx + px, by + py)} ${P(bx - px, by - py)}"/>`
  );
}

const SHADOW = (cx: number, cy: number, rx: number, ry: number) =>
  `<radialGradient id="sh"><stop offset="0" stop-color="#000" stop-opacity=".6"/><stop offset=".7" stop-color="#000" stop-opacity=".18"/><stop offset="1" stop-color="#000" stop-opacity="0"/></radialGradient>` +
  `</defs><ellipse cx="${cx}" cy="${cy}" rx="${rx}" ry="${ry}" fill="url(#sh)"/>`;

function router(): string {
  const sq = 0.34;
  const arrows = [
    planeArrow(60, 30, sq, -9, -9, -33, -33, 3.6), // out, north-west
    planeArrow(60, 30, sq, 9, 9, 33, 33, 3.6), // out, south-east
    planeArrow(60, 30, sq, 34, -34, 10, -10, 3.6), // in, from north-east
    planeArrow(60, 30, sq, -34, 34, -10, 10, 3.6), // in, from south-west
  ].join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="120" height="84" viewBox="0 0 120 84"><defs>
<linearGradient id="side" x1="0" x2="1"><stop offset="0" stop-color="#132131"/><stop offset=".38" stop-color="#2d4766"/><stop offset=".55" stop-color="#36557a"/><stop offset="1" stop-color="#111d2b"/></linearGradient>
<linearGradient id="top" x1="0" y1="0" x2=".25" y2="1"><stop offset="0" stop-color="#7097c0"/><stop offset=".55" stop-color="#4a6d93"/><stop offset="1" stop-color="#3a5878"/></linearGradient>
<linearGradient id="rim" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#b9d3ec" stop-opacity=".9"/><stop offset="1" stop-color="#b9d3ec" stop-opacity=".1"/></linearGradient>
${SHADOW(60, 72, 56, 11)}
<path d="M12 30 V50 A48 16 0 0 0 108 50 V30 Z" fill="url(#side)"/>
<path d="M12 50 A48 16 0 0 0 108 50" fill="none" stroke="#070d14" stroke-width="1.6" stroke-opacity=".9"/>
<path d="M44 54.5 h28 a3 3 0 0 1 0 7 h-28 a3 3 0 0 1 0 -7 z" fill="#0b131c" stroke="#4d6a8c" stroke-opacity=".5" stroke-width=".8"/>
<ellipse cx="60" cy="30" rx="48" ry="16" fill="url(#top)"/>
<ellipse cx="60" cy="30" rx="47.4" ry="15.5" fill="none" stroke="url(#rim)" stroke-width="1.3"/>
<g fill="#f1f6fb" stroke="#f1f6fb" stroke-linecap="round" stroke-linejoin="round">${arrows}</g>
</svg>`;
}

function sw(): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="130" height="80" viewBox="0 0 130 80"><defs>
<linearGradient id="top" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#7097c0"/><stop offset="1" stop-color="#456890"/></linearGradient>
<linearGradient id="front" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#2d4766"/><stop offset="1" stop-color="#172639"/></linearGradient>
${SHADOW(64, 68, 62, 9)}
<polygon points="8,32 110,32 122,18 20,18" fill="url(#top)"/>
<polygon points="8,32 110,32 110,58 8,58" fill="url(#front)"/>
<polygon points="110,32 122,18 122,44 110,58" fill="#111d2b"/>
<polyline points="8,32.4 110,32.4 122,18.4" fill="none" stroke="#b9d3ec" stroke-opacity=".55" stroke-width="1.1"/>
<g fill="#f1f6fb" stroke="#f1f6fb" stroke-linecap="round" stroke-linejoin="round">
<polyline points="38,21.6 80,21.6" stroke-width="2.6"/><polygon points="88,21.6 79,18.4 79,24.8"/>
<polyline points="90,28.4 48,28.4" stroke-width="2.6"/><polygon points="40,28.4 49,25.2 49,31.6"/>
</g>
<g fill="#09111a" stroke="#4d6a8c" stroke-opacity=".55" stroke-width=".7">
${Array.from({ length: 6 }, (_, i) => `<rect x="${15 + i * 11}" y="40" width="8" height="7" rx="1"/>`).join("")}
</g>
<path d="M88 41 h18 a2.5 2.5 0 0 1 0 8 h-18 a2.5 2.5 0 0 1 0 -8 z" fill="#0b131c" stroke="#4d6a8c" stroke-opacity=".45" stroke-width=".7"/>
</svg>`;
}

function server(): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="72" height="100" viewBox="0 0 72 100"><defs>
<linearGradient id="front" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#34527a"/><stop offset="1" stop-color="#1a2b40"/></linearGradient>
<linearGradient id="top" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#6a91ba"/><stop offset="1" stop-color="#4b6f96"/></linearGradient>
${SHADOW(37, 92, 32, 6)}
<polygon points="10,16 54,16 64,8 20,8" fill="url(#top)"/>
<rect x="10" y="16" width="44" height="76" rx="2" fill="url(#front)"/>
<polygon points="54,16 64,8 64,84 54,92" fill="#111d2b"/>
<polyline points="10,16.4 54,16.4 64,8.4" fill="none" stroke="#b9d3ec" stroke-opacity=".55" stroke-width="1"/>
<g fill="#0a121b" stroke="#55739a" stroke-opacity=".55" stroke-width=".7">
<rect x="15" y="23" width="34" height="9" rx="1.2"/><rect x="15" y="36" width="34" height="9" rx="1.2"/><rect x="15" y="49" width="34" height="9" rx="1.2"/>
</g>
<g stroke="#8fb0d0" stroke-opacity=".55" stroke-width="1.2" stroke-linecap="round">
<line x1="19" y1="27.5" x2="30" y2="27.5"/><line x1="19" y1="40.5" x2="30" y2="40.5"/><line x1="19" y1="53.5" x2="30" y2="53.5"/>
</g>
<path d="M36 62.5 h14 a3.5 3.5 0 0 1 0 7 h-14 a3.5 3.5 0 0 1 0 -7 z" fill="#0b131c" stroke="#4d6a8c" stroke-opacity=".45" stroke-width=".7"/>
<g stroke="#0d1621" stroke-width="1.6" stroke-linecap="round">
${Array.from({ length: 5 }, (_, i) => `<line x1="16" y1="${75 + i * 3.4}" x2="48" y2="${75 + i * 3.4}"/>`).join("")}
</g>
</svg>`;
}

function client(): string {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="110" height="84" viewBox="0 0 110 84"><defs>
<linearGradient id="screen" x1="0" y1="0" x2=".6" y2="1"><stop offset="0" stop-color="#2a6aa3"/><stop offset=".55" stop-color="#173c61"/><stop offset="1" stop-color="#10273f"/></linearGradient>
<linearGradient id="deck" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6a91ba"/><stop offset="1" stop-color="#456890"/></linearGradient>
${SHADOW(55, 76, 54, 7)}
<polygon points="24,6 86,6 90,52 20,52" fill="#1b2b3e" stroke="#7a9cc0" stroke-opacity=".5" stroke-width="1"/>
<polygon points="28.5,10 81.5,10 85,48 25,48" fill="url(#screen)"/>
<polygon points="28.5,10 52,10 34,48 25,48" fill="#ffffff" fill-opacity=".05"/>
<g stroke="#9cc4ea" stroke-opacity=".35" stroke-width="1.4" stroke-linecap="round">
<line x1="34" y1="20" x2="58" y2="20"/><line x1="33.5" y1="26" x2="70" y2="26"/><line x1="33" y1="32" x2="52" y2="32"/>
</g>
<polygon points="14,54 96,54 106,68 4,68" fill="url(#deck)"/>
<polyline points="14,54.3 96,54.3" stroke="#c4dbf0" stroke-opacity=".6" stroke-width="1"/>
<polygon points="45,60 65,60 67,64.5 43,64.5" fill="#3a5878"/>
<rect x="4" y="68" width="102" height="4.6" rx="1.5" fill="#1e3046"/>
</svg>`;
}

export const DEVICE_IMAGE: Record<NodeType, string> = {
  router: uri(router()),
  switch: uri(sw()),
  server: uri(server()),
  client: uri(client()),
};
