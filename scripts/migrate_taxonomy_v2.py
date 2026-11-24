"""Offline reviewed taxonomy migration: dry-run, apply, resume, rollback."""
import argparse
import json
from pathlib import Path
from carrel.classify.migration import plan_migration, execute_plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['dry-run', 'apply', 'resume', 'rollback'])
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--keyword-policy', type=Path, help='Reviewed JSON map of legacy terms to promote/exclude')
    args = parser.parse_args()
    if args.mode == 'dry-run':
        policy = json.loads(args.keyword_policy.read_text(encoding='utf-8')) if args.keyword_policy else None
        report = json.dumps(plan_migration(args.data_dir, keyword_policy=policy), ensure_ascii=False, indent=2)
        if args.manifest:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(report, encoding='utf-8')
            print(f'Migration preview saved: {args.manifest}')
        else:
            print(report)
    else:
        if not args.manifest:
            parser.error('--manifest is required; save and review dry-run output first')
        execute_plan(args.data_dir, args.manifest, rollback=args.mode == 'rollback')


if __name__ == '__main__':
    main()
