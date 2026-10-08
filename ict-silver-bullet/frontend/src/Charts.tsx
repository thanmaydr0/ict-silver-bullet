import createPlotlyComponent from "react-plotly.js/factory";
import Plotly from "plotly.js-finance-dist-min";
import type { Data, Layout } from "plotly.js";
import type { DashboardSnapshot } from "./types";
const Plot = createPlotlyComponent(Plotly);
const base: Partial<Layout> = {
  paper_bgcolor: "transparent",
  plot_bgcolor: "transparent",
  font: { color: "#9ba8b4", family: "Segoe UI, sans-serif", size: 11 },
  margin: { l: 62, r: 16, t: 18, b: 40 },
  autosize: true,
  showlegend: true,
  legend: { orientation: "h", y: 1.15, x: 0 },
  xaxis: { gridcolor: "#232c34", zeroline: false },
  yaxis: { gridcolor: "#232c34", zeroline: false },
  hovermode: "x unified",
};
export function EquityChart({
  equity,
}: {
  equity: NonNullable<DashboardSnapshot["equity"]["data"]>;
}) {
  const points = equity.points;
  const traces: Data[] = [
    {
      x: points.map((p) => p.ts),
      y: points.map((p) => p.equity),
      name: "Equity",
      type: "scatter",
      mode: "lines",
      line: { color: "#70dac5", width: 2 },
    },
    {
      x: points.map((p) => p.ts),
      y: points.map((p) => p.daily_limit),
      name: "Daily 4% reference",
      type: "scatter",
      mode: "lines",
      line: { color: "#caa25c", dash: "dot", shape: "hv" },
    },
    {
      x: points.map((p) => p.ts),
      y: points.map((p) => p.overall_limit),
      name: "Overall 6% reference",
      type: "scatter",
      mode: "lines",
      line: { color: "#c76c77", dash: "dot", shape: "hv" },
    },
  ];
  return (
    <Plot
      data={traces}
      layout={{
        ...base,
        uirevision: "equity",
        yaxis: { ...base.yaxis, rangemode: "normal" },
        height: 310,
      }}
      config={{
        displaylogo: false,
        responsive: true,
        scrollZoom: true,
        modeBarButtonsToRemove: ["lasso2d", "select2d", "sendChartToCloud"],
      }}
      useResizeHandler
      style={{ width: "100%" }}
    />
  );
}
export function SetupChart({
  setup,
}: {
  setup: NonNullable<DashboardSnapshot["setup"]["data"]>;
}) {
  const candles = setup.candles;
  return (
    <Plot
      data={[
        {
          type: "candlestick",
          x: candles.map((c) => c.ts),
          open: candles.map((c) => c.open),
          high: candles.map((c) => c.high),
          low: candles.map((c) => c.low),
          close: candles.map((c) => c.close),
          increasing: { line: { color: "#70dac5" } },
          decreasing: { line: { color: "#d4848f" } },
          name: setup.pair,
        },
      ]}
      layout={{
        ...base,
        showlegend: false,
        height: 345,
        uirevision: `setup-${setup.signal_id}`,
        xaxis: { ...base.xaxis, rangeslider: { visible: false } },
        yaxis: { ...base.yaxis, tickformat: ".5f" },
        shapes: setup.overlays.map((o, i) => ({
          type: o.kind === "band" ? "rect" : "line",
          xref: "paper",
          x0: 0,
          x1: 1,
          y0: o.bottom,
          y1: o.top,
          line: {
            color: ["#728fc6", "#caa25c", "#a193cd", "#d4848f", "#70dac5"][
              i % 5
            ],
            width: 1,
            dash: o.kind === "band" ? "solid" : "dot",
          },
          fillcolor: "rgba(114,143,198,.10)",
          layer: "below",
        })),
        annotations: setup.overlays.map((o) => ({
          xref: "paper",
          x: 1,
          y: o.top,
          text: o.label,
          showarrow: false,
          xanchor: "right",
          yanchor: "bottom",
          font: { size: 10, color: "#c4cdd5" },
        })),
      }}
      config={{
        displaylogo: false,
        responsive: true,
        scrollZoom: true,
        modeBarButtonsToRemove: ["lasso2d", "select2d", "sendChartToCloud"],
      }}
      useResizeHandler
      style={{ width: "100%" }}
    />
  );
}
