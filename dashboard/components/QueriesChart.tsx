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
            <stop offset="0%" stopColor="#0ea5e9" stopOpacity={0.35} />
            <stop offset="100%" stopColor="#0ea5e9" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="#e4e4e7" strokeDasharray="4 4" vertical={false} />
        <XAxis
          dataKey="day"
          stroke="#71717a"
          fontSize={11}
          tickLine={false}
          axisLine={false}
          tickFormatter={(d: string) => d.slice(5)}
        />
        <YAxis
          stroke="#71717a"
          fontSize={11}
          tickLine={false}
          axisLine={false}
          allowDecimals={false}
        />
        <Tooltip
          contentStyle={{
            background: "white",
            border: "1px solid #e4e4e7",
            borderRadius: 8,
            fontSize: 12,
          }}
        />
        <Area
          type="monotone"
          dataKey="count"
          stroke="#0284c7"
          fill="url(#queriesFill)"
          strokeWidth={2}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
