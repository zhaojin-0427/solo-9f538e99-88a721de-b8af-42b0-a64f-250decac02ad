"""Review-workflow domain rules: registration, re-judgement, supplements.

Terminology
-----------
decoded
    The value stored in the package.  For a lossless package this equals the
    original reading; for an approximated package it may differ by up to the
    package's error limit.
possible range
    The set of original readings that could have produced the package sample.
    For a lossless package it is the single decoded value.  For an approximated
    package it is the intersection of two constraints (see :func:`possible_range`).
known original
    A value supplied later through supplement (补录).  It is authoritative: the
    decoded value is no longer used for that sample.

Re-judging a sample under a *new* pass interval therefore has one of three
states:

* ``narrow`` / ``pass`` / ``wide`` -- the answer is settled;
* ``unknown`` (待查) -- an approximated sample whose possible originals do not
  all give the same verdict under the new interval.  The reviewer has to find
  the original record or supplement the value.

Supplemented values and the package/sample rows live in SQLite; see
:mod:`app.db` for the transaction and snapshot rules.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from . import pack

VERDICT_UNKNOWN = "unknown"

# Label for the "needs the original record" state, alongside pack.VERDICT_LABELS.
RESULT_LABELS = {**pack.VERDICT_LABELS, VERDICT_UNKNOWN: "待查"}

MAX_RANGE_SAMPLES = 5_000_000
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class ReviewError(ValueError):
    """A review request that must be rejected as a whole."""


def possible_range(decoded: int, eps: int, old_verdict: str,
                   old_lower: int, old_upper: int,
                   approximated: bool) -> tuple[int, int]:
    """Interval of originals that are compatible with one package sample.

    An approximated sample only tells us the decoded value plus the package's
    own guarantees.  The original must be within ``eps`` of the decoded value
    **and** its old verdict must equal the old verdict of the decoded value:

    * old ``narrow``: original < old_lower
    * old ``pass``:   old_lower <= original <= old_upper
    * old ``wide``:   original > old_upper

    A lossless package (``approximated == False``) stores the originals
    verbatim, so its range is the single decoded value even when the header
    advertises a non-zero error limit that was not used.
    """
    if not approximated:
        return decoded, decoded
    lo, hi = decoded - eps, decoded + eps
    if old_verdict == "narrow":
        hi = min(hi, old_lower - 1)
    elif old_verdict == "wide":
        lo = max(lo, old_upper + 1)
    else:
        lo = max(lo, old_lower)
        hi = min(hi, old_upper)
    lo = max(lo, pack.INT32_MIN)
    hi = min(hi, pack.INT32_MAX)
    if lo > hi:
        # Defensive: a well-formed package never loses every original in its
        # own band, but a hand-crafted header could.  Fall back to the decoded
        # value so registration still records something meaningful.
        return decoded, decoded
    return lo, hi


def judge_range(lo: int, hi: int, lower: int, upper: int) -> list[str]:
    """Possible verdicts over an inclusive interval of originals.

    The three verdict regions are contiguous and ordered (narrow, pass, wide),
    so checking non-empty intersection of ``[lo, hi]`` with each region is
    exact and independent of the interval width.
    """
    found = []
    if lo <= min(hi, lower - 1):
        found.append("narrow")
    if max(lo, lower) <= min(hi, upper):
        found.append("pass")
    if max(lo, upper + 1) <= hi:
        found.append("wide")
    return found


def sample_judgement(*, sample_no: int, decoded: int, lo: int, hi: int,
                     known: int | None, lower: int, upper: int) -> dict:
    """Judge one sample under a new interval, reporting how sure the answer is.

    ``known`` is the supplemented original, if any.  A known original is used
    verbatim and its range is a single point.
    """
    if known is not None:
        verdict = pack.verdict(known, lower, upper)
        return _point(sample_no, known, verdict, "supplement", decoded)
    if lo == hi:
        # Lossless package (or an exactly pinned band): the decoded value is
        # the original, so the judgement is always certain.
        verdict = pack.verdict(decoded, lower, upper)
        return _point(sample_no, decoded, verdict, "decoded", decoded)
    verdicts = judge_range(lo, hi, lower, upper)
    if len(verdicts) == 1:
        return {
            "sample_no": sample_no,
            "verdict": verdicts[0],
            "label": RESULT_LABELS[verdicts[0]],
            "certain": True,
            "source": "decoded",
            "value": None,
            "decoded": decoded,
            "possible": {"lo": lo, "hi": hi},
        }
    return {
        "sample_no": sample_no,
        "verdict": VERDICT_UNKNOWN,
        "label": RESULT_LABELS[VERDICT_UNKNOWN],
        "certain": False,
        "source": "decoded",
        "value": None,
        "decoded": decoded,
        "possible": {"lo": lo, "hi": hi},
        "candidates": verdicts,
    }


def _point(sample_no: int, value: int, verdict: str, source: str,
           decoded: int) -> dict:
    return {
        "sample_no": sample_no,
        "verdict": verdict,
        "label": RESULT_LABELS[verdict],
        "certain": True,
        "source": source,
        "value": value,
        "decoded": decoded,
        "possible": {"lo": value, "hi": value},
    }


@dataclass
class Registered:
    package_no: int
    sample_count: int
    old_lower: int
    old_upper: int
    eps: int
    approximated: bool


def register_package(conn: sqlite3.Connection, blob: bytes,
                     decoded: dict) -> Registered:
    """Persist one verified package and its per-sample ranges.

    The caller has already decoded and verified *blob*, so registration only
    records facts.  ``possible_lo`` / ``possible_hi`` are computed once here
    because supplements validate against them on every batch.
    """
    lower, upper = decoded["lower"], decoded["upper"]
    eps = decoded["eps"]
    values = decoded["values"]
    if len(values) != decoded["count"]:
        raise ReviewError("decoded sample count mismatch")
    if len(values) > MAX_RANGE_SAMPLES:
        raise ReviewError("package has too many samples to register")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    digest = hashlib.sha256(blob).hexdigest()
    cur = conn.execute(
        """
        INSERT INTO packages (registered_at, sample_count, old_lower, old_upper,
                              eps, approximated, codec, byte_size, sha256,
                              package)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now, len(values), lower, upper, eps,
         1 if decoded["approximated"] else 0, decoded["codec"], len(blob),
         digest, blob),
    )
    package_no = int(cur.lastrowid)

    rows = []
    for sample_no, value in enumerate(values, start=1):
        lo, hi = possible_range(
            value, eps, pack.verdict(value, lower, upper), lower, upper,
            decoded["approximated"],
        )
        rows.append((package_no, sample_no, value, lo, hi))
    conn.executemany(
        "INSERT INTO samples (package_no, sample_no, decoded, possible_lo, "
        "possible_hi) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return Registered(package_no, len(values), lower, upper, eps,
                      bool(decoded["approximated"]))


def load_package(conn: sqlite3.Connection, package_no: int) -> Registered:
    if not INT64_MIN <= package_no <= INT64_MAX:
        raise ReviewError(f"unknown package number {package_no}")
    row = conn.execute(
        "SELECT package_no, sample_count, old_lower, old_upper, eps, "
        "approximated FROM packages WHERE package_no = ?",
        (package_no,),
    ).fetchone()
    if row is None:
        raise ReviewError(f"unknown package number {package_no}")
    return Registered(
        int(row["package_no"]), int(row["sample_count"]), int(row["old_lower"]),
        int(row["old_upper"]), int(row["eps"]), bool(row["approximated"]),
    )


def _sample_rows(conn: sqlite3.Connection, package_no: int,
                 first: int, last: int) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.sample_no, s.decoded, s.possible_lo, s.possible_hi, o.value
          FROM samples AS s
          LEFT JOIN originals AS o
            ON o.package_no = s.package_no AND o.sample_no = s.sample_no
         WHERE s.package_no = ? AND s.sample_no BETWEEN ? AND ?
         ORDER BY s.sample_no
        """,
        (package_no, first, last),
    )


def _check_range(meta: Registered, first: int, last: int) -> None:
    if first < 1 or last < 1:
        raise ReviewError("sample numbers start at 1")
    if first > last:
        raise ReviewError("sample range start must not exceed its end")
    if last > meta.sample_count:
        raise ReviewError(
            f"sample range ends at {last} but the package has only "
            f"{meta.sample_count} samples"
        )
    if last - first + 1 > MAX_RANGE_SAMPLES:
        raise ReviewError("sample range is too large")


def rejudge(conn: sqlite3.Connection, package_no: int, first: int, last: int,
            lower: int, upper: int) -> dict:
    """Re-judge samples ``first..last`` under a new closed interval.

    Every read happens inside the caller's transaction, so all sample values
    and supplemented originals in one response come from the same committed
    snapshot even if supplements are being written concurrently.
    """
    meta = load_package(conn, package_no)
    if not INT64_MIN <= lower <= upper <= INT64_MAX:
        raise ReviewError(
            "new pass interval must be a non-empty signed 64-bit range"
        )
    _check_range(meta, first, last)

    results = []
    counts = {"narrow": 0, "pass": 0, "wide": 0, VERDICT_UNKNOWN: 0}
    supplemented = 0
    for row in _sample_rows(conn, package_no, first, last):
        known = None if row["value"] is None else int(row["value"])
        # A lossless package's samples have lo == hi.  An approximated
        # package's samples always carry the eps-expanded band.
        item = sample_judgement(
            sample_no=int(row["sample_no"]),
            decoded=int(row["decoded"]),
            lo=int(row["possible_lo"]),
            hi=int(row["possible_hi"]),
            known=known,
            lower=lower,
            upper=upper,
        )
        if known is not None:
            supplemented += 1
        counts[item["verdict"]] += 1
        results.append(item)

    if len(results) != last - first + 1:
        raise ReviewError(
            f"package {package_no} is missing samples in {first}..{last}"
        )

    return {
        "package_no": package_no,
        "range": {"first": first, "last": last},
        "new_interval": {"lower": lower, "upper": upper},
        "old_interval": {"lower": meta.old_lower, "upper": meta.old_upper},
        "approximated": meta.approximated,
        "error_limit": meta.eps,
        "counts": counts,
        "supplemented": supplemented,
        "samples": results,
    }


def parse_supplements(entries: Iterable) -> list[tuple[int, int]]:
    """Normalise a supplement batch, rejecting malformed or contradictory input.

    Each entry must be ``{"sample_no": int, "value": int}``.  Repeating the
    same sample with the same value is allowed (an idempotent retransmission);
    the same sample with a different value is ``异常值`` and rejects the batch.
    """
    seen: dict[int, int] = {}
    ordered: list[tuple[int, int]] = []
    for position, entry in enumerate(entries, start=1):
        try:
            sample_no = int(entry["sample_no"])
            value = int(entry["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewError(
                f"supplement entry {position} needs integer sample_no and value"
            ) from exc
        if isinstance(entry.get("sample_no"), bool) or isinstance(
            entry.get("value"), bool
        ):
            raise ReviewError(f"supplement entry {position} must not use booleans")
        if sample_no in seen:
            if seen[sample_no] != value:
                raise ReviewError(
                    f"sample {sample_no} appears twice with different values "
                    f"({seen[sample_no]} and {value})"
                )
            continue
        seen[sample_no] = value
        ordered.append((sample_no, value))
    if not ordered:
        raise ReviewError("supplement batch is empty")
    return ordered


def supplement(conn: sqlite3.Connection, package_no: int,
               entries: Iterable) -> dict:
    """Record original readings for a batch of samples, atomically.

    Validation failures raise :class:`ReviewError` before any row is written,
    so the whole batch is rejected and existing originals stay untouched.  A
    value that equals the stored original is reported as ``unchanged`` and the
    request still succeeds.

    Only the samples named in the batch are read; the package is never decoded
    again.

    The caller must already hold the write transaction (``db.connection(
    write=True)``).  Serialising writers up front means two simultaneous
    batches cannot both pass validation and then race on the same sample; the
    second one is validated against the first one's committed value.
    """
    if not conn.in_transaction:
        raise ReviewError("supplement requires an open write transaction")
    meta = load_package(conn, package_no)
    batch = parse_supplements(entries)

    # Fetch just the touched samples, in a deterministic order.
    wanted = sorted({sample_no for sample_no, _ in batch})
    rows = {}
    for chunk_start in range(0, len(wanted), 500):
        chunk = wanted[chunk_start:chunk_start + 500]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"""
            SELECT s.sample_no, s.possible_lo, s.possible_hi, o.value
              FROM samples AS s
              LEFT JOIN originals AS o
                ON o.package_no = s.package_no AND o.sample_no = s.sample_no
             WHERE s.package_no = ? AND s.sample_no IN ({placeholders})
            """,
            (package_no, *chunk),
        ):
            rows[int(row["sample_no"])] = row

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    inserts = []
    for sample_no, value in batch:
        row = rows.get(sample_no)
        if row is None:
            raise ReviewError(
                f"sample {sample_no} does not exist in package {package_no} "
                f"(valid range 1..{meta.sample_count})"
            )
        lo, hi = int(row["possible_lo"]), int(row["possible_hi"])
        previous = None if row["value"] is None else int(row["value"])
        # Answering "same value again" is always a success, even if a later
        # row of the same batch turns out invalid; only the stored rows count.
        if previous is not None and previous != value:
            raise ReviewError(
                f"sample {sample_no} already has original {previous}, "
                f"refusing conflicting value {value}"
            )
        if not lo <= value <= hi:
            raise ReviewError(
                f"sample {sample_no} value {value} is outside its possible "
                f"range {lo}..{hi}"
            )
        status = "unchanged" if previous == value else "stored"
        if status == "stored":
            inserts.append((package_no, sample_no, value, now))
        results.append({
            "sample_no": sample_no,
            "value": value,
            "status": status,
            "possible": {"lo": lo, "hi": hi},
        })

    if inserts:
        conn.executemany(
            "INSERT INTO originals (package_no, sample_no, value, updated_at) "
            "VALUES (?, ?, ?, ?)",
            inserts,
        )

    return {
        "package_no": package_no,
        "count": len(batch),
        "stored": len(inserts),
        "unchanged": len(batch) - len(inserts),
        "samples": results,
    }
