import argparse
import json
from pathlib import Path
from .evidence import now, verify_run
from .runner import run_validation


def main():
    parser = argparse.ArgumentParser(description="Research evidence and guarded explicit paper-only execution")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--output", type=Path, default=Path("runs-local"))
    run.add_argument("--vibe-controls", action="store_true", help="Requires an already installed Vibe environment")
    verify = commands.add_parser("verify")
    verify.add_argument("run", type=Path)
    verify.add_argument("--registry", type=Path, required=True)
    phase2 = commands.add_parser("phase2-controls", help="Historical importer and offline paper OMS controls only")
    phase2.add_argument("--output", type=Path, required=True)
    phase2.add_argument("--historical-model-controls", action="store_true",
                        help="Also check fixed four-ETF features, dated trading terms and cash/FX wallet controls")
    phase2.add_argument("--integrated-account-controls", action="store_true",
                        help="Include model components and installed-Vibe full-CNY stock/wallet controls; requires existing Vibe")
    history = commands.add_parser("historical-import", help="Validate local rights/unit/timing declarations and convert only")
    history.add_argument("--manifest", type=Path, required=True)
    history.add_argument("--private-root", type=Path, required=True)
    history.add_argument("--output", type=Path, required=True)
    account = commands.add_parser("account-run", help="Run one private conditional model with independent stock/wallet replay")
    account.add_argument("--input", type=Path, required=True)
    account.add_argument("--private-root", type=Path, required=True)
    account.add_argument("--output", type=Path, required=True)
    study = commands.add_parser("study-run", help="Run frozen private continuous S02 matrix; requires existing Vibe")
    study.add_argument("--manifest", type=Path, required=True)
    study.add_argument("--private-root", type=Path, required=True)
    study.add_argument("--output", type=Path, required=True)
    paper = commands.add_parser("paper-local-readiness", help="Local SDK/config inventory only; no provider connection or order")
    paper.add_argument("--config", type=Path, required=True)
    paper.add_argument("--private-root", type=Path, required=True)
    alpaca = commands.add_parser("alpaca-paper-run", help="One explicit PAPER action; fixed paper origin, no live fallback or retries")
    alpaca.add_argument("--config", type=Path, required=True)
    alpaca.add_argument("--credentials", type=Path, required=True)
    alpaca.add_argument("--private-root", type=Path, required=True)
    alpaca.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "alpaca-paper-run":
        from .official_paper_run import run_alpaca
        code, folder = run_alpaca(args.config, args.credentials, args.private_root, args.output)
        print(json.dumps({"exit_code": code, "run_id": folder.name, "output": str(folder)}, ensure_ascii=False))
        return code
    if args.command == "paper-local-readiness":
        from .official_paper_run import local_readiness
        print(json.dumps(local_readiness(args.config, args.private_root), ensure_ascii=False))
        return 0
    if args.command == "run":
        code, folder = run_validation(args.spec, args.workspace, args.output, args.vibe_controls)
        print(json.dumps({"exit_code": code, "run_id": folder.name, "output": str(folder)}, ensure_ascii=False))
        return code
    if args.command == "phase2-controls":
        from .phase2 import run_phase2_validation
        code, folder = run_phase2_validation(args.output, historical_model_controls=args.historical_model_controls,
                                            integrated_account_controls=args.integrated_account_controls)
        print(json.dumps({"exit_code": code, "run_id": folder.name, "output": str(folder)}, ensure_ascii=False))
        return code
    if args.command == "historical-import":
        from .historical_input import import_history
        result = import_history(args.manifest, args.output, args.private_root, checked_at=now())
        print(json.dumps({"classification": result["classification"],
                          "data_kind": result["data_kind"],
                          "historical_engine_ready": result["historical_engine_ready"],
                          "output": str(args.output.resolve())}, ensure_ascii=False))
        return 0
    if args.command == "account-run":
        from .historical_run import run_account
        code, folder = run_account(args.input, args.output, args.private_root)
        print(json.dumps({"exit_code": code, "run_id": folder.name, "output": str(folder)}, ensure_ascii=False))
        return code
    if args.command == "study-run":
        from .historical_study import run_study
        code, folder = run_study(args.manifest, args.output, args.private_root)
        print(json.dumps({"exit_code": code, "run_id": folder.name, "output": str(folder)}, ensure_ascii=False))
        return code
    result = verify_run(args.run, args.registry)
    print(json.dumps(result, ensure_ascii=False))
    return int(not result["verified"])


if __name__ == "__main__":
    raise SystemExit(main())
