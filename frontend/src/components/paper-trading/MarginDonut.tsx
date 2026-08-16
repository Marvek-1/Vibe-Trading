import { useEffect, useRef } from "react";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts, CHART_GROUP, connectCharts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";

interface Slice {
  symbol: string;
  margin: number;
}

interface Props {
  slices: Slice[];
  totalMargin: number;
  height?: number;
}

/** Same rotating hue set the rest of the page uses for per-item identity
 * (CARD_THEMES / maColors) -- keeps a session's donut and its card stripe
 * reading as one palette instead of inventing new colors per chart. */
function sliceColors(t: ReturnType<typeof getChartTheme>): string[] {
  return [t.infoColor, "#8b5cf6", t.warningColor, t.upColor, t.downColor, "#06b6d4"];
}

export function MarginDonut({ slices, totalMargin, height = 220 }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const { dark } = useDarkMode();

  useEffect(() => {
    if (!ref.current || slices.length === 0) return;
    const t = getChartTheme();
    const colors = sliceColors(t);
    const chart = echarts.init(ref.current);
    chart.group = CHART_GROUP;
    connectCharts();

    chart.setOption({
      backgroundColor: "transparent",
      color: colors,
      tooltip: {
        trigger: "item",
        backgroundColor: t.tooltipBg,
        borderColor: t.tooltipBorder,
        textStyle: { color: t.tooltipText, fontSize: 11 },
        formatter: (p: { name: string; value: number; percent: number }) =>
          `${p.name}<br/>${p.value.toLocaleString(undefined, { style: "currency", currency: "USD" })} (${p.percent}%)`,
      },
      series: [
        {
          type: "pie",
          radius: ["58%", "82%"],
          avoidLabelOverlap: true,
          label: { show: false },
          labelLine: { show: false },
          emphasis: { scale: true, scaleSize: 4 },
          data: slices.map((s) => ({ name: s.symbol, value: s.margin })),
        },
      ],
    });

    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slices, dark]);

  if (slices.length === 0) return null;

  return (
    <div className="relative" style={{ height }}>
      <div ref={ref} className="h-full w-full" />
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <p className="text-sm font-bold font-mono tabular-nums">
          {totalMargin.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 })}
        </p>
        <p className="text-[10px] text-muted-foreground uppercase tracking-wide">Total Margin</p>
      </div>
    </div>
  );
}
