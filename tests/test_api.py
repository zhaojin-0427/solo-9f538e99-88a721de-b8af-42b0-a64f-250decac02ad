from fastapi.testclient import TestClient

from app import pack
from app.main import app

client = TestClient(app)


def upload(values, lower=1434000, upper=1436000, eps=0, byte_limit=1_000_000):
    csv = "\n".join(str(v) for v in values) + "\n"
    return client.post(
        "/api/packages",
        files={"file": ("gauge.csv", csv.encode(), "text/csv")},
        data={
            "lower": lower,
            "upper": upper,
            "eps": eps,
            "byte_limit": byte_limit,
        },
    )


def test_health_and_docs():
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_round_trip_via_api():
    values = [1435000 + (i % 11) - 5 for i in range(200)]
    response = upload(values)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-package-verified"] == "true"
    assert response.headers["x-package-approximated"] == "false"

    decoded = pack.decode_package(response.content)
    assert decoded["values"] == values
    assert (decoded["lower"], decoded["upper"]) == (1434000, 1436000)

    report = client.post(
        "/api/inspect",
        files={
            "file": ("pkg.tgp", response.content, "application/octet-stream"),
            "original": (
                "gauge.csv",
                ("\n".join(map(str, values)) + "\n").encode(),
                "text/csv",
            ),
        },
    )
    assert report.status_code == 200
    body = report.json()
    assert body["values"] == values
    assert body["checked_against_original"] is True
    assert body["max_error"] == 0
    assert body["pass_interval"] == {"lower": 1434000, "upper": 1436000}


def test_verdicts_use_inclusive_interval():
    values = [1434000, 1436000, 1433999, 1436001]
    response = upload(values, lower=1434000, upper=1436000)
    report = client.post(
        "/api/inspect",
        files={"file": ("pkg.tgp", response.content, "application/octet-stream")},
    )
    body = report.json()
    assert body["verdicts"] == ["pass", "pass", "narrow", "wide"]
    assert body["verdict_labels"] == ["合格", "合格", "偏窄", "偏宽"]


def test_byte_limit_boundary():
    values = [1435000 + (i % 7) for i in range(500)]
    first = upload(values, byte_limit=10**9)
    size = int(first.headers["x-package-bytes"])
    assert len(first.content) == size

    at_limit = upload(values, byte_limit=size)
    assert at_limit.status_code == 200
    assert at_limit.headers["x-package-approximated"] == "false"

    too_small = upload(values, byte_limit=size - 1)
    assert too_small.status_code == 413
    assert too_small.content  # JSON error body, no package bytes


def test_approximation_path_via_api():
    values = [1500000 + (500 if i % 2 else -500) for i in range(20000)]
    response = upload(values, lower=1400000, upper=1600000, eps=600,
                      byte_limit=100)
    assert response.status_code == 200
    assert response.headers["x-package-approximated"] == "true"
    assert int(response.headers["x-package-max-error"]) <= 600
    assert len(response.content) <= 100

    report = client.post(
        "/api/inspect",
        files={
            "file": ("pkg.tgp", response.content, "application/octet-stream"),
            "original": (
                "gauge.csv",
                ("\n".join(map(str, values)) + "\n").encode(),
                "text/csv",
            ),
        },
    )
    assert report.status_code == 200
    body = report.json()
    assert body["checked_against_original"] is True
    assert body["approximated"] is True
    assert body["max_error"] <= 600
    assert len(body["values"]) == len(values)


def test_impossible_budget_reports_failure():
    values = list(range(1000))
    response = upload(values, lower=0, upper=2000, eps=0, byte_limit=60)
    assert response.status_code == 413
    assert "detail" in response.json()


def test_bad_inputs():
    values = [1, 2, 3]
    assert upload(values, lower=10, upper=0).status_code == 400
    assert upload(values, eps=-1).status_code == 400
    assert upload(values, byte_limit=10).status_code == 400
    bad_csv = client.post(
        "/api/packages",
        files={"file": ("x.csv", b"1\n2.5\n", "text/csv")},
        data={"lower": 0, "upper": 10, "eps": 0, "byte_limit": 1000},
    )
    assert bad_csv.status_code == 400


def test_corrupt_package_rejected():
    values = [1, 2, 3]
    good = upload(values, lower=0, upper=10)
    broken = bytearray(good.content)
    broken[-1] ^= 0xFF
    report = client.post(
        "/api/inspect",
        files={"file": ("pkg.tgp", bytes(broken), "application/octet-stream")},
    )
    assert report.status_code == 400
