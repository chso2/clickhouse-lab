#!/usr/bin/env python3
"""Measure A7-1 on an explicitly selected, single-shard test server.

Uses only the Python standard library. Creates a unique a7_capacity_* database;
never changes the existing shop_a7_1 or shop_benchmark databases.
"""

import argparse
import collections
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


KINDS = ("VIEW", "CART", "CLICK", "PURCHASE", "NOTIFY")
TABLES = ("view", "cart", "click", "purchase", "notification")
COUNT_COLUMNS = ("view_count", "cart_count", "click_count", "purchase_count", "notify_count")
HERE = Path(__file__).resolve().parent


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    def percentile(p):
        return round(values[max(0, math.ceil(len(values) * p) - 1)] * 1000, 3)
    return {"count": len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95),
            "p99_ms": percentile(.99), "max_ms": round(values[-1] * 1000, 3)}


class Client:
    def __init__(self, endpoint, user, password_env, timeout, max_threads):
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("Use an HTTP(S) endpoint without embedded credentials")
        self.endpoint = endpoint.rstrip("/") + "/"
        self.timeout = timeout
        self.headers = {"X-ClickHouse-User": user}
        if password_env:
            self.headers["X-ClickHouse-Key"] = os.environ[password_env]
        self.max_threads = max_threads

    def execute(self, sql, query_id=None, payload=None):
        params = {"max_threads": self.max_threads, "max_execution_time": 60,
                  "max_memory_usage": 2_000_000_000, "wait_end_of_query": 1,
                  "log_queries": 1, "log_queries_probability": 1,
                  "log_queries_min_query_duration_ms": 0}
        if query_id:
            params["query_id"] = query_id
        if payload is None:
            body = sql.encode()
        else:
            params["query"] = sql
            body = payload
        request = urllib.request.Request(self.endpoint + "?" + urllib.parse.urlencode(params),
                                         data=body, headers=self.headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode()
        except urllib.error.HTTPError as error:
            # SQL/body diagnostics contain synthetic fixture data, never credentials.
            detail = error.read().decode(errors="replace")[:1500]
            raise RuntimeError(f"ClickHouse HTTP {error.code}: {detail}") from None

    def rows(self, sql, query_id=None):
        response = self.execute(sql + " FORMAT JSONEachRow", query_id)
        return [json.loads(line) for line in response.splitlines() if line]


class Benchmark:
    def __init__(self, args):
        self.args = args
        self.client = Client(args.endpoint, args.user, args.password_env, args.timeout, args.max_threads)
        self.token = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.db = "a7_capacity_" + self.token
        self.prefix = self.db + "_"
        self.expected = collections.Counter()
        self.raw_rows = 0
        self.ordinal = 0
        self.report = {"database": self.db, "scope": "synthetic single-shard A7-1; not an AWS capacity guarantee",
                       "benchmark_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "profile_label": args.profile_label, "parameters": {
                           key: value for key, value in vars(args).items()
                           if key not in ("endpoint", "user", "password_env")}, "cases": []}
        self.output = Path(args.output) / self.db
        self.output.mkdir(parents=True, exist_ok=False)

    def save(self):
        (self.output / "report.json").write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + "\n")

    def setup(self):
        self.report["server"] = self.client.rows(
            "SELECT version() AS version, hostName() AS hostname, timezone() AS timezone")
        cluster = self.client.rows("SELECT shard_num, replica_num, host_name FROM system.clusters WHERE cluster='cluster1'")
        if len(cluster) != 1:
            raise RuntimeError("This benchmark requires cluster1 with exactly one shard and one replica; use an isolated test deployment")
        self.report["cluster"] = cluster
        self.report["settings"] = self.client.rows(
            "SELECT name, value FROM system.settings WHERE name IN "
            "('max_threads','max_memory_usage','insert_quorum','async_insert')")
        self.report["initial_metrics"] = self.telemetry()
        if self.client.rows(f"SELECT name FROM system.databases WHERE name='{self.db}'"):
            raise RuntimeError("Refusing to reuse an existing database")
        schema = (HERE / "schema.sql").read_text().replace("shop_a7_1", self.db)
        schema = schema.replace(" ON CLUSTER 'cluster1'", "")
        # Send statements separately so the HTTP interface does not need multiquery.
        for statement in schema.split(";"):
            if statement.strip():
                self.client.execute(statement)
        if self.args.store_raw:
            common = HERE.parent.parent / "common" / "schema.sql"
            raw_schema = common.read_text().replace("shop_benchmark", self.db)
            raw_schema = raw_schema.replace(" ON CLUSTER 'cluster1'", "")
            for statement in raw_schema.split(";"):
                if statement.strip():
                    self.client.execute(statement)
        self.save()

    def fixture(self, start, count):
        grouped = [[] for _ in KINDS]
        raw = []
        fixed_received = "2026-09-28 12:00:00.000"
        epoch = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
        for ordinal in range(start, start + count):
            # Every eleventh message is a distinct-message duplicate of its predecessor.
            canonical = ordinal - 1 if ordinal % 11 == 10 else ordinal
            journey = canonical // 5
            digest = hashlib.sha256(str(journey).encode()).hexdigest()
            placement_hash = int(digest[:16], 16)
            product = (1 if placement_hash % 100 < self.args.hot_percent
                       else 2 + placement_hash % (self.args.products - 1))
            index = canonical % 5
            occurred = epoch + datetime.timedelta(seconds=journey % (90 * 86400))
            if canonical != ordinal:
                occurred -= datetime.timedelta(seconds=60)
            row = {"message_id": hashlib.sha256(f"message-{ordinal}".encode()).hexdigest()[:32],
                   "mall_id": product % 10, "store_id": product % 100,
                   "product_id": product, "journey_id": digest[:32],
                   "customer_group_ids": [product % 20], "source_kind": "capacity",
                   "occurred_at": occurred.strftime("%Y-%m-%d %H:%M:%S.000"),
                   "received_at": fixed_received}
            raw.append(dict(row, event_kind=KINDS[index]))
            grouped[index].append((ordinal, canonical, row))
        return grouped, raw

    @staticmethod
    def payload(rows):
        return ("\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + "\n").encode()

    def insert(self, start, count, case, check_existing):
        grouped, raw = self.fixture(start, count)
        started = time.perf_counter()
        if self.args.store_raw:
            self.client.execute(f"INSERT INTO {self.db}.shopping_events_local FORMAT JSONEachRow",
                                self.prefix + case + f"_raw_{start}", self.payload(raw))
            self.raw_rows += count
        check_seconds = 0.0
        for index, entries in enumerate(grouped):
            if not entries:
                continue
            existing = set()
            if check_existing:
                keys = {(row["product_id"], row["journey_id"]) for _, _, row in entries}
                tuples = ",".join(f"({product},'{journey}')" for product, journey in sorted(keys))
                lookup_start = time.perf_counter()
                found = self.client.rows(
                    f"SELECT product_id, journey_id FROM {self.db}.{TABLES[index]}_events_local "
                    f"WHERE (product_id,journey_id) IN ({tuples}) GROUP BY product_id,journey_id",
                    self.prefix + case + f"_lookup_{index}_{start}")
                check_seconds += time.perf_counter() - lookup_start
                existing = {(int(row["product_id"]), row["journey_id"]) for row in found}
            rows = []
            for ordinal, canonical, row in entries:
                key = (row["product_id"], row["journey_id"])
                first = key not in existing if check_existing else ordinal == canonical
                existing.add(key)
                rows.append(dict(row, count_delta=int(first)))
            self.client.execute(f"INSERT INTO {self.db}.{TABLES[index]}_events_local FORMAT JSONEachRow",
                                self.prefix + case + f"_insert_{index}_{start}", self.payload(rows))
            # Independent oracle uses generator identities, not the lookup's returned delta.
            for ordinal, canonical, row in entries:
                if ordinal == canonical:
                    self.expected[(row["product_id"], index)] += 1
        return time.perf_counter() - started, check_seconds

    def summary_query(self, case, serial):
        product = 1 if serial % 2 == 0 else 2 + serial % (self.args.products - 1)
        return self.client.execute(
            f"SELECT * FROM {self.db}.cumulative_summary_by_product(product_id={product}) FORMAT JSONEachRow",
            self.prefix + case + f"_read_{serial}")

    def telemetry(self):
        return {"at": time.time(), "metrics": self.client.rows(
            "SELECT metric,value FROM system.asynchronous_metrics "
            "WHERE metric LIKE 'CGroup%' OR metric IN ('MemoryResident','OSMemoryAvailable','LoadAverage1')"),
                "parts": self.client.rows(
                    f"SELECT count() AS parts,sum(bytes_on_disk) AS bytes_on_disk,"
                    f"sum(data_compressed_bytes) AS compressed_bytes,sum(rows) AS physical_rows "
                    f"FROM system.parts WHERE active AND database='{self.db}'"),
                "merges": self.client.rows(
                    f"SELECT count() AS merges,sum(memory_usage) AS memory_bytes "
                    f"FROM system.merges WHERE database='{self.db}'"),
                "replicas": self.client.rows(
                    f"SELECT sum(queue_size) AS queue_size,max(absolute_delay) AS delay_seconds "
                    f"FROM system.replicas WHERE database='{self.db}'")}

    def validate(self):
        columns = ",".join(f"sum({column}) AS {column}" for column in COUNT_COLUMNS)
        actual = self.client.rows(f"SELECT product_id,{columns} FROM {self.db}.cumulative_summary GROUP BY product_id")
        actual_counts = collections.Counter()
        for row in actual:
            for index, column in enumerate(COUNT_COLUMNS):
                actual_counts[(int(row["product_id"]), index)] = int(row[column])
        mismatches = [{"product": key[0], "event": KINDS[key[1]], "expected": self.expected[key],
                       "actual": actual_counts[key]} for key in self.expected.keys() | actual_counts.keys()
                      if self.expected[key] != actual_counts[key]]
        finals = []
        for index, table in enumerate(TABLES):
            count = int(self.client.rows(f"SELECT count() AS n FROM {self.db}.{table}_events_local FINAL")[0]["n"])
            expected = sum(value for (product, kind), value in self.expected.items() if kind == index)
            finals.append({"event": KINDS[index], "actual": count, "expected": expected, "passed": count == expected})
        raw_count = None
        if self.args.store_raw:
            raw_count = int(self.client.rows(f"SELECT count() AS n FROM {self.db}.shopping_events_local")[0]["n"])
        return {"passed": not mismatches and all(item["passed"] for item in finals)
                and (raw_count is None or raw_count == self.raw_rows), "summary_mismatches": mismatches[:20],
                "final_counts": finals, "raw_rows": raw_count, "expected_raw_rows": self.raw_rows}

    def phase(self, rate, read_qps):
        name = f"r{rate}_q{read_qps}"
        stop = threading.Event()
        lock = threading.Lock()
        permits = threading.BoundedSemaphore(self.args.read_workers)
        reads, errors, samples = [], [], []
        read_stats = {"offered": 0, "dropped": 0}
        started = time.perf_counter()
        deadline = started + self.args.duration

        def read(serial):
            tick = time.perf_counter()
            try:
                self.summary_query(name, serial)
                with lock:
                    reads.append(time.perf_counter() - tick)
            except Exception as error:
                with lock:
                    errors.append({"operation": "read", "error": str(error)})
            finally:
                permits.release()

        def schedule(pool):
            if read_qps == 0:
                return
            serial = 0
            while time.perf_counter() < deadline and not stop.is_set():
                stop.wait(max(0, started + serial / read_qps - time.perf_counter()))
                if time.perf_counter() >= deadline or stop.is_set():
                    break
                read_stats["offered"] += 1
                if permits.acquire(blocking=False):
                    pool.submit(read, serial)
                else:
                    read_stats["dropped"] += 1
                serial += 1

        def monitor():
            while not stop.is_set():
                try:
                    samples.append(self.telemetry())
                except Exception as error:
                    samples.append({"at": time.time(), "error": str(error)})
                stop.wait(self.args.sample_interval)

        batches, checks, arrival_to_commit = [], [], []
        submitted = 0
        target = round(rate * self.args.duration)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.read_workers) as pool:
            scheduler = threading.Thread(target=schedule, args=(pool,), daemon=True)
            observer = threading.Thread(target=monitor, daemon=True)
            scheduler.start()
            observer.start()
            try:
                while submitted < target:
                    count = min(self.args.batch_size, target - submitted)
                    # Deliver a full batch after its events would have arrived.
                    stop.wait(max(0, started + (submitted + count) / rate - time.perf_counter()))
                    if time.perf_counter() > deadline + self.args.max_drain_seconds:
                        errors.append({"operation": "insert", "error": "Writer could not drain the offered load in time"})
                        break
                    elapsed, lookup = self.insert(self.ordinal, count, name, self.args.first_event_check)
                    arrival_to_commit.append(time.perf_counter() - (started + (submitted + 1) / rate))
                    self.ordinal += count
                    submitted += count
                    batches.append(elapsed)
                    checks.append(lookup)
            except Exception as error:
                errors.append({"operation": "insert", "error": str(error)})
            finally:
                write_finished = time.perf_counter()
                if write_finished < deadline:
                    stop.wait(deadline - write_finished)
                stop.set()
                scheduler.join()
                observer.join()
        wall_seconds = max(self.args.duration, write_finished - started)
        self.client.execute("SYSTEM FLUSH LOGS")
        query_log = self.client.rows(
            "SELECT if(position(query_id,'_read_')>0,'read',if(position(query_id,'_lookup_')>0,'lookup','insert')) AS operation,"
            "count() AS queries,quantileExact(.95)(query_duration_ms) AS server_p95_ms,"
            "max(memory_usage) AS max_query_memory_bytes,sum(read_rows) AS read_rows,"
            "sum(ProfileEvents['UserTimeMicroseconds']+ProfileEvents['SystemTimeMicroseconds'])/1000000 AS query_cpu_seconds "
            f"FROM system.query_log WHERE type='QueryFinish' AND startsWith(query_id,'{self.prefix}{name}_') GROUP BY operation")
        result = {"case": name, "offered_events_per_second": rate, "offered_read_qps": read_qps,
                  "offered_rows": target, "completed_rows": submitted,
                  "writer_elapsed_seconds": round(wall_seconds, 3),
                  "achieved_events_per_second": round(submitted / wall_seconds, 2),
                  "writer_drain_seconds": round(max(0, write_finished - deadline), 3),
                  "batch_latency": distribution(batches), "first_lookup_latency": distribution(checks),
                  "oldest_event_to_batch_commit_latency": distribution(arrival_to_commit),
                  "read_latency": distribution(reads), "read_requests": read_stats,
                  "errors": errors, "query_log": query_log, "samples": samples,
                  "correctness": self.validate()}
        result["passed"] = (not errors and submitted == target and read_stats["dropped"] == 0
                            and result["correctness"]["passed"]
                            and (not reads or result["read_latency"]["p95_ms"] <= self.args.read_p95_ms)
                            and result["writer_drain_seconds"] <= self.args.allowed_drain_seconds
                            and all("error" not in sample for sample in samples))
        self.report["cases"].append(result)
        self.save()
        print(json.dumps({key: value for key, value in result.items()
                          if key not in ("samples", "correctness", "query_log")}, ensure_ascii=False), flush=True)

    def run(self):
        self.setup()
        print(f"Created {self.db}; results: {self.output}", flush=True)
        tick = time.perf_counter()
        while self.ordinal < self.args.seed_rows:
            count = min(self.args.seed_batch_size, self.args.seed_rows - self.ordinal)
            self.insert(self.ordinal, count, "seed", False)
            self.ordinal += count
        self.report["seed_seconds"] = round(time.perf_counter() - tick, 3)
        self.report["seed_correctness"] = self.validate()
        if not self.report["seed_correctness"]["passed"]:
            raise RuntimeError("Seed correctness failed")
        for serial in range(20):
            self.summary_query("warmup", serial)
        print(f"Seeded {self.ordinal} input rows in {self.report['seed_seconds']}s", flush=True)
        for read_qps in self.args.read_qps:
            for rate in self.args.rates:
                self.phase(rate, read_qps)
        self.report["final_metrics"] = self.telemetry()
        self.report["all_cases_passed"] = all(case["passed"] for case in self.report["cases"])
        if self.args.cleanup:
            self.client.execute(f"DROP DATABASE {self.db} SYNC")
            self.report["database_removed"] = True
        self.save()
        return 0 if self.report["all_cases_passed"] else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Explicit HTTP endpoint of the isolated single-shard test server")
    parser.add_argument("--profile-label", required=True, help="Observed instance/Pod resource configuration, not an inferred equivalence")
    parser.add_argument("--user", default="default")
    parser.add_argument("--password-env", help="Environment variable containing the HTTP user's password")
    parser.add_argument("--seed-rows", type=int, default=1_000_000)
    parser.add_argument("--seed-batch-size", type=int, default=10_000)
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--products", type=int, default=2_000, help="Synthetic placement cardinality")
    parser.add_argument("--hot-percent", type=int, default=50)
    parser.add_argument("--rates", type=int, nargs="+", default=[400, 4_000])
    parser.add_argument("--read-qps", type=int, nargs="+", default=[10, 50])
    parser.add_argument("--read-workers", type=int, default=64)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--read-p95-ms", type=float, default=1_000)
    parser.add_argument("--allowed-drain-seconds", type=float, default=5)
    parser.add_argument("--max-drain-seconds", type=float, default=30)
    parser.add_argument("--sample-interval", type=float, default=2)
    parser.add_argument("--max-threads", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--first-event-check", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--store-raw", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--cleanup", action="store_true", help="Remove only this invocation's unique database after reporting")
    parser.add_argument("--output", default=str(HERE / "capacity-results"))
    args = parser.parse_args()
    if args.products < 2 or not 0 <= args.hot_percent <= 100:
        parser.error("products must be >=2 and hot-percent must be between 0 and 100")
    for name in ("seed_batch_size", "batch_size", "read_workers", "duration", "read_p95_ms",
                 "sample_interval", "max_threads", "timeout", "max_drain_seconds"):
        if getattr(args, name) <= 0:
            parser.error(f"{name} must be positive")
    if args.seed_rows < 0 or any(rate <= 0 for rate in args.rates) or any(qps < 0 for qps in args.read_qps):
        parser.error("seed-rows/read-qps must be nonnegative and rates must be positive")
    if len(set(args.rates)) != len(args.rates) or len(set(args.read_qps)) != len(args.read_qps):
        parser.error("rates and read-qps must not contain duplicates")
    if args.allowed_drain_seconds < 0:
        parser.error("allowed-drain-seconds must be nonnegative")
    return args


def main():
    bench = Benchmark(arguments())
    try:
        return bench.run()
    except Exception as error:
        bench.report["fatal_error"] = str(error)
        bench.save()
        print(f"Benchmark failed: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
