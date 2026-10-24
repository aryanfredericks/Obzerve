"""``obzerve`` command line: turn traces into regression cases, compare eval runs,
and gate releases in CI (exit code 1 when the gate fails).

Reads ``OBZERVE_ENDPOINT`` and ``OBZERVE_API_KEY`` from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from obzerve.client import ObzerveAPIError, ObzerveClient


def _client(args: argparse.Namespace) -> ObzerveClient:
    return ObzerveClient(args.endpoint, args.api_key)


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:+.1f}%"


def _money(value: float | None) -> str:
    return "–" if value is None else f"${value:.6f}"


def cmd_case_add(args: argparse.Namespace) -> int:
    assertions: list[dict] = []
    if args.no_error:
        assertions.append({"type": "no_error"})
    if args.contains_all:
        assertions.append({"type": "contains_all", "values": args.contains_all})
    if args.contains_any:
        assertions.append({"type": "contains_any", "values": args.contains_any})
    if args.not_contains:
        assertions.append({"type": "not_contains", "values": args.not_contains})
    if args.regex:
        assertions.append({"type": "regex", "pattern": args.regex})
    if args.max_latency_ms:
        assertions.append({"type": "max_latency_ms", "value": args.max_latency_ms})
    for raw in args.assertion or []:
        assertions.append(json.loads(raw))
    if not assertions:
        print("add at least one assertion (e.g. --no-error)", file=sys.stderr)
        return 2
    case = _client(args).create_case(
        args.dataset, args.trace_id, args.name, assertions, args.notes, args.mode
    )
    print(
        f"case {case['id']} added to {args.dataset!r} ({case['replayability']}, "
        f"{case['recordings']} recorded operations, replayed {case['replay_policy']})"
    )
    for reason in case["replay_reasons"]:
        print(f"  note: {reason}")
    return 0


def _print_comparison(cmp: dict) -> None:
    b, c = cmp["baseline"], cmp["candidate"]
    rows = [
        ("label", b["label"], c["label"]),
        ("prompt", _prompt(b), _prompt(c)),
        (
            "pass rate",
            f"{b['pass_rate']:.0%} ({b['passed']}/{b['cases']})",
            f"{c['pass_rate']:.0%} ({c['passed']}/{c['cases']})",
        ),
        (
            "est. cost / case",
            _money(b["estimated_cost_per_case_usd"]),
            _money(c["estimated_cost_per_case_usd"]),
        ),
        (
            "output tokens / case",
            _num(b["output_tokens_per_case"]),
            _num(c["output_tokens_per_case"]),
        ),
        ("p95 latency", _ms(b["latency_p95_ms"]), _ms(c["latency_p95_ms"])),
    ]
    width = max(len(r[1]) for r in rows) + 2
    print(f"{'':22}{'baseline':{width}}candidate")
    for name, x, y in rows:
        print(f"{name:22}{x:{width}}{y}")
    print(
        f"\nregressions: {cmp['regressions']}   fixes: {cmp['fixes']}   "
        f"cost/case {_pct(cmp['cost_per_case_delta_pct'])}   "
        f"p95 {_pct(cmp['latency_p95_delta_pct'])}"
    )
    if not cmp["comparable"]:
        print("NOT COMPARABLE: " + "; ".join(cmp["reasons"]))
    for case in cmp["cases"]:
        if case["change"] in ("regression", "fix", "still_failing"):
            why = f" — {'; '.join(case['failed_assertions'])}" if case["failed_assertions"] else ""
            print(f"  {case['change']:13} {case['case_name']}{why}")


def _prompt(run: dict) -> str:
    p = run.get("prompt")
    return f"{p['name']} v{p['version']}" if p else "–"


def _num(v: float | None) -> str:
    return "–" if v is None else f"{v:.0f}"


def _ms(v: float | None) -> str:
    return "–" if v is None else f"{v:.0f} ms"


def cmd_compare(args: argparse.Namespace) -> int:
    _print_comparison(_client(args).compare_runs(args.baseline, args.candidate))
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    result = _client(args).gate(
        args.baseline,
        args.candidate,
        max_regressions=args.max_regressions,
        min_pass_rate=args.min_pass_rate,
        max_pass_rate_drop=args.max_pass_rate_drop,
        max_cost_increase_pct=args.max_cost_increase_pct,
        max_p95_latency_increase_pct=args.max_p95_latency_increase_pct,
    )
    _print_comparison(result["comparison"])
    print()
    for check in result["checks"]:
        print(f"  [{'PASS' if check['passed'] else 'FAIL'}] {check['name']}: {check['detail']}")
    print("\nGATE " + ("PASSED" if result["passed"] else "FAILED"))
    return 0 if result["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obzerve", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--endpoint", default=os.environ.get("OBZERVE_ENDPOINT", "http://localhost:8000")
    )
    parser.add_argument("--api-key", default=os.environ.get("OBZERVE_API_KEY"))
    sub = parser.add_subparsers(dest="command", required=True)

    case = sub.add_parser("case", help="Regression cases").add_subparsers(dest="sub", required=True)
    add = case.add_parser("add", help="Turn a trace into a regression case")
    add.add_argument("trace_id")
    add.add_argument("--dataset", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--notes")
    add.add_argument(
        "--mode",
        choices=["recorded", "llm-live", "live"],
        default="llm-live",
        help="recorded: re-run the recorded failure exactly; llm-live: model live (default)",
    )
    add.add_argument("--no-error", action="store_true", help="the run must not raise")
    add.add_argument("--contains-all", nargs="+", metavar="TERM")
    add.add_argument("--contains-any", nargs="+", metavar="TERM")
    add.add_argument("--not-contains", nargs="+", metavar="TERM")
    add.add_argument("--regex", help="output must match (case-insensitive)")
    add.add_argument("--max-latency-ms", type=float)
    add.add_argument("--assertion", action="append", metavar="JSON", help="raw assertion spec")
    add.set_defaults(fn=cmd_case_add)

    ev = sub.add_parser("eval", help="Eval runs").add_subparsers(dest="sub", required=True)
    compare = ev.add_parser("compare", help="Compare a candidate run with a baseline run")
    compare.add_argument("baseline")
    compare.add_argument("candidate")
    compare.set_defaults(fn=cmd_compare)
    gate = ev.add_parser("gate", help="Fail (exit 1) if the candidate does not meet the criteria")
    gate.add_argument("baseline")
    gate.add_argument("candidate")
    gate.add_argument("--max-regressions", type=int, default=0)
    gate.add_argument("--min-pass-rate", type=float)
    gate.add_argument("--max-pass-rate-drop", type=float)
    gate.add_argument("--max-cost-increase-pct", type=float)
    gate.add_argument("--max-p95-latency-increase-pct", type=float)
    gate.set_defaults(fn=cmd_gate)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ObzerveAPIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
