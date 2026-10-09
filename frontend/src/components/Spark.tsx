import { Line, LineChart, ResponsiveContainer, Tooltip, YAxis } from "recharts";

interface Props {
  data: { t: number; v: number | null }[];
  color?: string;
  format: (v: number) => string;
  label: string;
  domainMax?: number;
}

/** Tiny single-series trend (the label names it, so no legend). */
export default function Spark({ data, color = "#e6e4df", format, label, domainMax }: Props) {
  const last = [...data].reverse().find((d) => d.v != null)?.v ?? null;
  return (
    <div>
      <div className="flex items-baseline justify-between text-[12.5px]">
        <span className="text-ink-3">{label}</span>
        <span className="num text-ink">{last == null ? "–" : format(last)}</span>
      </div>
      <div className="h-[46px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 4, right: 2, bottom: 2, left: 2 }}>
            <YAxis hide domain={[0, domainMax ?? "auto"]} />
            <Tooltip
              cursor={{ stroke: "#3d4c5d" }}
              contentStyle={{ background: "#18212b", border: "1px solid #3d4c5d", borderRadius: 4, fontSize: 12 }}
              labelFormatter={(_, p) => (p?.[0] ? new Date(p[0].payload.t * 1000).toLocaleTimeString([], { hour12: false }) : "")}
              formatter={(v) => [typeof v === "number" ? format(v) : "–", label]}
            />
            <Line type="monotone" dataKey="v" stroke={color} strokeWidth={2} dot={false} isAnimationActive={false} connectNulls={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
