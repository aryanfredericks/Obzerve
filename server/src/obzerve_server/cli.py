"""Admin CLI: ``obzerve-server <command>``; run with ``--help`` for the list."""

import argparse
import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select

from obzerve_server.auth import ALL_SCOPES, generate_api_key, hash_api_key
from obzerve_server.config import get_settings
from obzerve_server.db import create_engine, create_sessionmaker
from obzerve_server.models import ModelPrice, Project, ProjectApiKey

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(database_url: str):
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def migrate(database_url: str, revision: str = "head") -> None:
    from alembic import command

    command.upgrade(alembic_config(database_url), revision)


class _Db:
    """Engine + session for one CLI command."""

    async def __aenter__(self):
        self.engine = create_engine(get_settings())
        self.session = create_sessionmaker(self.engine)()
        return self.session

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.engine.dispose()


async def _project_by_name(session, name: str) -> Project | None:
    return await session.scalar(select(Project).where(Project.name == name))


async def _create_key(session, project: Project, key: str | None, scopes: list[str]) -> str:
    key = key or generate_api_key()
    session.add(
        ProjectApiKey(
            project_id=project.id,
            name="default",
            key_prefix=key[:12],
            key_hash=hash_api_key(key),
            scopes=scopes,
        )
    )
    return key


async def create_project(name: str, key: str | None, scopes: list[str], if_not_exists: bool) -> int:
    async with _Db() as session:
        project = await _project_by_name(session, name)
        if project is not None:
            if not if_not_exists:
                print(f"Project '{name}' already exists", file=sys.stderr)
                return 1
            if key and await session.scalar(
                select(ProjectApiKey).where(ProjectApiKey.key_hash == hash_api_key(key))
            ):
                print(f"Project '{name}' already exists with that key")
                return 0
        else:
            project = Project(name=name)
            session.add(project)
            await session.flush()
        new_key = await _create_key(session, project, key, scopes)
        await session.commit()
        print(f"project_id: {project.id}")
        print(f"api_key:    {new_key}")
        print("Store the API key now; only its hash is kept.")
        return 0


async def create_key(project_name: str, scopes: list[str]) -> int:
    async with _Db() as session:
        project = await _project_by_name(session, project_name)
        if project is None:
            print(f"No project named '{project_name}'", file=sys.stderr)
            return 1
        new_key = await _create_key(session, project, None, scopes)
        await session.commit()
        print(f"api_key: {new_key}")
        return 0


async def pricing_set(args) -> int:
    async with _Db() as session:
        project_id = None
        if args.project:
            project = await _project_by_name(session, args.project)
            if project is None:
                print(f"No project named '{args.project}'", file=sys.stderr)
                return 1
            project_id = project.id
        effective = args.effective_from or datetime(2000, 1, 1, tzinfo=UTC)
        existing = await session.scalar(
            select(ModelPrice).where(
                ModelPrice.project_id.is_(None)
                if project_id is None
                else ModelPrice.project_id == project_id,
                ModelPrice.provider.is_(None)
                if args.provider is None
                else ModelPrice.provider == args.provider,
                ModelPrice.model == args.model,
                ModelPrice.effective_from == effective,
            )
        )
        price = existing or ModelPrice(
            project_id=project_id,
            provider=args.provider,
            model=args.model,
            effective_from=effective,
        )
        price.input_usd_per_mtok = args.input
        price.output_usd_per_mtok = args.output
        price.cached_input_usd_per_mtok = args.cached_input
        price.note = args.note
        session.add(price)
        await session.commit()
        scope = f"project '{args.project}'" if args.project else "all projects"
        print(
            f"{'Updated' if existing else 'Added'} price for {args.model} ({scope}) "
            f"from {effective.date()}: ${args.input}/M input, ${args.output}/M output"
        )
        print("Run `obzerve-server pricing recompute` to re-price spans already stored.")
        return 0


async def pricing_list() -> int:
    async with _Db() as session:
        rows = await session.execute(
            select(ModelPrice, Project.name)
            .outerjoin(Project, Project.id == ModelPrice.project_id)
            .order_by(ModelPrice.model, ModelPrice.effective_from)
        )
        print(
            f"{'model':32} {'provider':10} {'project':12} {'from':10} "
            f"{'in/M':>10} {'out/M':>10} {'cached/M':>10}"
        )
        for price, project in rows:
            cached = price.cached_input_usd_per_mtok
            print(
                f"{price.model:32} {price.provider or '*':10} {project or '*':12} "
                f"{price.effective_from.date()!s:10} {price.input_usd_per_mtok:>10} "
                f"{price.output_usd_per_mtok:>10} {cached if cached is not None else '-':>10}"
            )
        return 0


async def pricing_recompute(project_name: str | None, since: datetime | None) -> int:
    from obzerve_server.pricing import recompute_costs

    async with _Db() as session:
        query = select(Project)
        if project_name:
            query = query.where(Project.name == project_name)
        projects = list(await session.scalars(query))
        if project_name and not projects:
            print(f"No project named '{project_name}'", file=sys.stderr)
            return 1
        for project in projects:
            count = await recompute_costs(session, project.id, since)
            print(f"{project.name}: re-priced {count} spans")
        return 0


async def run_worker() -> int:
    from prometheus_client import start_http_server

    from obzerve_server.queue import create_redis
    from obzerve_server.worker import IngestWorker

    settings = get_settings()
    engine = create_engine(settings)
    redis = create_redis(settings)
    worker = IngestWorker(redis, create_sessionmaker(engine), settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port)
    try:
        await worker.run()
    finally:
        await redis.aclose()
        await engine.dispose()
    return 0


async def queue_status() -> int:
    from obzerve_server.queue import IngestQueue, create_redis

    settings = get_settings()
    redis = create_redis(settings)
    try:
        depth = await IngestQueue(redis, settings).depth()
    finally:
        await redis.aclose()
    print(f"backlog:      {depth.length}")
    print(f"in progress:  {depth.pending}")
    print(f"dead letters: {depth.dead_letters}")
    return 0


async def queue_requeue_dead(limit: int) -> int:
    """Move dead-lettered messages back to the ingest stream (e.g. after a fix)."""
    from obzerve_server.queue import FIELD_ENQUEUED_AT, FIELD_PAYLOAD, FIELD_PROJECT, create_redis

    settings = get_settings()
    redis = create_redis(settings)
    moved = 0
    try:
        entries = await redis.xrange(settings.queue_dead_letter_stream, count=limit)
        for message_id, fields in entries:
            keep = {
                k: fields[k]
                for k in (FIELD_PROJECT, FIELD_PAYLOAD, FIELD_ENQUEUED_AT)
                if k in fields
            }
            async with redis.pipeline(transaction=True) as pipe:
                pipe.xadd(settings.queue_stream, keep)
                pipe.xdel(settings.queue_dead_letter_stream, message_id)
                await pipe.execute()
            moved += 1
    finally:
        await redis.aclose()
    print(f"requeued {moved} message(s)")
    return 0


def _decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value}") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("prices cannot be negative")
    return result


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obzerve-server")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    sub.add_parser("worker", help="Run a queue worker (OBZERVE_INGEST_MODE=queue)")
    sub.add_parser("migrate", help="Apply database migrations")

    scopes_help = f"Comma-separated scopes (default: {','.join(ALL_SCOPES)})"
    project = sub.add_parser("create-project", help="Create a project and an API key")
    project.add_argument("name")
    project.add_argument("--key", help="Use this API key instead of generating one (dev only)")
    project.add_argument("--scopes", default=",".join(ALL_SCOPES), help=scopes_help)
    project.add_argument("--if-not-exists", action="store_true")

    key = sub.add_parser("create-key", help="Create another API key for a project")
    key.add_argument("project")
    key.add_argument("--scopes", default=",".join(ALL_SCOPES), help=scopes_help)

    pricing = sub.add_parser("pricing", help="Manage model prices used for cost estimates")
    pricing_sub = pricing.add_subparsers(dest="pricing_command", required=True)
    price_set = pricing_sub.add_parser(
        "set", help="Add or update a model price (USD per 1M tokens)"
    )
    price_set.add_argument("model")
    price_set.add_argument("--input", type=_decimal, required=True, help="USD per 1M input tokens")
    price_set.add_argument(
        "--output", type=_decimal, required=True, help="USD per 1M output tokens"
    )
    price_set.add_argument("--cached-input", type=_decimal, help="USD per 1M cache-read tokens")
    price_set.add_argument("--provider", help="Only for this provider (default: any)")
    price_set.add_argument("--project", help="Only for this project (default: all projects)")
    price_set.add_argument(
        "--effective-from", type=_datetime, help="ISO date/time (default: applies to all history)"
    )
    price_set.add_argument("--note")
    pricing_sub.add_parser("list", help="List prices")
    recompute = pricing_sub.add_parser("recompute", help="Re-price stored spans")
    recompute.add_argument("--project")
    recompute.add_argument("--since", type=_datetime)

    queue = sub.add_parser("queue", help="Inspect the ingest queue")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_sub.add_parser("status", help="Show backlog, in-progress, and dead-letter counts")
    requeue = queue_sub.add_parser("requeue-dead", help="Retry dead-lettered messages")
    requeue.add_argument("--limit", type=int, default=1000)

    args = parser.parse_args(argv)
    if hasattr(args, "scopes"):
        scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
        unknown = set(scopes) - set(ALL_SCOPES)
        if unknown or not scopes:
            parser.error(f"invalid scopes: {sorted(unknown) or '(none)'}")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    match args.command:
        case "serve":
            import uvicorn

            uvicorn.run(
                "obzerve_server.app:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                reload=args.reload,
            )
            return 0
        case "worker":
            return asyncio.run(run_worker())
        case "migrate":
            migrate(get_settings().database_url)
            print("Migrations applied")
            return 0
        case "create-project":
            return asyncio.run(create_project(args.name, args.key, scopes, args.if_not_exists))
        case "create-key":
            return asyncio.run(create_key(args.project, scopes))
        case "pricing":
            if args.pricing_command == "set":
                return asyncio.run(pricing_set(args))
            if args.pricing_command == "list":
                return asyncio.run(pricing_list())
            return asyncio.run(pricing_recompute(args.project, args.since))
        case "queue":
            if args.queue_command == "status":
                return asyncio.run(queue_status())
            return asyncio.run(queue_requeue_dead(args.limit))
    return 1


if __name__ == "__main__":
    sys.exit(main())
