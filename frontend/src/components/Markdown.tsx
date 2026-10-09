import type { ReactNode } from "react";

// A deliberately small Markdown subset for copilot answers: headings, paragraphs, bullet and
// numbered lists, **bold**, `code`. It builds React elements only (no HTML injection), and it
// marks every value the grounding check could not find in the data the copilot read.

function escapeRe(s: string) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function inline(text: string, flagged: string[], key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  const plain = (s: string) => {
    if (!flagged.length) return [s];
    const fre = new RegExp(`(${flagged.map(escapeRe).join("|")})`, "g");
    return s.split(fre).map((piece, j) =>
      flagged.includes(piece) ? (
        <span
          key={`${key}-f${i}-${j}`}
          className="underline decoration-ink-3 decoration-dotted underline-offset-4"
          title="Not found in the data the copilot read: it may be derived or wrong"
        >
          {piece}
        </span>
      ) : (
        piece
      ),
    );
  };
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(...plain(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(<strong key={`${key}-b${i}`} className="font-semibold text-ink">{plain(tok.slice(2, -2))}</strong>);
    else out.push(<code key={`${key}-c${i}`} className="rounded bg-[#151e28] px-1 py-px text-[12px] text-ink">{tok.slice(1, -1)}</code>);
    last = m.index + tok.length;
    i++;
  }
  if (last < text.length) out.push(...plain(text.slice(last)));
  return out;
}

export default function Markdown({ text, flagged = [] }: { text: string; flagged?: string[] }) {
  const lines = text.replace(/\r/g, "").split("\n");
  const blocks: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) {
      const k = `p${blocks.length}`;
      blocks.push(<p key={k}>{inline(para.join(" "), flagged, k)}</p>);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      const k = `l${blocks.length}`;
      const items = list.items.map((it, j) => <li key={j}>{inline(it, flagged, `${k}-${j}`)}</li>);
      blocks.push(
        list.ordered ? (
          <ol key={k} className="list-decimal space-y-1 pl-5 marker:text-ink-3">{items}</ol>
        ) : (
          <ul key={k} className="list-disc space-y-1 pl-5 marker:text-ink-3">{items}</ul>
        ),
      );
      list = null;
    }
  };
  for (const raw of lines) {
    const line = raw.trimEnd();
    const h = line.match(/^#{1,4}\s+(.*)$/);
    const ul = line.match(/^\s*[-*•]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (h) {
      flushPara();
      flushList();
      const k = `h${blocks.length}`;
      blocks.push(<h4 key={k} className="font-cond text-[14px] font-semibold text-ink">{inline(h[1].replace(/\*\*/g, ""), flagged, k)}</h4>);
    } else if (ul || ol) {
      flushPara();
      const ordered = !!ol;
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, items: [] };
      list.items.push((ul ?? ol)![1]);
    } else if (!line.trim()) {
      flushPara();
      flushList();
    } else if (list && /^\s{2,}/.test(raw)) {
      list.items[list.items.length - 1] += " " + line.trim();
    } else {
      flushList();
      para.push(line.trim());
    }
  }
  flushPara();
  flushList();
  return <div className="space-y-2 text-[13.5px] leading-relaxed text-ink-2">{blocks}</div>;
}
