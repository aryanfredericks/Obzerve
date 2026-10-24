export function formatDuration(ms: number): string {
  if (ms === 0) return "0";
  if (ms < 1) return `${(ms * 1000).toFixed(0)} µs`;
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 1 : 0)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.floor(ms / 60_000)}m ${((ms % 60_000) / 1000).toFixed(0)}s`;
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function relativeTime(iso: string): string {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

export function formatNumber(n: number): string {
  return n.toLocaleString();
}

/** Pretty-print JSON payloads; leave plain text as is. */
export function prettyPayload(text: string): string {
  const trimmed = text.trim();
  if (trimmed.startsWith('"')) {
    // A JSON-encoded string (exact replay recording): show the string itself.
    try {
      const value = JSON.parse(trimmed);
      if (typeof value === "string") return value;
    } catch {
      return text;
    }
  }
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return text;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return text;
  }
}

/** Offset of `iso` from `originMs`, in ms, with microsecond precision. */
export function offsetMs(iso: string, originMs: number): number {
  return parseIsoMs(iso) - originMs;
}

/** Date.parse drops sub-millisecond digits; keep them for short spans. */
export function parseIsoMs(iso: string): number {
  const base = Date.parse(iso);
  const match = /\.(\d+)/.exec(iso);
  if (!match || match[1].length <= 3) return base;
  return base + Number(`0.${match[1].slice(3)}`);
}

/** Estimated USD cost; small amounts keep significant digits instead of rounding to $0.00. */
export function formatCost(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toPrecision(2)}`;
  if (usd < 100) return `$${usd.toFixed(2)}`;
  return `$${Math.round(usd).toLocaleString()}`;
}

export function formatPercent(fraction: number | null): string {
  if (fraction === null) return "–";
  const pct = fraction * 100;
  if (pct > 0 && pct < 0.1) return "<0.1%";
  if (pct < 100 && pct > 99.9) return ">99.9%";
  return `${pct < 10 && pct !== 0 ? pct.toFixed(1) : pct.toFixed(0)}%`;
}

/** 1,284 / 12.9K / 4.2M / 1.1B, independent of the browser locale. */
export function formatCompact(n: number): string {
  const abs = Math.abs(n);
  for (const [size, suffix] of [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ] as const) {
    if (abs >= size * 10) return `${Math.round(n / size)}${suffix}`;
    if (abs >= size) return `${(n / size).toFixed(1).replace(/\.0$/, "")}${suffix}`;
  }
  return n.toLocaleString();
}
