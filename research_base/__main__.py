import argparse
import json
from pathlib import Path
from .evidence import verify_run
from .runner import run_validation


def main():
    parser = argparse.ArgumentParser(description="Offline research evidence and acceptance; no broker orders")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--output", type=Path, default=Path("runs-local"))
    run.add_argument("--vibe-controls", action="store_true", help="Requires an already installed Vibe environment")
    verify = commands.add_parser("verify")
    verify.add_argument("run", type=Path)
    verify.add_argument("--registry", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        code, folder = run_validation(args.spec, args.workspace, args.output, args.vibe_controls)
        print(json.dumps({"exit_code": code, "run_id": folder.name, "output": str(folder)}, ensure_ascii=False))
        return code
    result = verify_run(args.run, args.registry)
    print(json.dumps(result, ensure_ascii=False))
    return int(not result["verified"])


if __name__ == "__main__":
    raise SystemExit(main())
