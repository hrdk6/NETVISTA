"""Entry point:  python -m netvista [--host 0.0.0.0] [--port 8000] [--topology file.json]"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    ap = argparse.ArgumentParser(prog="netvista", description="NETVISTA network digital twin backend")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--topology", type=Path, default=None, help="topology JSON (default: topologies/default.json)")
    ap.add_argument("--switch", choices=["ovs", "linuxbridge"], default=None)
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    if os.geteuid() != 0:
        print("NETVISTA must run as root (Mininet creates namespaces, veths and qdiscs).", file=sys.stderr)
        print("  WSL:   wsl -d Ubuntu-24.04 -u root -- bash scripts/run.sh", file=sys.stderr)
        print("  Linux: sudo bash scripts/run.sh", file=sys.stderr)
        sys.exit(1)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    if args.topology:
        settings.topology_path = args.topology.resolve()
    if args.switch:
        settings.switch_impl = args.switch
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level=args.log_level, ws_ping_interval=20)


if __name__ == "__main__":
    main()
