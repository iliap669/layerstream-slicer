"""Command-line entry: serve the web UI or probe a printer."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="layerstream-slicer",
        description="Layer-by-layer / mid-print-swap slicer for the Creality Ender-3 V3 KE.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the web UI (default if no command is given).")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    probe = sub.add_parser(
        "probe",
        help="Detect Moonraker vs stock Creality firmware on a printer IP.",
    )
    probe.add_argument("host", help="Printer hostname or IP, e.g. 192.168.68.72")
    probe.add_argument("--timeout", type=float, default=3.0)

    raw = list(sys.argv[1:] if argv is None else argv)
    # `layerstream-slicer` with no args (or only --host/--port) starts the server,
    # matching the Windows .venv\\Scripts\\layerstream-slicer.exe workflow.
    if not raw or raw[0].startswith("-"):
        raw = ["serve", *raw]
    args = parser.parse_args(raw)
    command = args.command or "serve"
    if command == "probe":
        from layerstream_slicer.transport.probe import probe_host_sync

        report = probe_host_sync(args.host, timeout=args.timeout)
        sys.stdout.write(report.render())
        return 0 if report.recommended != "none" else 2
    return _serve(getattr(args, "host", "127.0.0.1"), getattr(args, "port", 8000))


def _serve(host: str, port: int) -> int:
    import uvicorn

    uvicorn.run("layerstream_slicer.api.app:app", host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
