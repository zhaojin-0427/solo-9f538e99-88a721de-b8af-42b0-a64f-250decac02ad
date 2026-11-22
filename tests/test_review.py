"""Review workflow: register packages, re-judge ranges, supplement originals."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, pack, review
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GAUGE_DB_PATH", str(tmp_path / "review.db"))
    with TestClient(app) as test_client:
        yield test_client


def package_bytes(client, values, lower=1000, upper=2000, eps=0,
                  byte_limit=10**9):
    csv = "\n".join(str(v) for v in values) + "\n"
    response = client.post(
        "/api/packages",
        files={"file": ("gauge.csv", csv.encode(), "text/csv")},
        data={"lower": lower, "upper": upper, "eps": eps,
              "byte_limit": byte_limit},
    )
    assert response.status_code == 200, response.text
    return response.content, response.headers


def register(client, blob):
    response = client.post(
        "/api/review/packages",
        files={"file": ("pkg.tgp", blob, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def rejudge(client, package_no, first, last, lower, upper):
    return client.post(
        f"/api/review/packages/{package_no}/rejudge",
        json={"first_sample": first, "last_sample": last,
              "lower": lower, "upper": upper},
    )


def supplement(client, package_no, samples):
    return client.post(
        f"/api/review/packages/{package_no}/supplements",
        json={"samples": samples},
    )


def verdicts(body):
    return [sample["verdict"] for sample in body["samples"]]


# 20 000 alternating readings do not fit losslessly in 100 bytes; the service
# approximates them into one held value with per-sample error <= 600, so every
# possible range is 600 wide.
APPROX_VALUES = [1500000 + (500 if i % 2 else -500) for i in range(20000)]


def approx_package(client):
    blob, headers = package_bytes(
        client, APPROX_VALUES, lower=1400000, upper=1600000,
        eps=600, byte_limit=100,
    )
    assert headers["x-package-approximated"] == "true"
    body = register(client, blob)
    assert body["approximated"] is True
    return blob, body["package_no"]

# --- registration ---------------------------------------------------------

def test_register_lossless_returns_package_number(client):
    blob, _ = package_bytes(client, [900, 1000, 2000, 2001])
    body = register(client, blob)
    assert body["package_no"] >= 1
    assert body["sample_count"] == 4
    assert body["old_interval"] == {"lower": 1000, "upper": 2000}
    assert body["approximated"] is False
    # A second registration gets a distinct, increasing number.
    again = register(client, blob)
    assert again["package_no"] == body["package_no"] + 1


def test_register_rejects_broken_package(client):
    blob, _ = package_bytes(client, [1, 2, 3], lower=0, upper=10)
    broken = bytearray(blob)
    broken[-1] ^= 0xFF
    response = client.post(
        "/api/review/packages",
        files={"file": ("pkg.tgp", bytes(broken), "application/octet-stream")},
    )
    assert response.status_code == 400


def test_metadata_endpoint(client):
    blob, _ = package_bytes(client, [1000, 1001])
    package_no = register(client, blob)["package_no"]
    response = client.get(f"/api/review/packages/{package_no}")
    assert response.status_code == 200
    body = response.json()
    assert body["sample_count"] == 2
    assert body["supplemented_samples"] == 0
    assert len(body["sha256"]) == 64
    assert client.get("/api/review/packages/9999").status_code == 400


# --- new / old boundary self-check ---------------------------------------

def test_lossless_old_and_new_boundaries(client):
    # Old interval [1000, 2000]: 999 narrow, 1000/2000 pass, 2001 wide.
    values = [999, 1000, 1500, 2000, 2001]
    blob, _ = package_bytes(client, values)
    package_no = register(client, blob)["package_no"]

    same = rejudge(client, package_no, 1, 5, 1000, 2000)
    assert same.status_code == 200
    assert verdicts(same.json()) == ["narrow", "pass", "pass", "pass", "wide"]
    assert same.json()["old_interval"] == {"lower": 1000, "upper": 2000}
    assert same.json()["counts"] == {"narrow": 1, "pass": 3, "wide": 1,
                                     "unknown": 0}

    # Tighten to [1200, 1800]: inclusive endpoints still pass.
    body = rejudge(client, package_no, 1, 5, 1200, 1800).json()
    assert verdicts(body) == ["narrow", "narrow", "pass", "wide", "wide"]
    assert [s["label"] for s in body["samples"][:2]] == ["偏窄", "偏窄"]

    # Shift to a single-point interval: [1200, 1200].
    body = rejudge(client, package_no, 2, 4, 1200, 1200).json()
    assert verdicts(body) == ["narrow", "wide", "wide"]

    # Exactly on the new endpoints.
    body = rejudge(client, package_no, 1, 5, 1500, 2000).json()
    assert verdicts(body) == ["narrow", "narrow", "pass", "pass", "wide"]


def test_rejudge_range_validation(client):
    blob, _ = package_bytes(client, [1, 2, 3], lower=0, upper=10)
    package_no = register(client, blob)["package_no"]
    assert rejudge(client, package_no, 0, 2, 0, 10).status_code == 400
    assert rejudge(client, package_no, 2, 1, 0, 10).status_code == 400
    assert rejudge(client, package_no, 1, 4, 0, 10).status_code == 400
    assert rejudge(client, package_no, 1, 3, 10, 0).status_code == 400
    assert rejudge(client, 9999, 1, 3, 0, 10).status_code == 400
    # Sample numbers are 1-based; a 0-based client is rejected outright.
    assert rejudge(client, package_no, 1, 3, 0, 10).status_code == 200


def test_approximated_unknown_then_supplement(client):
    blob, package_no = approx_package(client)

    # Under the old interval every sample was far inside.  Tightening to a
    # narrow band around 1,500,000 makes the held value's possible range
    # straddle the new interval, so the answer is not certain.
    body = rejudge(client, package_no, 1, 4, 1499800, 1500200).json()
    assert verdicts(body) == ["unknown"] * 4
    assert body["counts"]["unknown"] == 4
    labels = {s["label"] for s in body["samples"]}
    assert labels == {"待查"}
    for sample in body["samples"]:
        assert sample["certain"] is False
        assert set(sample["candidates"]) == {"narrow", "pass", "wide"}

    # Supplementing the true original resolves that sample deterministically.
    sample_no = 2
    original = APPROX_VALUES[sample_no - 1]
    response = supplement(client, package_no, [
        {"sample_no": sample_no, "value": original},
    ])
    assert response.status_code == 200, response.text
    assert response.json()["stored"] == 1

    after = rejudge(client, package_no, 1, 4, 1499800, 1500200).json()
    resolved = after["samples"][1]
    assert resolved["sample_no"] == sample_no
    assert resolved["verdict"] == pack.verdict(original, 1499800, 1500200)
    assert resolved["source"] == "supplement"
    assert resolved["value"] == original
    assert resolved["certain"] is True
    assert after["supplemented"] == 1
    assert after["counts"]["unknown"] == 3


def test_approximated_certain_when_whole_range_agrees(client):
    # Every possible original stays inside the old interval, so even the
    # approximated package gives one verdict across its whole possible range.
    _, package_no = approx_package(client)
    body = rejudge(client, package_no, 1, 64, 1400000, 1600000).json()
    assert set(verdicts(body)) == {"pass"}
    assert all(sample["certain"] for sample in body["samples"])
    assert all(sample["source"] == "decoded" for sample in body["samples"])


def test_approximated_partially_overlapping_interval(client):
    # New lower just below the held value: possible originals span pass/wide
    # only, never narrow.
    _, package_no = approx_package(client)
    body = rejudge(client, package_no, 1, 1, 1499900, 1600000).json()
    sample = body["samples"][0]
    # Held value 1499900 lies on the new lower edge; anything above is pass,
    # anything below is narrow.
    assert set(sample["candidates"]) == {"narrow", "pass"}


# --- supplements ----------------------------------------------------------

def test_supplement_lossless_accepts_only_exact_value(client):
    blob, _ = package_bytes(client, [1000, 1001, 1002])
    package_no = register(client, blob)["package_no"]
    ok = supplement(client, package_no, [{"sample_no": 2, "value": 1001}])
    assert ok.status_code == 200
    assert ok.json()["stored"] == 1
    # A lossless possible range is a single point: any other value is refused,
    # whether or not the sample already carries a stored original.
    bad = supplement(client, package_no, [{"sample_no": 3, "value": 1002 + 1}])
    assert bad.status_code == 400
    assert "possible range 1002..1002" in bad.json()["detail"]
    conflict = supplement(
        client, package_no, [{"sample_no": 2, "value": 1002}]
    )
    assert conflict.status_code == 400
    assert "conflicting" in conflict.json()["detail"]


def test_supplement_batch_atomic_on_bad_sample_number(client):
    blob, _ = package_bytes(client, [1000, 1001, 1002])
    package_no = register(client, blob)["package_no"]
    response = supplement(client, package_no, [
        {"sample_no": 1, "value": 1000},
        {"sample_no": 9, "value": 1000},
    ])
    assert response.status_code == 400
    # Even the valid first entry was not persisted.
    meta = client.get(f"/api/review/packages/{package_no}").json()
    assert meta["supplemented_samples"] == 0


def test_supplement_batch_atomic_on_out_of_range_value(client):
    blob, package_no = approx_package(client)
    decoded = pack.decode_package(blob)["values"]
    response = supplement(client, package_no, [
        {"sample_no": 1, "value": decoded[0]},
        {"sample_no": 2, "value": 9_000_000},
    ])
    assert response.status_code == 400
    assert "possible range" in response.json()["detail"]
    assert client.get(
        f"/api/review/packages/{package_no}"
    ).json()["supplemented_samples"] == 0


def test_supplement_same_value_retransmission_succeeds(client):
    blob, package_no = approx_package(client)
    decoded = pack.decode_package(blob)["values"]
    first = supplement(client, package_no, [{"sample_no": 3, "value": decoded[2]}])
    assert first.status_code == 200
    assert first.json()["stored"] == 1
    repeat = supplement(client, package_no, [
        {"sample_no": 3, "value": decoded[2]},
        {"sample_no": 3, "value": decoded[2]},
    ])
    assert repeat.status_code == 200
    assert repeat.json()["stored"] == 0
    assert repeat.json()["unchanged"] == 1


def test_supplement_conflicting_value_rejects_whole_batch(client):
    _, package_no = approx_package(client)
    blob, _ = package_bytes(
        client, APPROX_VALUES, lower=1400000, upper=1600000,
        eps=600, byte_limit=100,
    )
    decoded = pack.decode_package(blob)["values"]
    assert supplement(
        client, package_no, [{"sample_no": 4, "value": decoded[3]}]
    ).status_code == 200
    # decoded[3] + 1 is still inside the 600-wide possible range.
    response = supplement(client, package_no, [
        {"sample_no": 5, "value": decoded[4]},
        {"sample_no": 4, "value": decoded[3] + 1},
    ])
    assert response.status_code == 400
    assert "conflicting" in response.json()["detail"]
    # Sample 5 was rolled back with the batch.
    assert client.get(
        f"/api/review/packages/{package_no}"
    ).json()["supplemented_samples"] == 1


def test_supplement_duplicate_in_one_batch_with_different_values(client):
    blob, _ = package_bytes(client, [1000, 1001, 1002])
    package_no = register(client, blob)["package_no"]
    response = supplement(client, package_no, [
        {"sample_no": 1, "value": 1000},
        {"sample_no": 1, "value": 0},
    ])
    assert response.status_code == 400


def test_supplemented_original_survives_interval_change(client):
    _, package_no = approx_package(client)
    sample_no = 7
    original = APPROX_VALUES[sample_no - 1]
    assert supplement(
        client, package_no, [{"sample_no": sample_no, "value": original}]
    ).status_code == 200

    first = rejudge(client, package_no, sample_no, sample_no, 1400000, 1600000)
    assert first.json()["samples"][0]["verdict"] == "pass"
    # A later interval change re-judges the stored original, not the package
    # value, so the supplement keeps working.
    second = rejudge(client, package_no, sample_no, sample_no, 1500100, 1600000)
    assert second.json()["samples"][0]["value"] == original
    assert second.json()["samples"][0]["verdict"] == pack.verdict(
        original, 1500100, 1600000
    )


def test_supplement_reads_only_touched_samples(client, monkeypatch):
    """Supplement must not need to decode the package again."""
    _, package_no = approx_package(client)

    def forbidden(_blob):
        raise AssertionError("supplement decoded the whole package")

    monkeypatch.setattr(pack, "decode_package", forbidden)
    response = supplement(
        client, package_no, [{"sample_no": 11, "value": APPROX_VALUES[10]}]
    )
    assert response.status_code == 200, response.text
    assert response.json()["stored"] == 1


# --- persistence and snapshots -------------------------------------------

def test_state_survives_new_connection(client, tmp_path):
    blob, _ = package_bytes(client, [1000, 1001])
    package_no = register(client, blob)["package_no"]
    # Read through a brand-new connection to the same file: the package and
    # its samples are genuinely on disk, not in a process-local cache.
    conn = sqlite3.connect(tmp_path / "review.db")
    try:
        with db.connection() as held:
            meta = review.load_package(held, package_no)
        assert meta.sample_count == 2
        count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_rejudge_uses_one_snapshot_while_supplements_commit(client):
    """A whole-range read must not mix pre- and post-supplement values."""
    _, package_no = approx_package(client)
    before = rejudge(client, package_no, 1, 8, 1499800, 1500200).json()
    assert before["supplemented"] == 0
    assert verdicts(before) == ["unknown"] * 8

    supplement(client, package_no, [
        {"sample_no": no, "value": APPROX_VALUES[no - 1]} for no in range(1, 9)
    ])

    after = rejudge(client, package_no, 1, 8, 1499800, 1500200).json()
    assert after["supplemented"] == 8
    assert all(s["source"] == "supplement" for s in after["samples"])
    assert all(s["certain"] for s in after["samples"])
