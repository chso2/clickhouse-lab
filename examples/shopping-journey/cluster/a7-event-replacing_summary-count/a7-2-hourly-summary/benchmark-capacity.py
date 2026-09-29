#!/usr/bin/env python3
"""A7-2 state, signed hourly delta, source events and reconciliation benchmark.

Reuses the A7-1 open-loop scheduler and unique database safeguards. The workload
has one writer; controlled two-writer races are separate correctness contracts.
"""
import collections
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import threading
import time

HERE = Path(__file__).resolve().parent
BASE_PATH = HERE.parent / 'a7-1-cumulative-summary' / 'benchmark-capacity.py'
spec = importlib.util.spec_from_file_location('a7_capacity_base', BASE_PATH)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
COUNTS = ('notify_count', 'view_count', 'cart_count', 'click_count', 'purchase_count')


class Benchmark(base.Benchmark):
    def __init__(self, args):
        super().__init__(args)
        self.hourly_expected = collections.Counter()
        self.reconcile_stop = None
        self.reconcile_thread = None
        self.reconcile_results = []
        self.report['scope'] = 'synthetic single-shard A7-2; not an AWS capacity guarantee'
        self.report['benchmark_source_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        sources = [BASE_PATH, HERE / 'benchmark-capacity.py', HERE / 'schema.sql',
                   HERE / 'backfill-from-a7-1.sql', HERE / 'reconcile-product.sql',
                   base.HERE / 'schema.sql', HERE.parent.parent / 'common' / 'schema.sql']
        self.report['source_sha256'] = {str(path.relative_to(HERE.parent.parent)):
                                      hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}

    def statements(self, path, db=None):
        sql = path.read_text().replace('shop_a7_1', db or self.db).replace('shop_a7_2', db or self.db)
        sql = sql.replace(" ON CLUSTER 'cluster1'", '')
        # SQL sources in this example contain no semicolons inside string literals.
        return [statement for statement in sql.split(';') if statement.strip()]

    def setup(self):
        super().setup()
        for statement in self.statements(HERE / 'schema.sql'):
            self.client.execute(statement)
        self.save()

    def oracle(self, grouped):
        # Derive the expected bucket from the fixture identity, independently of
        # the database lookup or signed deltas returned by plan(). Each duplicate
        # has exactly one original, one hour later, including batch boundaries.
        epoch = base.datetime.datetime(2026, 6, 1, tzinfo=base.datetime.timezone.utc)
        for index, entries in enumerate(grouped):
            for ordinal, canonical, row in entries:
                hour = row['occurred_at'][:13] + ':00:00'
                product = row['product_id']
                if ordinal == canonical:
                    self.hourly_expected[(product, hour, base.KINDS[index])] += 1
                else:
                    identity = hashlib.sha256(str(canonical // 5).encode()).hexdigest()
                    original = epoch + base.datetime.timedelta(seconds=int(identity[:16], 16) % (90 * 86400))
                    old_hour = original.strftime('%Y-%m-%d %H:00:00')
                    self.hourly_expected[(product, old_hour, base.KINDS[index])] -= 1
                    self.hourly_expected[(product, hour, base.KINDS[index])] += 1

    def event_time(self, ordinal, canonical, epoch):
        # Spread even a small seed over 90 days, rather than filling only its
        # first few days. All earlier duplicates move by a full hour.
        identity = hashlib.sha256(str(canonical // 5).encode()).hexdigest()
        occurred = epoch + base.datetime.timedelta(seconds=int(identity[:16], 16) % (90 * 86400))
        if ordinal != canonical:
            occurred -= base.datetime.timedelta(hours=1)
        return occurred

    def lookup(self, rows, query_id):
        keys = {(row['product_id'], row['event_kind'], row['journey_id']) for row in rows}
        tuples = ','.join(f"({product},'{kind}','{journey}')" for product, kind, journey in sorted(keys))
        found = self.client.rows(
            f'SELECT product_id,event_type,journey_id,mall_id,store_id,first_occurred_at '
            f'FROM {self.db}.first_event_state_local FINAL '
            f'WHERE (product_id,event_type,journey_id) IN ({tuples})', query_id)
        return {(int(row['product_id']), row['event_type'], row['journey_id']): row for row in found}

    def plan(self, rows, existing):
        states, deltas = [], collections.Counter()
        events = [[] for _ in base.KINDS]
        for row in rows:
            key = (row['product_id'], row['event_kind'], row['journey_id'])
            old = existing.get(key)
            first = old is None
            index = base.KINDS.index(row['event_kind'])
            event = {name: value for name, value in row.items() if name != 'event_kind'}
            events[index].append(dict(event, count_delta=int(first)))
            if old is not None and old['first_occurred_at'] <= row['occurred_at']:
                continue
            if old is not None:
                deltas[(int(old['mall_id']), int(old['store_id']), row['product_id'],
                        old['first_occurred_at'][:13] + ':00:00', row['event_kind'])] -= 1
            deltas[(row['mall_id'], row['store_id'], row['product_id'],
                    row['occurred_at'][:13] + ':00:00', row['event_kind'])] += 1
            state = {name: row[name] for name in ('mall_id', 'store_id', 'product_id', 'journey_id', 'customer_group_ids')}
            state.update(event_type=row['event_kind'], first_occurred_at=row['occurred_at'])
            states.append(state)
            existing[key] = state
        grouped_deltas = {}
        for (mall, store, product, hour, kind), delta in deltas.items():
            if delta:
                bucket = grouped_deltas.setdefault((mall, store, product, hour), dict(
                    mall_id=mall, store_id=store, product_id=product, event_hour=hour,
                    **{column: 0 for column in COUNTS}))
                bucket[base.COUNT_COLUMNS[base.KINDS.index(kind)]] += delta
        return states, list(grouped_deltas.values()), events

    def write(self, table, rows, token):
        if rows:
            self.client.execute(
                f"INSERT INTO {self.db}.{table} SETTINGS insert_deduplication_token='{token}' FORMAT JSONEachRow",
                token, self.payload(rows))

    def commit_plan(self, plan, token, include_delta=True):
        states, deltas, events = plan
        for index, rows in enumerate(events):
            self.write(base.TABLES[index] + '_events_local', rows, token + f'_event_{index}')
        self.write('first_event_state_local', states, token + '_state')
        if include_delta:
            self.write('hourly_summary_local', deltas, token + '_hourly')

    def insert(self, start, count, case, check_existing):
        grouped, raw = self.fixture(start, count)
        if not check_existing:
            elapsed, lookup = super().insert(start, count, case, False)
            self.oracle(grouped)
            return elapsed, lookup
        started = time.perf_counter()
        if self.args.store_raw:
            self.write('shopping_events_local', raw, self.prefix + case + f'_raw_{start}')
            self.raw_rows += count
        lookup_start = time.perf_counter()
        existing = self.lookup(raw, self.prefix + case + f'_lookup_{start}')
        lookup_elapsed = time.perf_counter() - lookup_start
        self.commit_plan(self.plan(raw, existing), self.prefix + case + f'_insert_{start}')
        for index, entries in enumerate(grouped):
            for ordinal, canonical, row in entries:
                if ordinal == canonical:
                    self.expected[(row['product_id'], index)] += 1
        self.oracle(grouped)
        return time.perf_counter() - started, lookup_elapsed

    def summary_query(self, case, serial):
        product = 1 if serial % 2 == 0 else 2 + serial % (self.args.products - 1)
        return self.client.execute(
            f"SELECT * FROM {self.db}.hourly_summary_by_product(product_id={product},"
            "from_hour='2026-05-01 00:00:00',to_hour='2026-10-01 00:00:00') "
            'ORDER BY event_hour FORMAT JSONEachRow', self.prefix + case + f'_read_{serial}')

    def hourly_counts(self, table='hourly_summary'):
        columns = ','.join(f'sum({column}) AS {column}' for column in COUNTS)
        rows = self.client.rows(f'SELECT product_id,event_hour,{columns} FROM {self.db}.{table} GROUP BY product_id,event_hour')
        result = collections.Counter()
        for row in rows:
            for index, kind in enumerate(base.KINDS):
                value = int(row[base.COUNT_COLUMNS[index]])
                if value:
                    result[(int(row['product_id']), row['event_hour'], kind)] = value
        return result

    def reconcile(self, product, query_id):
        statement = self.statements(HERE / 'reconcile-product.sql')[-1]
        statement = statement.replace('{product_id:UInt64}', str(product))
        self.client.execute(statement + ' SETTINGS join_use_nulls=1, distributed_foreground_insert=1', query_id)

    def contracts(self):
        original = self.db
        self.db = original + '_contracts'
        checks = []
        try:
            for path in (base.HERE / 'schema.sql', HERE / 'schema.sql'):
                for statement in self.statements(path):
                    self.client.execute(statement)
            def row(message, hour, product=9000001):
                return dict(message_id=message, mall_id=999, store_id=9999, product_id=product,
                            journey_id=f'contract-{product}', customer_group_ids=[], source_kind='contract',
                            event_kind='CLICK', occurred_at=f'2026-09-28 {hour}:00.000',
                            received_at='2026-09-28 12:00:00.000')
            first = row('first', '11:20')
            self.commit_plan(self.plan([first], {}), self.db + '_first')
            later = row('later', '12:30')
            later_plan = self.plan([later], self.lookup([later], self.db + '_lookup_later'))
            assert not later_plan[0] and not later_plan[1], 'Later candidate must not change state or delta'
            self.commit_plan(later_plan, self.db + '_later')
            earlier = row('earlier', '09:10')
            move_plan = self.plan([earlier], self.lookup([earlier], self.db + '_lookup_earlier'))
            self.commit_plan(move_plan, self.db + '_move')
            expected = collections.Counter({(9000001, '2026-09-28 09:00:00', 'CLICK'): 1})
            assert self.hourly_counts() == expected, 'Hour movement must remove the old bucket'
            checks.append({'name': 'late_first_moves_hour_total_stays_one', 'passed': True})
            # Re-send exactly the same plan with the same per-table tokens.
            self.commit_plan(move_plan, self.db + '_move')
            assert self.hourly_counts() == expected, 'Retry must not repeat hour movement'
            retry = row('retry', '10:10', 9000002)
            retry_plan = self.plan([retry], {})
            self.commit_plan(retry_plan, self.db + '_retry')
            self.commit_plan(retry_plan, self.db + '_retry')
            expected[(9000002, '2026-09-28 10:00:00', 'CLICK')] = 1
            assert self.hourly_counts() == expected, 'Same token must not apply a delta twice'
            checks.append({'name': 'same_message_same_token_retry_once', 'passed': True})
            barrier = threading.Barrier(2)
            def racer(number):
                event = row(f'race-{number}', '08:10', 9000003)
                state = self.lookup([event], self.db + f'_lookup_race_{number}')
                assert not state
                plan = self.plan([event], state)
                barrier.wait(timeout=15)
                self.commit_plan(plan, self.db + f'_race_{number}')
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(racer, number) for number in range(2)]
                for future in futures:
                    future.result()
            race_key = (9000003, '2026-09-28 08:00:00', 'CLICK')
            assert self.hourly_counts()[race_key] == 2, 'Controlled race should expose overcount before reconcile'
            tick = time.perf_counter()
            self.reconcile(9000003, self.db + '_reconcile_race')
            race_reconcile_ms = round((time.perf_counter() - tick) * 1000, 3)
            expected[race_key] = 1
            assert self.hourly_counts() == expected
            self.reconcile(9000003, self.db + '_reconcile_race_again')
            assert self.hourly_counts() == expected, 'Quiescent reconciliation must be idempotent'
            checks.append({'name': 'two_writers_same_key_2_then_reconcile_1', 'passed': True,
                           'reconcile_ms': race_reconcile_ms})
            partial = row('partial', '07:10', 9000004)
            self.commit_plan(self.plan([partial], {}), self.db + '_partial', include_delta=False)
            expected[(9000004, '2026-09-28 07:00:00', 'CLICK')] = 1
            assert self.hourly_counts() != expected
            self.reconcile(9000004, self.db + '_reconcile_partial')
            assert self.hourly_counts() == expected
            checks.append({'name': 'source_and_state_committed_missing_delta_repaired', 'passed': True})
            state = self.client.rows(f'SELECT first_occurred_at FROM {self.db}.first_event_state_local FINAL WHERE product_id=9000001')[0]
            assert state['first_occurred_at'] == '2026-09-28 09:10:00.000'
            self.client.execute(f'DROP DATABASE {self.db} SYNC')
        finally:
            self.db = original
            self.report['contracts'] = checks
            self.save()

    def stop_reconciliation(self):
        if self.reconcile_stop is not None:
            self.reconcile_stop.set()
            self.reconcile_thread.join()
            self.reconcile_stop = None

    def validate(self):
        self.stop_reconciliation()
        result = super().validate()
        actual = self.hourly_counts()
        mismatches = [{'product': key[0], 'hour': key[1], 'event': key[2],
                       'expected': self.hourly_expected[key], 'actual': actual[key]}
                      for key in self.hourly_expected.keys() | actual.keys()
                      if self.hourly_expected[key] != actual[key]]
        # A background reconcile snapshot can overlap the multi-table writer.
        # Record its residual error, then measure quiescent convergence separately
        # from ingestion drain and service query latency.
        repairs = []
        if mismatches:
            for product in sorted({item['product'] for item in mismatches}):
                tick = time.perf_counter()
                self.reconcile(product, self.prefix + f'final_reconcile_{self.ordinal}_{product}')
                repairs.append({'product': product, 'seconds': time.perf_counter() - tick})
            actual = self.hourly_counts()
        remaining = [{'product': key[0], 'hour': key[1], 'event': key[2],
                      'expected': self.hourly_expected[key], 'actual': actual[key]}
                     for key in self.hourly_expected.keys() | actual.keys()
                     if self.hourly_expected[key] != actual[key]]
        unions = ' UNION ALL '.join(
            f"SELECT '{kind}' AS event_type,product_id,journey_id,occurred_at FROM {self.db}.{table}_events_local FINAL"
            for kind, table in zip(base.KINDS, base.TABLES))
        # Full key/time comparison catches missing or stale state, including keys
        # whose total counts happen to agree. This runs after offered load stops.
        state_mismatches = int(self.client.rows(
            f'WITH source AS ({unions}), state AS (SELECT event_type,product_id,journey_id,first_occurred_at '
            f'FROM {self.db}.first_event_state_local FINAL) SELECT count() AS n FROM source '
            'FULL OUTER JOIN state USING (event_type,product_id,journey_id) '
            'WHERE source.occurred_at IS NULL OR state.first_occurred_at IS NULL '
            'OR source.occurred_at != state.first_occurred_at SETTINGS join_use_nulls=1')[0]['n'])
        result.update(hourly_mismatches_before_final_reconcile=mismatches[:20],
                      hourly_mismatch_count_before_final_reconcile=len(mismatches),
                      final_reconcile_requests=repairs,
                      final_reconcile_total_seconds=round(sum(item['seconds'] for item in repairs), 3),
                      hourly_mismatches=remaining[:20], state_key_time_mismatches=state_mismatches,
                      hourly_correct=not remaining, state_correct=state_mismatches == 0)
        result['passed'] = result['passed'] and not remaining and state_mismatches == 0
        return result

    def phase(self, rate, read_qps):
        self.reconcile_results = []
        self.reconcile_stop = threading.Event()
        stop = self.reconcile_stop
        name = f'r{rate}_q{read_qps}'
        def worker():
            serial = 0
            while not stop.wait(self.args.reconcile_interval):
                for product in self.args.reconcile_products:
                    if stop.is_set():
                        break
                    tick = time.perf_counter()
                    try:
                        self.reconcile(product, self.prefix + name + f'_reconcile_{serial}_{product}')
                        self.reconcile_results.append({'product': product, 'seconds': time.perf_counter() - tick})
                    except Exception as error:
                        self.reconcile_results.append({'product': product, 'error': str(error)})
                    serial += 1
        self.reconcile_thread = threading.Thread(target=worker, daemon=True)
        self.reconcile_thread.start()
        try:
            super().phase(rate, read_qps)
        finally:
            self.stop_reconciliation()
        case = self.report['cases'][-1]
        case['reconciliation'] = dict(
            scope='specified products only; not all changed-product backlog',
            products=self.args.reconcile_products, interval_seconds=self.args.reconcile_interval,
            requests=self.reconcile_results,
            latency=base.distribution([item['seconds'] for item in self.reconcile_results if 'seconds' in item]))
        case['passed'] = case['passed'] and all('error' not in item for item in self.reconcile_results)
        self.save()

    def run(self):
        self.setup()
        print(f'Created {self.db}; results: {self.output}', flush=True)
        self.contracts()
        tick = time.perf_counter()
        while self.ordinal < self.args.seed_rows:
            count = min(self.args.seed_batch_size, self.args.seed_rows - self.ordinal)
            self.insert(self.ordinal, count, 'seed', False)
            self.ordinal += count
        self.report['seed_seconds'] = round(time.perf_counter() - tick, 3)
        tick = time.perf_counter()
        for statement in self.statements(HERE / 'backfill-from-a7-1.sql'):
            self.client.execute(statement)
        self.report['a7_2_backfill_seconds'] = round(time.perf_counter() - tick, 3)
        self.report['seed_correctness'] = self.validate()
        if not self.report['seed_correctness']['passed']:
            raise RuntimeError('A7-2 backfill correctness failed')
        for serial in range(20):
            self.summary_query('warmup', serial)
        print(f"Seeded {self.ordinal} rows; source {self.report['seed_seconds']}s, A7-2 backfill {self.report['a7_2_backfill_seconds']}s", flush=True)
        for read_qps in self.args.read_qps:
            for rate in self.args.rates:
                self.phase(rate, read_qps)
        self.report['storage'] = self.client.rows(
            f"SELECT table,sum(rows) AS physical_rows,sum(data_compressed_bytes) AS compressed_bytes,"
            f"sum(bytes_on_disk) AS bytes_on_disk FROM system.parts WHERE active AND database='{self.db}' GROUP BY table")
        self.report['final_metrics'] = self.telemetry()
        self.report['all_cases_passed'] = all(case['passed'] for case in self.report['cases'])
        if self.args.cleanup:
            self.client.execute(f'DROP DATABASE {self.db} SYNC')
            self.report['database_removed'] = True
        self.save()
        return 0 if self.report['all_cases_passed'] else 1


def arguments():
    parser = base.argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reconcile-interval', type=float, default=5)
    parser.add_argument('--reconcile-products', type=int, nargs='+', default=[1, 2])
    args = base.arguments(parser)
    if args.reconcile_interval <= 0 or any(product <= 0 for product in args.reconcile_products):
        parser.error('reconcile interval and product IDs must be positive')
    if not args.first_event_check:
        parser.error('A7-2 requires --first-event-check')
    if args.output == str(base.HERE / 'capacity-results'):
        args.output = str(HERE / 'capacity-results')
    return args


def main():
    bench = Benchmark(arguments())
    try:
        return bench.run()
    except Exception as error:
        bench.stop_reconciliation()
        bench.report['fatal_error'] = str(error)
        bench.save()
        print(f'Benchmark failed: {error}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
