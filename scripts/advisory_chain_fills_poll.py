"""Advisory chain_fills poller CLI (v2 B1 + B2).

执行: LD_PRELOAD="" uv run python scripts/advisory_chain_fills_poll.py [--profile analyze|trade|all]

systemd timer 调用此脚本每 60s 跑一次。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.advisory.chain_fills_poller import poll_profile, poll_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="all", choices=["all", "analyze", "trade"])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.profile == "all":
        out = poll_all()
    else:
        out = {"results": [poll_profile(args.profile).as_dict()]}
    print(json.dumps(out, indent=2, default=str))
    has_error = any(r.get("error") for r in out.get("results", []))
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
