"""Run the demo research agent and send its traces to Obzerve.

research-agent ["question"] [--fault F]           run the agent
research-agent replay TRACE_ID [--mode M]          replay a recorded trace
research-agent eval DATASET --label L              run an eval dataset against this agent
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from obzerve import Observer
from obzerve.client import ObzerveAPIError, ObzerveClient
from obzerve.evals import run_eval
from obzerve.replay import POLICIES
from research_agent.agent import ResearchAgent
from research_agent.faults import Fault
from research_agent.llm import LLMTimeoutError, LLMUnavailableError, MockLLM, OllamaLLM
from research_agent.prompts import PROMPTS

DEFAULT_QUESTION = "Compare recent approaches to RAG evaluation"
PROMPT_NAME = "research_agent"


def _common(parser: argparse.ArgumentParser) -> None:
    env = os.environ.get
    parser.add_argument("--endpoint", default=env("OBZERVE_ENDPOINT", "http://localhost:8000"))
    parser.add_argument("--api-key", default=env("OBZERVE_API_KEY", "dev_project_key"))
    parser.add_argument(
        "--dashboard-url", default=env("OBZERVE_DASHBOARD_URL", "http://localhost:5173")
    )
    parser.add_argument(
        "--llm",
        choices=["mock", "ollama"],
        default=env("RESEARCH_AGENT_LLM", "mock"),
        help="mock: deterministic offline stand-in (default); ollama: a real local model",
    )
    parser.add_argument(
        "--ollama-model", default=env("RESEARCH_AGENT_OLLAMA_MODEL", "qwen3:latest")
    )
    parser.add_argument("--ollama-url", default=env("OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--llm-timeout", type=float, help="seconds (default: 1 mock, 120 ollama)")
    parser.add_argument("--think", action="store_true", help="enable Qwen3-style thinking mode")
    parser.add_argument(
        "--prompt",
        choices=sorted(PROMPTS),
        default="grounded",
        help="prompt configuration (registered as a prompt version)",
    )
    parser.add_argument(
        "--fallback",
        action="store_true",
        help="degrade gracefully when the LLM fails instead of raising",
    )


def _build(args: argparse.Namespace) -> tuple[Observer, ObzerveClient, ResearchAgent]:
    from research_agent.retrieval import LazyDenseRetriever

    observer = Observer(endpoint=args.endpoint, api_key=args.api_key, service_name="research-agent")
    client = ObzerveClient(args.endpoint, args.api_key)
    if args.llm == "ollama":
        url = args.ollama_url if "://" in args.ollama_url else f"http://{args.ollama_url}"
        llm = OllamaLLM(
            args.ollama_model, url, timeout_s=args.llm_timeout or 120.0, think=args.think
        )
    else:
        llm = MockLLM(timeout_s=args.llm_timeout or 1.0)
    prompt = PROMPTS[args.prompt]
    try:
        version = client.register_prompt(PROMPT_NAME, prompt.as_config(llm.model))
    except (ObzerveAPIError, OSError) as exc:  # e.g. a key without the `write` scope
        print(f"warning: prompt version not registered ({exc})", file=sys.stderr)
        version = None
    label = f" (prompt {PROMPT_NAME} v{version['version']})" if version else ""
    print(f"llm: {llm.model} via {llm.provider}; prompt: {prompt.label}{label}")
    agent = ResearchAgent(
        observer,
        LazyDenseRetriever(),
        llm,
        prompt=prompt,
        prompt_version=version,
        fallback_on_llm_failure=args.fallback,
    )
    return observer, client, agent


def _finish(observer: Observer) -> None:
    if not observer.flush(timeout_ms=10_000):
        print("warning: not all telemetry was exported", file=sys.stderr)
    observer.shutdown()


def cmd_run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="research-agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("question", nargs="?", default=DEFAULT_QUESTION)
    parser.add_argument("--fault", type=Fault, choices=list(Fault), default=Fault.NONE)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--quiet", action="store_true", help="Do not print the report")
    _common(parser)
    args = parser.parse_args(argv)
    observer, _, agent = _build(args)

    failures = 0
    for _ in range(args.runs):
        try:
            result = agent.run(args.question, fault=args.fault)
        except (LLMTimeoutError, LLMUnavailableError) as exc:
            failures += 1
            print(f"run failed: {exc}", file=sys.stderr)
            print(f"trace: {args.dashboard_url}/traces/{agent.last_trace_id}")
            continue
        if not args.quiet:
            print(result.report, end="\n\n")
        print(f"trace: {args.dashboard_url}/traces/{result.trace_id}")
    _finish(observer)
    return 1 if failures else 0


def cmd_replay(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="research-agent replay", description="Replay a recorded trace of this agent."
    )
    parser.add_argument("trace_id")
    parser.add_argument(
        "--mode",
        choices=sorted(POLICIES),
        default="recorded",
        help="recorded: nothing live; llm-live: model live, tools recorded; "
        "live: everything live (side effects stay blocked)",
    )
    parser.add_argument("--label", help="free-text note stored on the replay")
    parser.add_argument("--quiet", action="store_true")
    _common(parser)
    args = parser.parse_args(argv)
    observer, client, agent = _build(args)

    bundle = client.replay_bundle(args.trace_id)
    print(
        f"replaying {bundle.trace_id} ({bundle.replayability}, "
        f"{len(bundle.recordings)} recorded operations) in {args.mode} mode"
    )
    for reason in bundle.reasons:
        print(f"  note: {reason}")
    if bundle.replayability == "non_replayable":
        return 2
    failed = None
    with observer.replay(bundle, POLICIES[args.mode], label=args.label) as session:
        try:
            result = agent.run(**bundle.root_input)
        except Exception as exc:  # reproducing a failure is a valid replay outcome
            failed = exc
    print(
        f"recorded calls: {session.recorded_calls}, live calls: {session.live_calls}, "
        f"diverged: {', '.join(session.divergences) or 'none'}"
    )
    if failed is not None:
        print(f"replay failed: {type(failed).__name__}: {failed}")
    elif not args.quiet:
        print(result.report)
    print(f"replay:  {args.dashboard_url}/traces/{session.root_trace_id}")
    print(f"compare: {args.dashboard_url}/traces/{bundle.trace_id}/compare/{session.root_trace_id}")
    _finish(observer)
    return 0


def cmd_eval(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="research-agent eval", description="Run an eval dataset against this agent."
    )
    parser.add_argument("dataset")
    parser.add_argument("--label", required=True, help="name for this run, e.g. 'baseline'")
    parser.add_argument(
        "--mode",
        choices=sorted(POLICIES),
        help="force one replay policy for every case (default: each case's own)",
    )
    _common(parser)
    args = parser.parse_args(argv)
    observer, client, agent = _build(args)

    def on_case(case: dict, result: dict) -> None:
        verdict = "PASS" if result["passed"] else "FAIL"
        print(f"  [{verdict}] {case['name']} ({result['latency_ms']:.0f} ms)")
        for a in result["assertions"]:
            if not a["passed"]:
                print(f"         {a['type']}: {a['detail']}")

    print(f"running dataset {args.dataset!r} as {args.label!r} ({args.mode or 'per-case policy'})")
    run = run_eval(
        observer,
        client,
        args.dataset,
        lambda root_input: agent.run(**root_input).report,
        label=args.label,
        policy=POLICIES[args.mode] if args.mode else None,
        prompt_version=agent.prompt_version,
        config={
            "llm": agent.llm.model,
            "prompt": agent.prompt.label,
            "fallback_on_llm_failure": agent.fallback_on_llm_failure,
        },
        on_case=on_case,
    )["run"]
    print(
        f"\npass rate {run['pass_rate']:.0%} ({run['passed']}/{run['cases']}), "
        f"dataset v{run['dataset_version']}"
    )
    print(f"run: {run['id']}")
    print(f"     {args.dashboard_url}/evals/runs/{run['id']}")
    _finish(observer)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.WARNING)
    commands = {"replay": cmd_replay, "eval": cmd_eval}
    if argv and argv[0] in commands:
        return commands[argv[0]](argv[1:])
    return cmd_run(argv)


if __name__ == "__main__":
    sys.exit(main())
