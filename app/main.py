"""FastAPI handoff service for gauge-reading packages.

Endpoint summary
----------------
POST /api/packages   CSV + limits in, self-describing binary package out.
POST /api/inspect    Package (and optionally the original CSV) in, decoded
                     readings, verdicts and an integrity report out.
GET  /healthz        liveness probe.

Interactive docs (upload forms included) are served at /docs.
"""

from __future__ import annotations

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import pack, transfer

app = FastAPI(
    title="轨距读数交接 API",
    version="1.0.0",
    description=(
        "按采样顺序接收轨距 CSV（整数微米），生成自带解码信息与合格区间的"
        "二进制包。无损包超过字节上限时，允许在绝对误差限内近似；仍装不下则"
        "返回 413，绝不交付半包。"
    ),
)

PACKAGE_MEDIA_TYPE = "application/octet-stream"
PACKAGE_FILENAME = "gauge-package.tgp"
MAX_PACKAGE_BYTES = 256 * 1024 * 1024


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/healthz", summary="Health check")
def healthz() -> dict:
    return {"status": "ok"}


@app.exception_handler(transfer.InputError)
async def _input_error_handler(_request, exc: transfer.InputError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(pack.PackageTooLarge)
async def _too_large_handler(_request, exc: pack.PackageTooLarge):
    return JSONResponse(status_code=413, content={"detail": str(exc)})


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
