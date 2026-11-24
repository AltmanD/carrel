"""Validate a saved provider on real library text and the legacy GLM baseline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carrel.config import load_config
from carrel.model.verification import probe_provider, report_path, verify_provider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", help="provider ID in providers.toml")
    parser.add_argument("--data-dir")
    parser.add_argument("--config")
    parser.add_argument("--paper", default="", help="existing paper ID; otherwise use first available introduction")
    parser.add_argument("--baseline-provider", help="saved GLM provider ID; default uses the legacy [model] config")
    parser.add_argument("--probe-only", action="store_true", help="512-token connectivity check; cannot enable recommended routes")
    args = parser.parse_args()
    config = load_config(data_dir=args.data_dir, config_file=args.config)
    provider = config.providers.get(args.provider)
    if provider is None:
        print("Provider not found. Add it in /settings first.", file=sys.stderr)
        return 2
    if args.probe_only:
        result = probe_provider(provider, config.model)
        print(result)
        return 0 if result["ok"] else 2
    try:
        baseline = config.providers.get(args.baseline_provider) if args.baseline_provider else None
        if args.baseline_provider and baseline is None:
            raise ValueError("Baseline provider not found")
        report = verify_provider(config, provider, paper_id=args.paper, baseline_provider=baseline,
                                 progress=lambda task, done, total: print(f"[{done}/{total}] {task}", flush=True))
    except Exception as exc:
        print(provider.redact(str(exc)), file=sys.stderr)
        return 2
    print("task               candidate   GLM baseline   coverage / hard QA")
    for row in report["tasks"]:
        a, b = row["candidate"], row["baseline"]
        print(f"{row['task']:<18} {'PASS' if a['passed'] else 'FAIL':<11} {'PASS' if b['passed'] else 'FAIL':<14} {a.get('coverage')} / {a.get('hard_qa')}")
        for key in ("candidate", "baseline"):
            if row[key].get("errors"):
                print(f"  {key}: " + "; ".join(row[key]["errors"]))
                print(row[key].get("response", ""))
    print("PASS" if report["passed"] else "FAIL — recommended routing remains disabled")
    print(f"Report: {report_path(config.data_dir, provider.id)}")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
