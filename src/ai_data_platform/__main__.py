"""Small CLI adapter for the reusable capability layer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_data_platform import discover_datasets, load_registry


def _default_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "registry"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and discover semantic dataset contracts.")
    parser.add_argument("--registry", type=Path, default=_default_registry_path())
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover_parser = subparsers.add_parser("discover", help="Discover ranked datasets.")
    discover_parser.add_argument("question")
    discover_parser.add_argument("--limit", type=int, default=5)
    subparsers.add_parser("validate", help="Validate registry metadata and references.")
    args = parser.parse_args()

    registry = load_registry(args.registry)
    if args.command == "validate":
        print("Registry is valid.")
        return 0
    result = discover_datasets(args.question, registry, args.limit)
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
