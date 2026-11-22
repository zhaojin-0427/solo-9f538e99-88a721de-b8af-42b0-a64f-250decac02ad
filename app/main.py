"""FastAPI handoff service for gauge-reading packages.

Endpoint summary
----------------
POST /api/packages                    CSV + limits in, self-describing package out.
POST /api/inspect                     Package (+ optional original CSV) in,
                                      decoded readings, verdicts and report out.
POST /api/review/packages             Register a .tgp, get a persistent package no.
GET  /api/review/packages/{no}        Registered package metadata.
POST /api/review/packages/{no}/rejudge
                                      Re-judge a 1-based sample range under a
                                      new closed pass interval (SQLite-backed).
POST /api/review/packages/{no}/supplements
                                      Batch-record original readings.
GET  /healthz                         liveness probe.

Interactive docs (upload forms included) are served at /docs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, StrictInt

from . import db, pack, review, transfer


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Prepare the SQLite file and schema before serving.  A failure here is
    # reported but does not block packaging-only use of the service; /healthz
    # will report the database as unavailable until it is fixed.
    try:
        db.ensure_ready()
    except db.DatabaseUnavailable:
        pass
    yield


app = FastAPI(
    title="轨距读数交接 API",
    version="1.1.0",
    lifespan=_lifespan,
    description=(
        "按采样顺序接收轨距 CSV（整数微米），生成自带解码信息与合格区间的"
        "二进制包。无损包超过字节上限时，允许在绝对误差限内近似；仍装不下则"
        "返回 413，绝不交付半包。复核时可在 /docs 登记交接包取得包号，再按"
        "包号在新合格区间下逐点复判并批量补录原值；登记与补录持久化到 SQLite。"
    ),
)

PACKAGE_MEDIA_TYPE = "application/octet-stream"
PACKAGE_FILENAME = "gauge-package.tgp"
MAX_PACKAGE_BYTES = 256 * 1024 * 1024


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", summary="Health check")
def healthz() -> JSONResponse:
    # Includes a real SELECT so an unwritable or unmounted database is visible
    # to the Docker healthcheck instead of only failing on first review request.
    status = db.health()
    if status["database"] == "ready":
        return JSONResponse(content={"status": "ok", **status})
    return JSONResponse(status_code=503,
                        content={"status": "degraded", **status})


@app.exception_handler(db.DatabaseUnavailable)
async def _database_error_handler(_request, exc: db.DatabaseUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(transfer.InputError)
async def _input_error_handler(_request, exc: transfer.InputError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(pack.PackageTooLarge)
async def _too_large_handler(_request, exc: pack.PackageTooLarge):
    return JSONResponse(status_code=413, content={"detail": str(exc)})


@app.exception_handler(review.ReviewError)
async def _review_error_handler(_request, exc: review.ReviewError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise transfer.InputError(f"upload exceeds {limit} bytes")
    return data


@app.post(
    "/api/packages",
    summary="CSV → 交接包",
    response_class=Response,
    responses={
        200: {
            "content": {PACKAGE_MEDIA_TYPE: {}},
            "description": (
                "自描述交接包。响应头 X-Package-Samples / X-Package-Bytes / "
                "X-Package-Codec / X-Package-Approximated / "
                "X-Package-Max-Error 概括包内容。"
            ),
        },
        400: {"description": "CSV 或参数不合法"},
        413: {"description": "在字节上限内无法交付完整包"},
    },
)
async def create_package(
    file: UploadFile = File(..., description="按采样顺序排列的整数微米 CSV"),
    lower: int = Form(..., description="合格闭区间下界（微米，含）"),
    upper: int = Form(..., description="合格闭区间上界（微米，含）"),
    eps: int = Form(..., description="逐点绝对误差上限（微米，0 表示必须无损）"),
    byte_limit: int = Form(..., description="包的字节上限（含头部）"),
):
    raw = await _read_upload(file, transfer.MAX_CSV_BYTES)
    transfer.check_params(lower, upper, eps, byte_limit)
    readings = transfer.parse_readings(raw)

    # deliver() only returns a package that has already been decoded and
    # checked against the originals; a failure raises before any bytes leave.
    delivery = transfer.deliver(readings.values, lower, upper, eps, byte_limit)

    headers = {
        "Content-Disposition": f'attachment; filename="{PACKAGE_FILENAME}"',
        "X-Package-Samples": str(delivery.original_len),
        "X-Package-Bytes": str(len(delivery.package)),
        "X-Package-Codec": delivery.codec,
        "X-Package-Approximated": "true" if delivery.approximated else "false",
        "X-Package-Max-Error": str(delivery.max_error),
        "X-Package-Error-Limit": str(eps),
        "X-Package-Verified": "true" if delivery.verified else "false",
    }
    return Response(
        content=delivery.package,
        media_type=PACKAGE_MEDIA_TYPE,
        headers=headers,
    )


@app.post(
    "/api/inspect",
    summary="交接包 → 读数与判定",
    responses={
        200: {"description": "按原采样顺序解码出的数值、判定与完整性报告"},
        400: {"description": "包损坏、格式不支持或与原值核对不一致"},
    },
)
async def inspect_package(
    file: UploadFile = File(..., description="交接包（.tgp）"),
    original: UploadFile | None = File(
        None,
        description="可选：原始 CSV。提供时逐点核对误差上限与判定一致性",
    ),
):
    blob = await _read_upload(file, MAX_PACKAGE_BYTES)
    try:
        decoded = pack.decode_package(blob)
    except (ValueError, pack.PackError) as exc:
        raise transfer.InputError(f"package failed to decode: {exc}") from exc

    report = {
        "count": decoded["count"],
        "bytes": decoded["bytes"],
        "codec": decoded["codec"],
        "approximated": decoded["approximated"],
        "pass_interval": {"lower": decoded["lower"], "upper": decoded["upper"]},
        "error_limit": decoded["eps"],
        "values": decoded["values"],
        "verdicts": [
            pack.verdict(v, decoded["lower"], decoded["upper"])
            for v in decoded["values"]
        ],
        "verdict_labels": [
            pack.VERDICT_LABELS[
                pack.verdict(v, decoded["lower"], decoded["upper"])
            ]
            for v in decoded["values"]
        ],
        "checked_against_original": False,
    }

    if original is not None:
        raw = await _read_upload(original, transfer.MAX_CSV_BYTES)
        readings = transfer.parse_readings(raw)
        # Raises InputError (400) if an error bound or a verdict differs.
        summary = transfer.verify_package(
            blob, readings.values, decoded["lower"], decoded["upper"],
            decoded["eps"],
        )
        report["checked_against_original"] = True
        report["max_error"] = summary["max_error"]

    return JSONResponse(content=report)


# --------------------------------------------------------------------------
# Review workflow: register a .tgp, re-judge ranges, supplement originals.
# All state lives in SQLite (see app/db.py and app/review.py).
# --------------------------------------------------------------------------

class RejudgeRequest(BaseModel):
    first_sample: StrictInt
    last_sample: StrictInt
    lower: StrictInt
    upper: StrictInt


class SupplementEntry(BaseModel):
    sample_no: StrictInt
    value: StrictInt


class SupplementRequest(BaseModel):
    samples: List[SupplementEntry]


def _json_error(detail: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": detail})


@app.exception_handler(RequestValidationError)
async def _request_validation_handler(request, exc: RequestValidationError):
    # Keep the review API's error contract simple: every rejected request is a
    # 400 with a JSON ``detail``.  Other endpoints keep FastAPI's default 422.
    if request.url.path.startswith("/api/review/"):
        return _json_error(
            "; ".join(
                f"{'.'.join(str(part) for part in err.get('loc', ()))}: "
                f"{err.get('msg', 'invalid')}"
                for err in exc.errors()
            ) or "invalid request"
        )
    return await request_validation_exception_handler(request, exc)


@app.post(
    "/api/review/packages",
    summary="登记交接包，取得包号",
    responses={
        200: {"description": "包已持久化，返回包号与包内元数据"},
        400: {"description": "包损坏或格式不支持"},
    },
)
async def register_review_package(
    file: UploadFile = File(..., description="交接包（.tgp）"),
):
    blob = await _read_upload(file, MAX_PACKAGE_BYTES)
    try:
        decoded = pack.decode_package(blob)
    except (ValueError, pack.PackError) as exc:
        raise transfer.InputError(f"package failed to decode: {exc}") from exc

    with db.connection(write=True) as conn:
        meta = review.register_package(conn, blob, decoded)

    return {
        "package_no": meta.package_no,
        "sample_count": meta.sample_count,
        "old_interval": {"lower": meta.old_lower, "upper": meta.old_upper},
        "error_limit": meta.eps,
        "approximated": meta.approximated,
    }


@app.get(
    "/api/review/packages/{package_no}",
    summary="已登记包的信息",
)
def get_review_package(package_no: int):
    with db.connection() as conn:
        meta = review.load_package(conn, package_no)
        row = conn.execute(
            "SELECT registered_at, sha256, codec, byte_size, "
            "(SELECT COUNT(*) FROM originals WHERE package_no = ?) AS supplements "
            "FROM packages WHERE package_no = ?",
            (package_no, package_no),
        ).fetchone()
    return {
        "package_no": meta.package_no,
        "sample_count": meta.sample_count,
        "old_interval": {"lower": meta.old_lower, "upper": meta.old_upper},
        "error_limit": meta.eps,
        "approximated": meta.approximated,
        "registered_at": row["registered_at"],
        "sha256": row["sha256"],
        "codec": row["codec"],
        "bytes": row["byte_size"],
        "supplemented_samples": row["supplements"],
    }


@app.post(
    "/api/review/packages/{package_no}/rejudge",
    summary="按新合格闭区间复判采样号范围",
    responses={
        200: {"description": "逐点返回 偏窄 / 合格 / 偏宽 / 待查"},
        400: {"description": "包号或采样号范围不合法"},
    },
)
def rejudge_range(package_no: int, request: RejudgeRequest):
    # All sample rows and supplemented originals for this range are read in
    # one transaction.  Under WAL that is a single committed snapshot even if
    # a supplement batch commits while this query is running.
    with db.connection() as conn:
        return review.rejudge(
            conn, package_no, request.first_sample, request.last_sample,
            request.lower, request.upper,
        )


@app.post(
    "/api/review/packages/{package_no}/supplements",
    summary="批量补录采样号与原值",
    responses={
        200: {"description": "整批写入成功（含同值重传）"},
        400: {"description": "整批退回：采样号无效、超出可能范围或同点异值"},
    },
)
def supplement_samples(package_no: int, request: SupplementRequest):
    entries = [entry.model_dump() for entry in request.samples]
    with db.connection(write=True) as conn:
        return review.supplement(conn, package_no, entries)
