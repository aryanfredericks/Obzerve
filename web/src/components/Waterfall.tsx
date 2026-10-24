import { useMemo, useState } from "react";
import { formatDuration, offsetMs, parseIsoMs } from "../format";
import type { Span } from "../types";
import { KindTag } from "./Badges";

interface Row {
  span: Span;
  depth: number;
  hasChildren: boolean;
}

/** Depth-first ordering of spans; spans whose parent is missing become roots. */
function buildRows(spans: Span[], collapsed: Set<string>): Row[] {
  const ids = new Set(spans.map((s) => s.span_id));
  const children = new Map<string | null, Span[]>();
  for (const span of spans) {
    const parent = span.parent_span_id && ids.has(span.parent_span_id) ? span.parent_span_id : null;
    const list = children.get(parent) ?? [];
    list.push(span);
    children.set(parent, list);
  }
  for (const list of children.values()) {
    list.sort((a, b) => parseIsoMs(a.start_time) - parseIsoMs(b.start_time));
  }
  const rows: Row[] = [];
  const visit = (parent: string | null, depth: number) => {
    for (const span of children.get(parent) ?? []) {
      const hasChildren = children.has(span.span_id);
      rows.push({ span, depth, hasChildren });
      if (hasChildren && !collapsed.has(span.span_id)) visit(span.span_id, depth + 1);
    }
  };
  visit(null, 0);
  return rows;
}

const TICKS = [0, 0.25, 0.5, 0.75, 1];

export function Waterfall({
  spans,
  selectedId,
  onSelect,
}: {
  spans: Span[];
  selectedId: string | null;
  onSelect: (spanId: string) => void;
}) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const rows = useMemo(() => buildRows(spans, collapsed), [spans, collapsed]);

  const origin = Math.min(...spans.map((s) => parseIsoMs(s.start_time)));
  const end = Math.max(...spans.map((s) => parseIsoMs(s.end_time)));
  const total = Math.max(end - origin, 0.001);

  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="waterfall" role="tree">
      <div className="wf-row wf-header">
        <div className="wf-label">Operation</div>
        <div className="wf-track">
          {TICKS.map((t) => (
            <span key={t} className="wf-tick" style={{ left: `${t * 100}%` }}>
              {formatDuration(total * t)}
            </span>
          ))}
        </div>
      </div>
      {rows.map(({ span, depth, hasChildren }) => {
        const left = (offsetMs(span.start_time, origin) / total) * 100;
        const width = Math.max((span.duration_ms / total) * 100, 0.4);
        const isCollapsed = collapsed.has(span.span_id);
        return (
          <div
            key={span.span_id}
            role="treeitem"
            aria-selected={span.span_id === selectedId}
            aria-expanded={hasChildren ? !isCollapsed : undefined}
            className={`wf-row${span.span_id === selectedId ? " selected" : ""}${span.status === "error" ? " has-error" : ""}`}
            onClick={() => onSelect(span.span_id)}
          >
            <div className="wf-label" style={{ paddingLeft: 8 + depth * 16 }}>
              {hasChildren ? (
                <button
                  className="wf-toggle"
                  aria-label={isCollapsed ? "Expand" : "Collapse"}
                  onClick={(e) => {
                    e.stopPropagation();
                    toggle(span.span_id);
                  }}
                >
                  {isCollapsed ? "▸" : "▾"}
                </button>
              ) : (
                <span className="wf-toggle-spacer" />
              )}
              <KindTag kind={span.kind} />
              <span className="wf-name" title={span.name}>
                {span.name}
              </span>
              {span.status === "error" && <span className="wf-flag error" title={span.error_type ?? "error"}>!</span>}
              {span.retry_count > 0 && (
                <span className="wf-flag retry" title={`${span.retry_count} retries`}>
                  ↻{span.retry_count}
                </span>
              )}
            </div>
            <div className="wf-track">
              {TICKS.slice(1, -1).map((t) => (
                <span key={t} className="wf-gridline" style={{ left: `${t * 100}%` }} />
              ))}
              <div
                className={`wf-bar kind-bg-${span.kind}${span.status === "error" ? " error" : ""}`}
                style={{ left: `${left}%`, width: `${width}%` }}
              />
              <span
                className="wf-duration"
                style={left + width > 80 ? { right: `${100 - left + 0.5}%` } : { left: `${left + width + 0.5}%` }}
              >
                {formatDuration(span.duration_ms)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
