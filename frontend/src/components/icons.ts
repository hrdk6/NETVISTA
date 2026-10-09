// Node glyphs as SVG data URIs for Cytoscape backgrounds (drawn in ink on the node body).
const svg = (body: string) =>
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#e6e4df" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`,
  );

export const GLYPH = {
  // router: the classic four-arrow roundel
  router: svg(
    '<path d="M12 3v6M9.5 5.5 12 3l2.5 2.5"/><path d="M12 21v-6M9.5 18.5 12 21l2.5-2.5"/><path d="M3 12h6M5.5 9.5 3 12l2.5 2.5"/><path d="M21 12h-6M18.5 9.5 21 12l-2.5 2.5"/>',
  ),
  // switch: two opposing arrow pairs
  switch: svg('<path d="M4 9h15M16 6l3 3-3 3"/><path d="M20 15H5M8 12l-3 3 3 3"/>'),
  // client: laptop
  client: svg('<rect x="5" y="5" width="14" height="10" rx="1.2"/><path d="M3 19h18"/>'),
  // server: stacked units
  server: svg('<rect x="5" y="3.5" width="14" height="7" rx="1"/><rect x="5" y="13.5" width="14" height="7" rx="1"/><path d="M8 7h.01M8 17h.01"/>'),
};
