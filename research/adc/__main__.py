"""Only a local mock demo; writes SQLite and synthetic traces to a chosen directory."""
import argparse
import json
import os
from pathlib import Path

from .runner import demo
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="ADC P0 mock only; no model or network calls")
    parser.add_argument("--run-dir", default=os.getenv("CRACKRAG_RESEARCH_DIR"))
    args = parser.parse_args()
    if not args.run_dir:
        parser.error("provide --run-dir or CRACKRAG_RESEARCH_DIR outside the repository")
    directory = Path(args.run_dir).resolve()
    repository = Path(__file__).resolve().parents[2]
    if directory == repository or repository in directory.parents:
        parser.error("run directory must be outside the repository")
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / "adc-mock.sqlite3")
    try:
        report = demo(store)
        text = json.dumps(report, ensure_ascii=False, indent=2)
        (directory / "summary.json").write_text(text + "\n", encoding="utf-8")
        print(text)
    finally:
        store.close()


if __name__ == "__main__":
    main()
