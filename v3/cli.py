"""Source 2 Porting Kit's noninteractive command-line entry point."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import signal
import sys
from pathlib import Path

from s2kit.core.agent_catalog import catalog, describe, doctor, prepare
from s2kit.core.agent_jobs import AgentRuntime, JobStore
from s2kit.core.agent_schema import RequestError


def common(parser) -> None:
    """Accept shared options before or after a subcommand."""
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--settings", type=Path, default=argparse.SUPPRESS,
                        help="read an alternate settings JSON without changing it")
    parser.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS,
                        help="directory for durable headless job records")


def parser() -> argparse.ArgumentParser:
    """Define discoverable, noninteractive commands."""
    root = argparse.ArgumentParser(description="Source 2 Porting Kit v3 agent CLI")
    common(root)
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("list", "describe", "validate", "run", "doctor"):
        command = commands.add_parser(name)
        common(command)
        if name in {"describe", "validate", "run"}:
            command.add_argument("tool_id")
        if name in {"validate", "run"}:
            command.add_argument("--params", help="parameter JSON file, or - for stdin")
            command.add_argument("--preferences", help="preference override JSON file, or - for stdin")
    jobs = commands.add_parser("jobs")
    common(jobs)
    job_commands = jobs.add_subparsers(dest="operation", required=True)
    for name in ("list", "status", "events", "cancel", "artifact"):
        command = job_commands.add_parser(name)
        common(command)
        if name != "list":
            command.add_argument("job_id")
        if name == "artifact":
            command.add_argument("path")
        if name in {"events", "artifact"}:
            command.add_argument("--cursor", type=int, default=0)
        if name in {"list", "events", "artifact"}:
            command.add_argument("--limit", type=int, default=100)
    return root


def read_json(filename: str | None) -> dict:
    """Read one strict JSON request object from disk or stdin."""
    if filename is None:
        return {}
    try:
        text = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8-sig")
        value = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError) as exc:
        raise RequestError({"json": str(exc)}) from exc
    if not isinstance(value, dict):
        raise RequestError({"json": "Expected an object"})
    return value


def print_event(event: dict) -> None:
    """Keep all progress off stdout so machine output remains parseable."""
    if event["kind"] == "log" and event["payload"]["level"] != "DEBUG":
        payload = event["payload"]
        print(f"[{payload['level']}] {payload['message']}", file=sys.stderr)


def execute(args) -> tuple[object, int]:
    """Dispatch a command and return its result and process exit status."""
    settings = getattr(args, "settings", None)
    state = getattr(args, "state_dir", None)
    if args.command == "list":
        return catalog(), 0
    if args.command == "describe":
        return describe(args.tool_id), 0
    if args.command == "doctor":
        return doctor(settings), 0
    if args.command == "jobs":
        store = JobStore(state)
        if args.operation == "list":
            return store.list(args.limit), 0
        if args.operation == "status":
            return store.get(args.job_id), 0
        if args.operation == "cancel":
            return store.cancel(args.job_id), 0
        if args.operation == "events":
            return store.events(args.job_id, args.cursor, args.limit), 0
        return store.read_artifact(args.job_id, args.path, args.cursor, args.limit), 0
    if args.params == "-" and args.preferences == "-":
        raise RequestError({"stdin": "Only one JSON input may read stdin"})
    params, overrides = read_json(args.params), read_json(args.preferences)
    if args.command == "validate":
        values, prefs = prepare(args.tool_id, params, overrides, settings)
        return {"valid": True, "tool_id": args.tool_id, "params": values, "preferences": prefs}, 0
    runtime = AgentRuntime(state, settings)
    previous = signal.getsignal(signal.SIGINT)
    interrupted = False

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True
        runtime.runner.cancel_all()

    try:
        signal.signal(signal.SIGINT, interrupt)
        started = runtime.start(args.tool_id, params, overrides, on_event=print_event)
        if interrupted:
            runtime.runner.cancel_all()
        result = runtime.wait(started["job_id"])
        code = 0 if result["status"] == "success" else 130 if result["status"] == "cancelled" else 1
        return result, code
    finally:
        signal.signal(signal.SIGINT, previous)
        runtime.close()


def main(argv: list[str] | None = None) -> int:
    """Run without launching a UI, opening a chooser or installing dependencies."""
    args = parser().parse_args(argv)
    try:
        with redirect_stdout(sys.stderr):
            result, code = execute(args)
    except RequestError as exc:
        result, code = {"status": "invalid", "errors": exc.errors}, 2
    except KeyboardInterrupt:
        result, code = {"status": "cancelled"}, 130
    except (OSError, ImportError) as exc:
        result, code = {"status": "unavailable", "errors": {"environment": str(exc)}}, 2
    except Exception as exc:
        result, code = {"status": "failed", "errors": {"runtime": str(exc)}}, 1
    if getattr(args, "json", False) or not isinstance(result, list):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in result:
            print(f"{item.get('id', item.get('job_id', '')):24} {item.get('title', item.get('status', ''))}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
