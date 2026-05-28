"use client";

import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export function QueriesChart({
  data,
}: {
  data: { day: string; count: number }[];
}) {
  if (!data.length) {
    return (
      <div className="h-64 flex items-center justify-center text-sm text-zinc-500">
        No queries logged in the selected window yet.
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={240}>
      <AreaChart data={data} margin={{ left: -10, right: 6, top: 8, bottom: 0 }}>
        <defs>
          <linearGradient id="queriesFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#FF4D94" stopOpacity={0.35} />
            <stop offset="100%" stopColor="#FF4D94" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#0E11161A" strokeDasharray="4 4" vertical={false} />
        <XAxis
          dataKey="day"
          stroke="#0E11168C"
          fontSize={11}
          tickLine={false}
          axisLine={false}
          tickFormatter={(d: string) => d.slice(5)}
        />
        <YAxis
          stroke="#0E11168C"
          fontSize={11}
          tickLine={false}
          axisLine={false}
          allowDecimals={false}
        />
        <Tooltip
          contentStyle={{
            background: "#FFFCF5",
            border: "2px solid #0E1116",
            borderRadius: 12,
            fontSize: 12,
            fontWeight: 500,
          }}
        />
        <Area
          type="monotone"
          dataKey="count"
          stroke="#E33A7E"
          fill="url(#queriesFill)"
          strokeWidth={2.5}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
