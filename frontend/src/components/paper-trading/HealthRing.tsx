import { useEffect, useRef } from "react";
import { getChartTheme } from "@/lib/chart-theme";
import { echarts, CHART_GROUP, connectCharts } from "@/lib/echarts";
import { useDarkMode } from "@/hooks/useDarkMode";

interface Props {
  /** Margin usage as a 0..1 fraction of wallet balance -- how much of the
   * account is committed vs. still free. Health is the inverse: low usage
   * reads as healthy, usage approaching 1 reads as at-risk. */
  marginUsagePct: number;
  height?: number;
}

function riskLabel(usage: number): { label: string; color: (t: ReturnType<typeof getChartTheme>) => string } {
  if (usage >= 0.8) return { label: "High", color: (t) => t.downColor };
  if (usage >= 0.5) return { label: "Elevated", color: (t) => t.warningColor };
  return { label: "Low", color: (t) => t.upColor };
}

export function HealthRing({ marginUsagePct, height = 220 }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const { dark } = useDarkMode();
  const usage = Math.max(0, Math.min(1, marginUsagePct));
  const healthPct = Math.round((1 - usage) * 100);
  const risk = riskLabel(usage);

  useEffect(() => {
    if (!ref.current) return;
    const t = getChartTheme();
    const ringColor = risk.color(t);
    const chart = echarts.init(ref.current);
    chart.group = CHART_GROUP;
    connectCharts();

    chart.setOption({
      backgroundColor: "transparent",
      series: [
        {
          type: "pie",
          radius: ["70%", "88%"],
          startAngle: 90,
          silent: true,
          label: { show: false },
          labelLine: { show: false },
          data: [
            { value: healthPct, itemStyle: { color: ringColor } },
            { value: 100 - healthPct, itemStyle: { color: t.gridColor } },
          ],
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
  }, [healthPct, dark]);

  return (
    <div className="relative" style={{ height }}>
      <div ref={ref} className="h-full w-full" />
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <p className="text-2xl font-bold font-mono tabular-nums">{healthPct}%</p>
        <p className="text-xs text-muted-foreground">{risk.label} risk</p>
      </div>
    </div>
  );
}
