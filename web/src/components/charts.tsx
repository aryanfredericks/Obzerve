/**
 * Small SVG chart kit for the metrics page: time-series lines, stacked columns,
 * and horizontal bars. Marks follow one spec (2px lines, <=24px columns with 4px
 * rounded data-ends and 2px surface gaps, hairline grid), every chart has a
 * crosshair/hover tooltip with keyboard support, a legend for 2+ series, and a
 * table view so no value is reachable only by hovering.
 */
import { type KeyboardEvent, type PointerEvent, type ReactNode, useEffect, useRef, useState } from "react";

export interface Series {
  key: string;
  label: string;
  /** CSS color, normally a var(--series-n) / var(--status-*) token */
  color: string;
  values: (number | null)[];
}

const MARGIN = { top: 14, right: 16, bottom: 26, left: 56 };
const END_LABEL_SPACE = 64;

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** Round the axis maximum up to a clean step: 1, 2, 2.5, or 5 × 10^n. */
function niceScale(max: number, ticks = 4): { max: number; ticks: number[] } {
  if (!(max > 0)) return { max: 1, ticks: [0, 1] };
  const rough = max / ticks;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough)!;
  const top = Math.ceil(max / step) * step;
  const values = [];
  for (let v = 0; v <= top + step / 2; v += step) values.push(Number(v.toPrecision(12)));
  return { max: top, ticks: values };
}

export function formatBucketTime(iso: string, bucketSeconds: number, withDate: boolean): string {
  const d = new Date(iso);
  if (bucketSeconds >= 86_400) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return withDate ? `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}` : time;
}

interface TimeAxisProps {
  times: string[];
  bucketSeconds: number;
  x: (i: number) => number;
  y: number;
  plotWidth: number;
}

function TimeAxis({ times, bucketSeconds, x, y, plotWidth }: TimeAxisProps) {
  const count = Math.max(2, Math.min(6, Math.floor(plotWidth / 110)));
  const spansDays = times.length > 1 && Date.parse(times[times.length - 1]) - Date.parse(times[0]) > 86_400_000;
  const indices = Array.from({ length: count }, (_, k) => Math.round((k * (times.length - 1)) / (count - 1)));
  return (
    <g className="chart-axis-labels">
      {[...new Set(indices)].map((i, k, all) => (
        <text
          key={i}
          x={x(i)}
          y={y + 18}
          textAnchor={k === 0 ? "start" : k === all.length - 1 ? "end" : "middle"}
        >
          {formatBucketTime(times[i], bucketSeconds, spansDays)}
        </text>
      ))}
    </g>
  );
}

function YGrid({ ticks, y, left, right, format }: { ticks: number[]; y: (v: number) => number; left: number; right: number; format: (v: number) => string }) {
  return (
    <g>
      {ticks.map((t) => (
        <g key={t}>
          <line className={t === 0 ? "chart-baseline" : "chart-grid"} x1={left} x2={right} y1={y(t)} y2={y(t)} />
          <text className="chart-axis-labels" x={left - 8} y={y(t)} dy="0.32em" textAnchor="end">
            {format(t)}
          </text>
        </g>
      ))}
    </g>
  );
}

export function Legend({ series, mark }: { series: Pick<Series, "key" | "label" | "color">[]; mark: "line" | "rect" }) {
  return (
    <ul className="chart-legend">
      {series.map((s) => (
        <li key={s.key}>
          <span className={`legend-key legend-${mark}`} style={{ background: s.color }} />
          {s.label}
        </li>
      ))}
    </ul>
  );
}

interface TooltipRow {
  key: string;
  label: string;
  color: string;
  value: string;
}

function Tooltip({ x, width, title, rows }: { x: number; width: number; title: string; rows: TooltipRow[] }) {
  const flip = x > width - 180;
  return (
    <div className="chart-tooltip" style={flip ? { right: width - x + 12 } : { left: x + 12 }} role="status">
      <div className="chart-tooltip-title">{title}</div>
      {rows.map((r) => (
        <div key={r.key} className="chart-tooltip-row">
          <span className="tooltip-key" style={{ background: r.color }} />
          <strong>{r.value}</strong>
          <span>{r.label}</span>
        </div>
      ))}
    </div>
  );
}

/** Shared hover/keyboard index for charts with one position per time bucket. */
function useHoverIndex(count: number, xToIndex: (x: number) => number) {
  const [index, setIndex] = useState<number | null>(null);
  return {
    index,
    onPointerMove: (e: PointerEvent<SVGElement>) => {
      const rect = e.currentTarget.getBoundingClientRect();
      setIndex(Math.max(0, Math.min(count - 1, xToIndex(e.clientX - rect.left))));
    },
    onPointerLeave: () => setIndex(null),
    onKeyDown: (e: KeyboardEvent<SVGElement>) => {
      if (e.key === "ArrowRight") setIndex((i) => Math.min(count - 1, (i ?? -1) + 1));
      else if (e.key === "ArrowLeft") setIndex((i) => Math.max(0, (i ?? count) - 1));
      else if (e.key === "Escape") setIndex(null);
      else return;
      e.preventDefault();
    },
    onBlur: () => setIndex(null),
  };
}

interface TimeChartProps {
  title: string;
  times: string[];
  bucketSeconds: number;
  series: Series[];
  format: (v: number) => string;
  height?: number;
  empty?: ReactNode;
}

export function LineChart({ title, times, bucketSeconds, series, format, height = 220, empty }: TimeChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const n = times.length;
  const directLabels = series.length > 1 && series.length <= 4;
  const left = MARGIN.left;
  const right = width - MARGIN.right - (directLabels ? END_LABEL_SPACE : 0);
  const plotWidth = Math.max(right - left, 10);
  const bottom = height - MARGIN.bottom;
  const max = Math.max(0, ...series.flatMap((s) => s.values.map((v) => v ?? 0)));
  const scale = niceScale(max);
  const x = (i: number) => left + (n <= 1 ? plotWidth / 2 : (i * plotWidth) / (n - 1));
  const y = (v: number) => bottom - (v / scale.max) * (bottom - MARGIN.top);
  const hover = useHoverIndex(n, (px) => Math.round(((px - left) / plotWidth) * (n - 1)));
  const hasData = series.some((s) => s.values.some((v) => v !== null));

  // End labels sit at each series' last value, unless two would collide.
  const ends = series
    .map((s) => {
      const i = s.values.findLastIndex((v) => v !== null);
      return i < 0 ? null : { s, i, v: s.values[i] as number };
    })
    .filter((e) => e !== null);
  const ys = ends.map((e) => y(e.v)).sort((a, b) => a - b);
  const labelsFit = directLabels && ys.every((v, k) => k === 0 || v - ys[k - 1] >= 14);

  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      {series.length > 1 && <Legend series={series} mark="line" />}
      <div className="chart-frame" ref={ref} style={{ height }}>
        {width > 0 && (
          <svg
            width={width}
            height={height}
            tabIndex={0}
            role="img"
            aria-label={`${title}. Use the arrow keys to read values.`}
            onPointerMove={hover.onPointerMove}
            onPointerLeave={hover.onPointerLeave}
            onKeyDown={hover.onKeyDown}
            onBlur={hover.onBlur}
          >
            <YGrid ticks={scale.ticks} y={y} left={left} right={right} format={format} />
            <TimeAxis times={times} bucketSeconds={bucketSeconds} x={x} y={bottom} plotWidth={plotWidth} />
            {series.map((s) => (
              <path key={s.key} className="chart-line" stroke={s.color} d={linePath(s.values, x, y)} />
            ))}
            {/* Isolated points (no neighbour to draw a line to) still need a mark. */}
            {series.flatMap((s) =>
              s.values.map((v, i) =>
                v !== null && (s.values[i - 1] ?? null) === null && (s.values[i + 1] ?? null) === null ? (
                  <circle key={`${s.key}-${i}`} className="chart-dot" cx={x(i)} cy={y(v)} r={3} fill={s.color} />
                ) : null,
              ),
            )}
            {labelsFit &&
              ends.map((e) => (
                <text key={e.s.key} className="chart-end-label" x={right + 8} y={y(e.v)} dy="0.32em">
                  {e.s.label}
                </text>
              ))}
            {hover.index !== null && (
              <g>
                <line className="chart-crosshair" x1={x(hover.index)} x2={x(hover.index)} y1={MARGIN.top} y2={bottom} />
                {series.map((s) => {
                  const v = s.values[hover.index!];
                  return v === null ? null : (
                    <circle key={s.key} className="chart-dot" cx={x(hover.index!)} cy={y(v)} r={4} fill={s.color} />
                  );
                })}
              </g>
            )}
          </svg>
        )}
        {!hasData && <div className="chart-empty">{empty ?? "No data in this range"}</div>}
        {hover.index !== null && (
          <Tooltip
            x={x(hover.index)}
            width={width}
            title={formatBucketTime(times[hover.index], bucketSeconds, true)}
            rows={series.map((s) => ({
              key: s.key,
              label: s.label,
              color: s.color,
              value: s.values[hover.index!] === null ? "–" : format(s.values[hover.index!]!),
            }))}
          />
        )}
      </div>
      <TableView times={times} bucketSeconds={bucketSeconds} series={series} format={format} />
    </figure>
  );
}

function linePath(values: (number | null)[], x: (i: number) => number, y: (v: number) => number): string {
  let d = "";
  let drawing = false;
  values.forEach((v, i) => {
    if (v === null) {
      drawing = false;
      return;
    }
    d += `${drawing ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    drawing = true;
  });
  return d;
}

/** Stacked columns, series listed bottom to top. */
export function StackedColumns({ title, times, bucketSeconds, series, format, height = 220, empty }: TimeChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const n = times.length;
  const left = MARGIN.left;
  const right = width - MARGIN.right;
  const plotWidth = Math.max(right - left, 10);
  const bottom = height - MARGIN.bottom;
  const band = plotWidth / Math.max(n, 1);
  const GAP = 2;
  const barWidth = Math.max(1, Math.min(24, band - GAP));
  const totals = times.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] ?? 0), 0));
  const scale = niceScale(Math.max(0, ...totals));
  const x = (i: number) => left + band * (i + 0.5);
  const y = (v: number) => bottom - (v / scale.max) * (bottom - MARGIN.top);
  const hover = useHoverIndex(n, (px) => Math.floor((px - left) / band));
  const hasData = totals.some((t) => t > 0);

  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      {series.length > 1 && <Legend series={series} mark="rect" />}
      <div className="chart-frame" ref={ref} style={{ height }}>
        {width > 0 && (
          <svg
            width={width}
            height={height}
            tabIndex={0}
            role="img"
            aria-label={`${title}. Use the arrow keys to read values.`}
            onPointerMove={hover.onPointerMove}
            onPointerLeave={hover.onPointerLeave}
            onKeyDown={hover.onKeyDown}
            onBlur={hover.onBlur}
          >
            <YGrid ticks={scale.ticks} y={y} left={left} right={right} format={format} />
            <TimeAxis times={times} bucketSeconds={bucketSeconds} x={x} y={bottom} plotWidth={plotWidth} />
            {hover.index !== null && (
              <rect className="chart-hover-band" x={left + band * hover.index} width={band} y={MARGIN.top} height={bottom - MARGIN.top} />
            )}
            {times.map((_, i) => {
              const top = series.reduce((last, s, k) => ((s.values[i] ?? 0) > 0 ? k : last), -1);
              let base = 0;
              return series.map((s, k) => {
                const v = s.values[i] ?? 0;
                if (v <= 0) return null;
                const y0 = y(base);
                base += v;
                // 2px surface gap above every segment except the topmost
                const y1 = y(base) + (k === top ? 0 : GAP);
                const h = y0 - y1;
                if (h <= 0) return null;
                return (
                  <path
                    key={`${i}-${s.key}`}
                    fill={s.color}
                    d={columnPath(x(i) - barWidth / 2, y1, barWidth, h, k === top ? Math.min(4, barWidth / 2, h) : 0)}
                  />
                );
              });
            })}
          </svg>
        )}
        {!hasData && <div className="chart-empty">{empty ?? "No data in this range"}</div>}
        {hover.index !== null && (
          <Tooltip
            x={x(hover.index)}
            width={width}
            title={formatBucketTime(times[hover.index], bucketSeconds, true)}
            rows={[...series].reverse().map((s) => ({
              key: s.key,
              label: s.label,
              color: s.color,
              value: format(s.values[hover.index!] ?? 0),
            }))}
          />
        )}
      </div>
      <TableView times={times} bucketSeconds={bucketSeconds} series={series} format={format} />
    </figure>
  );
}

/** A column with rounded top corners and a square base. */
function columnPath(x: number, y: number, w: number, h: number, r: number): string {
  return [
    `M${x},${y + h}`,
    `V${y + r}`,
    r ? `Q${x},${y} ${x + r},${y}` : "",
    `H${x + w - r}`,
    r ? `Q${x + w},${y} ${x + w},${y + r}` : "",
    `V${y + h}`,
    "Z",
  ].join("");
}

export function BarList({
  title,
  rows,
  format,
  color = "var(--series-1)",
}: {
  title: string;
  rows: { key: string; label: string; value: number }[];
  format: (v: number) => string;
  color?: string;
}) {
  const max = Math.max(0, ...rows.map((r) => r.value));
  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      {rows.length === 0 || max === 0 ? (
        <div className="chart-empty static">No data in this range</div>
      ) : (
        <ul className="bar-list">
          {rows.map((r) => (
            <li key={r.key} title={`${r.label}: ${format(r.value)}`}>
              <span className="bar-label">{r.label}</span>
              <span className="bar-track">
                <span className="bar-fill" style={{ width: `${(r.value / max) * 100}%`, background: color }} />
                <span className="bar-value">{format(r.value)}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </figure>
  );
}

function TableView({ times, bucketSeconds, series, format }: Omit<TimeChartProps, "title">) {
  const rows = times
    .map((t, i) => ({ t, i }))
    .filter(({ i }) => series.some((s) => s.values[i] !== null && s.values[i] !== 0));
  return (
    <details className="chart-table">
      <summary>Table view</summary>
      {rows.length === 0 ? (
        <p className="muted">No data in this range.</p>
      ) : (
        <table className="attr-table">
          <thead>
            <tr>
              <th>Time</th>
              {series.map((s) => (
                <th key={s.key}>{s.label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ t, i }) => (
              <tr key={t}>
                <td>{formatBucketTime(t, bucketSeconds, true)}</td>
                {series.map((s) => (
                  <td key={s.key}>{s.values[i] === null ? "–" : format(s.values[i]!)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </details>
  );
}
