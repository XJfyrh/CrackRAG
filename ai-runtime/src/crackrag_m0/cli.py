from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from .config import ConfigError, SCENARIOS, load_config, read_api_key
from .report import generate_report
from .runner import run_experiment


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="CrackRAG M0 frozen-prefix experiment (mock by default)")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a new experiment without overwriting previous runs")
    run.add_argument("--config", type=Path, default=Path("experiments/m0.toml"))
    run.add_argument("--provider", choices=("mock", "deepseek"))
    run.add_argument("--allow-live", action="store_true", help="explicitly permit real provider requests")
    run.add_argument("--env-file", type=Path, default=Path(".env"))
    run.add_argument("--output-dir", type=Path, help="parent directory for newly created run directories")
    run.add_argument("--run-id", help="optional unique directory name; existing directories are refused")
    run.add_argument("--repetitions", type=int)
    run.add_argument("--dispatch-mode", choices=("sequential", "concurrent"))
    run.add_argument("--mock-scenario", choices=SCENARIOS)
    report = commands.add_parser("report", help="verify artifacts and regenerate the report offline")
    report.add_argument("run_dir", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "report":
            summary = generate_report(args.run_dir.resolve())
            print(f"Report verified: {(args.run_dir / 'report.md').resolve()}")
            return 0 if not summary["incomplete"] else 1
        config = load_config(
            args.config, provider=args.provider, repetitions=args.repetitions,
            dispatch_mode=args.dispatch_mode, mock_scenario=args.mock_scenario,
            output_dir=args.output_dir.resolve() if args.output_dir else None,
        )
        key = ""
        if config.provider == "deepseek":
            if not args.allow_live:
                raise ConfigError("live requests disabled. Use --provider mock; "
                                  "after adding a key, explicitly pass --provider deepseek --allow-live.")
            key = read_api_key(args.env_file)
        print(f"M0 provider={config.provider}, mode={config.dispatch_mode}, "
              f"max_calls={config.repetitions * 2}, automatic_retries=0", flush=True)
        run_path = asyncio.run(run_experiment(config, allow_live=args.allow_live, api_key=key, run_id=args.run_id))
        # Already generated and verified by the runner; read without redundant recomputation.
        import json
        summary = json.loads((run_path / "summary.json").read_text(encoding="utf-8"))
        print(f"Report: {run_path / 'report.md'}")
        print(f"State: {summary['state']}; cost={summary['cost']['status']}; "
              f"simulated={summary['simulated']}")
        return 0 if summary["state"] == "COMPLETED" else 1
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Inspect the run journal; dispatched calls may have unknown outcomes/costs.", file=sys.stderr)
        return 130
