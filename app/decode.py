#!/usr/bin/env python3
"""Offline decoder for .tgp handoff packages.

Usage:
    python3 -I app/decode.py PKG [--csv] [--original CSV]

Prints the readings in sampling order together with the pass interval and the
narrow / pass / wide verdict for each sample.  With --original it re-checks the
per-sample error bound and verdict agreement against the source CSV.

Only the standard library is needed, so an isolated review terminal can decode
a package without installing FastAPI or its dependencies.
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow `python3 -I -m app.decode` / `python3 -I app/decode.py`: isolated mode
# suppresses the implicit sys.path entries this module would normally get, so
# add the project root explicitly.  Only our own code is loaded from there; the
# package being decoded is read as data, never imported.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import pack, transfer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -I app/decode.py",
                                     description=__doc__)
    parser.add_argument("package", help="path to the .tgp package")
    parser.add_argument("--original", help="path to the original CSV to check")
    parser.add_argument(
        "--csv", action="store_true", help="print values as one-per-line CSV"
    )
    args = parser.parse_args(argv)

    with open(args.package, "rb") as handle:
        blob = handle.read()
    try:
        decoded = pack.decode_package(blob)
    except (ValueError, pack.PackError) as exc:
        print(f"error: cannot decode package: {exc}", file=sys.stderr)
        return 2

    lower, upper = decoded["lower"], decoded["upper"]
    print(
        f"# samples={decoded['count']} bytes={decoded['bytes']} "
        f"codec={decoded['codec']} approximated={decoded['approximated']} "
        f"interval=[{lower},{upper}] eps={decoded['eps']}",
        file=sys.stderr,
    )

    if args.csv:
        for value in decoded["values"]:
            print(value)
    else:
        for index, value in enumerate(decoded["values"], start=1):
            label = pack.VERDICT_LABELS[pack.verdict(value, lower, upper)]
            print(f"{index}\t{value}\t{label}")

    if args.original:
        with open(args.original, "rb") as handle:
            readings = transfer.parse_readings(handle.read())
        try:
            summary = transfer.verify_package(
                blob, readings.values, lower, upper, decoded["eps"]
            )
        except transfer.InputError as exc:
            print(f"error: verification failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"# verified against original, max_error={summary['max_error']}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
