import type { Status } from "../types";

export function StatusBadge({ status }: { status: Status }) {
  const label = status === "unset" ? "pending" : status;
  return <span className={`badge status-${status}`}>{label}</span>;
}

export function KindTag({ kind }: { kind: string }) {
  return <span className={`kind kind-${kind}`}>{kind}</span>;
}

const REPLAY_LABELS = {
  replayable: "Replayable",
  partial: "Partially replayable",
  non_replayable: "Not replayable",
} as const;

export function ReplayBadge({ value }: { value: keyof typeof REPLAY_LABELS }) {
  return <span className={`badge replay-${value}`}>{REPLAY_LABELS[value]}</span>;
}

export function PassBadge({ passed }: { passed: boolean }) {
  return <span className={`badge ${passed ? "status-ok" : "status-error"}`}>{passed ? "✓ pass" : "✗ fail"}</span>;
}
