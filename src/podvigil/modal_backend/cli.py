"""Explicit provider commands; read-only commands never import the Modal SDK."""

import argparse
import json
import math
import sys

from .. import settings
from ..outcomes import EXIT_CODES
from ..records import save
from . import spec, state, logs


def parser():
    class Version(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            from ..identity import version_text

            print(version_text())
            parser.exit()

    def minutes(value):
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "Timeout must be a positive number of minutes."
            ) from None
        if not math.isfinite(number) or number <= 0:
            raise argparse.ArgumentTypeError("Timeout must be a positive number of minutes.")
        return number

    p = argparse.ArgumentParser(prog="podvigil-modal", description=__doc__)
    p.add_argument("--version", action=Version, nargs=0)
    p.add_argument(
        "--state-dir", help="local state root; Modal ledgers live in its modal/jobs subfolder"
    )
    commands = p.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="list local Modal job ledgers without cloud access")
    for name in ("plan", "launch"):
        sub = commands.add_parser(name)
        sub.add_argument("config")
        if name == "launch":
            sub.add_argument("--detach", action="store_true")
    for name in ("status", "wait", "cancel", "recover", "logs"):
        sub = commands.add_parser(name)
        sub.add_argument("job", help="Modal ledger path, latest or unique ID prefix")
        if name == "logs":
            sub.add_argument("--lines", type=int, choices=range(1, 1001), default=100, metavar="N")
        if name == "wait":
            sub.add_argument("--timeout", type=minutes, metavar="MINUTES")
        if name == "recover":
            sub.add_argument(
                "--assert-no-sandbox",
                action="store_true",
                help="explicitly assert no Sandbox exists after the deadline plus timeout; never inferred from elapsed time",
            )
            sub.add_argument("--accept-partial", action="store_true")
            sub.add_argument(
                "--stop-running",
                action="store_true",
                help="allow recovery to stop a Sandbox before its deadline",
            )
            sub.add_argument(
                "--sandbox-id",
                help="reconcile a lost receipt by ID after verifying both ownership tags",
            )
    return p


def run_cli(argv):
    def say(line):
        print(line, file=sys.stderr, flush=True)

    folder = None
    args = None
    try:
        args = parser().parse_args(argv)
        settings.configure(state_dir=args.state_dir)
        outcome = "success"
        if args.action == "list":
            result = {"jobs": state.list_jobs()}
        elif args.action == "plan":
            result = spec.plan(args.config)
            if result["price_check"]["warning"]:
                say(result["price_check"]["warning"])
        elif args.action == "launch":
            from .runner import launch

            result, outcome = launch(args.config, detach=args.detach, progress=say)
        else:
            folder = state.find_job(args.job, warn=say)
            if args.action == "status":
                result = state.status(folder)
            elif args.action == "logs":
                result = logs.snapshot(folder, args.lines)
            elif args.action == "wait":
                result, outcome = state.wait(folder, args.timeout, progress=say)
            elif args.action == "cancel":
                result = state.status(folder)
                if result["ended"]:
                    outcome = result["job_outcome"]
                if not result["ended"]:
                    save(folder / "cancel.json", {"requested": True})
                    result, outcome = state.status(folder), "pending"
                    say(
                        "Cancellation requested; a live watchdog will stop compute. If it is no longer heard, use podvigil-modal recover."
                    )
            else:
                from .runner import recover

                result, outcome = recover(
                    folder,
                    accept_partial=args.accept_partial,
                    stop_running=args.stop_running,
                    sandbox_id=args.sandbox_id,
                    assert_no_sandbox=args.assert_no_sandbox,
                )
    except KeyboardInterrupt:
        outcome = "interrupted"
        result = state.status(folder) if folder else {}
        say(
            "Waiting interrupted; the job is unaffected."
            if args and args.action == "wait"
            else "Interrupted; inspect the Modal ledger before assuming cleanup completed."
        )
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        result, outcome = {"error": "Invalid Modal command arguments."}, "usage_error"
    except Exception as exc:
        # Only explicitly typed project diagnostics are safe to display.
        from .runner import safe_error

        result, outcome = (
            {"error": safe_error(exc)},
            "error",
        )
        say(result["error"])
    finally:
        settings.configure()
    print(
        json.dumps(
            {**result, "outcome": outcome, "exit_code": EXIT_CODES[outcome]},
            indent=None if args and args.action == "launch" and args.detach else 2,
        ),
        flush=True,
    )
    return EXIT_CODES[outcome]
