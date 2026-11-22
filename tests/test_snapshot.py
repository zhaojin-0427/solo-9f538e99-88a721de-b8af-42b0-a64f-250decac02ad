"""Cross-connection snapshot test: a whole-range read sees one committed state.

The in-process API test covers the transaction contract end to end.  This test
drives two real SQLite connections so the guarantee is exercised the way the
review terminal uses it: one request re-judges a long range while another
request keeps committing supplements.
"""

import os
import tempfile
import threading
import unittest

from app import db, pack, review, transfer


def build_approximated():
    values = [1500000 + (500 if i % 2 else -500) for i in range(20000)]
    delivery = transfer.deliver(values, 1400000, 1600000, 600, 100)
    assert delivery.approximated
    decoded = pack.decode_package(delivery.package)
    return values, delivery.package, decoded


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.environ.get("GAUGE_DB_PATH")
        self.db_file = os.path.join(self.tmp.name, "review.db")
        os.environ["GAUGE_DB_PATH"] = self.db_file
        self.values, self.blob, self.decoded = build_approximated()
        with db.connection(write=True) as conn:
            self.meta = review.register_package(conn, self.blob, self.decoded)

    def tearDown(self):
        if self.env is None:
            os.environ.pop("GAUGE_DB_PATH", None)
        else:
            os.environ["GAUGE_DB_PATH"] = self.env
        self.tmp.cleanup()

    def test_reader_keeps_one_snapshot_while_writer_commits(self):
        package_no = self.meta.package_no
        started = threading.Event()
        committed = threading.Event()
        failures = []

        def writer():
            started.set()
            try:
                # Commit supplements for every sample in the read range.
                with db.connection(write=True) as conn:
                    review.supplement(conn, package_no, [
                        {"sample_no": no, "value": self.values[no - 1]}
                        for no in range(1, 65)
                    ])
            except Exception as exc:  # pragma: no cover - surfaced below
                failures.append(exc)
            finally:
                committed.set()

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(started.wait(5))

        # Open the reader before the writer commits, then pause so the writer
        # finishes in between.  A non-snapshot reader would mix the two states.
        conn = db._connect(self.db_file)
        try:
            conn.execute("BEGIN")
            first = conn.execute(
                "SELECT COUNT(*) FROM originals"
            ).fetchone()[0]
            self.assertEqual(first, 0)
            self.assertTrue(committed.wait(10))
            thread.join(10)
            self.assertFalse(thread.is_alive())
            second = conn.execute(
                "SELECT COUNT(*) FROM originals"
            ).fetchone()[0]
            self.assertEqual(second, 0)  # still the old snapshot
            conn.execute("COMMIT")
        finally:
            conn.close()

        with db.connection() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM originals"
            ).fetchone()[0]
        self.assertEqual(after, 64)

    def test_supplement_failure_leaves_existing_values(self):
        package_no = self.meta.package_no
        with db.connection(write=True) as conn:
            review.supplement(conn, package_no, [
                {"sample_no": 1, "value": self.values[0]},
            ])
        with self.assertRaises(review.ReviewError):
            with db.connection(write=True) as conn:
                review.supplement(conn, package_no, [
                    {"sample_no": 2, "value": self.values[1]},
                    {"sample_no": 3, "value": 2**31 - 1},
                ])
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT sample_no, value FROM originals ORDER BY sample_no"
            ).fetchall()
        self.assertEqual([(r["sample_no"], r["value"]) for r in rows],
                         [(1, self.values[0])])


if __name__ == "__main__":
    unittest.main()
