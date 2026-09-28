# A7 · a7-event-replacing_summary-count

[전체 비교](../README.md) · [공통 기준](../common/README.md) · [기대 기준](../../expected/README.md) · [아키텍처](../../docs/architecture.md#이벤트별-최초-데이터와-clickhouse-summary-개선안)

| 구분 | 경로 |
|---|---|
| A7-1 전체 누적 | 최초 이벤트 판정 → 이벤트별 ReplacingMergeTree → 숫자 delta summary |
| 시간·고객 그룹별 | A7-1에서 제외, 후속 실험에서 직접 `FINAL` 조회부터 검증 |
| A7-1 발송 누적 | 공통 NOTIFY 상세 → 최초 이벤트 숫자 delta; S3 사전 집계 직접 합산은 후속 |
| 생성 흐름 | 이벤트 종류별 후보 저장과 최초 이벤트 누적 count 생성 |
| A7-1 저장 대상 | 이벤트별 Replacing 테이블, 누적 summary |
| 독립 DB | `shop_a7_1` |
| 현재 상태 | A7-1 DDL·정확성·1차 성능 검증 완료 |

## 설계 목적

A7은 하나의 넓은 dedup aggregate state를 조회할 때 발생한 `argMinMerge` 비용을 줄이기 위한 후보입니다. VIEW·CART·CLICK·PURCHASE·NOTIFY를 이벤트별 테이블로 나누고, 서비스 조회는 작은 summary에서 처리합니다.

![A7 쇼핑몰 구매 여정 집계 아키텍처](../../docs/event-summary-architecture.svg)

```text
행동·클릭 입력
  ├─ view_event
  ├─ cart_event
  ├─ click_event
  └─ purchase_event
          │  ReplacingMergeTree: 상품·여정별 가장 빠른 이벤트
          ├─ count_delta=1만 누적 숫자 delta summary
          └─ 변경 상품 FINAL 재계산 → 시간·고객 그룹 summary

S3 발송 입력
  ├─ notification_count ─────────────→ 누적 summary 직접 합산
  └─ notification_raw → notification_event → 시간·고객 그룹 summary
```

위 그림은 A7의 전체 후보 구조입니다. 이번에 구현한 **A7-1은 시간 summary와 보정 경로를 만들지 않고 누적 summary까지만 검증**합니다.

## A7-1 · 시간 Summary 없는 누적 구조

```text
shop_benchmark.shopping_events
  ├─ view_events         ─┐
  ├─ cart_events          │
  ├─ click_events         ├─ count_delta=1만 count delta 추가
  ├─ purchase_events      │              ↓
  └─ notification_events ─┘  SummingMergeTree cumulative_summary
                                         ↓
                          상품별 숫자 누적 count View
```

이벤트 테이블은 `(product_id, journey_id)`를 정렬 키로 사용합니다. `first_version`은 더 이른 `occurred_at`일수록 큰 값이 되므로 `FINAL`과 background merge에서 최초 발생 후보가 선택됩니다.

입력 처리기는 동일 이벤트 테이블의 `(product_id, journey_id)`가 처음 등록될 때만 `count_delta=1`을 전달해야 합니다. 중복과 늦게 도착한 더 이른 후보는 이벤트 테이블에는 저장하지만 `count_delta=0`이므로 누적 count를 다시 증가시키지 않습니다.

두 열의 의미는 다릅니다. `first_version`은 상세 행 중 **발생 시각이 가장 빠른 행**을 고르고, `count_delta`는 해당 입력이 **누적 count에 더할 값**을 나타냅니다.

### Count Delta

Delta는 기존 누적값에 더할 변화량입니다. `count_delta`는 최종 count가 아니라 이번 INSERT가 Summary에 기여하는 값입니다.

| `count_delta` | 의미 | Summary 변화 |
|---:|---|---:|
| 1 | 해당 이벤트 키를 처음 집계 | 이벤트 count +1 |
| 0 | 중복 또는 후속 후보 | 변화 없음 |

집계 키는 이벤트 종류별 `(product_id, journey_id)`입니다. 예를 들어 한 상품에 서로 다른 두 journey의 CLICK이 있으면 결과는 2가 맞습니다.

```text
product_id | journey_id | count_delta
-----------+------------+------------
p-1        | j-1        | 1
p-1        | j-1        | 0  ← 동일 journey 중복
p-1        | j-2        | 1

상품 p-1의 click_count = 1 + 0 + 1 = 2
```

동일한 `j-1`에 `count_delta=1`이 두 번 생성되면 잘못된 중복 집계입니다. 입력 처리기가 동일 이벤트 키를 순차 판정해야 하는 이유입니다.

이벤트 테이블의 delta는 MV에서 이벤트별 count 열로 변환됩니다. `cumulative_summary_local`은 `count_delta` 자체를 저장하지 않습니다.

```text
click_events_local.count_delta
              │ sum(count_delta)
              ▼
cumulative_summary_local.click_count
```

`cumulative_summary_local`은 상품별 `notify_count`, `view_count`, `cart_count`, `click_count`, `purchase_count` delta 행을 `SummingMergeTree`로 저장합니다. background merge 전 행과 shard별 부분 count가 남아 있을 수 있으므로 조회 View가 마지막 `sum()`을 수행해 완성된 한 행을 반환합니다.

누적 summary는 tracking ID 집합 대신 다음 숫자 delta를 저장합니다.

```text
product_id | notify_count | view_count | cart_count | click_count | purchase_count
p-1        |      0       |      0     |      0     |      1      |        0
```

MV는 기존 행을 UPDATE하지 않고 INSERT 블록별 delta 행을 추가합니다. `SummingMergeTree`가 동일 상품의 행을 background에서 합치며, parameterized View의 내부 `sum()`이 merge 전 행과 shard별 부분 count를 최종 숫자로 합칩니다. 애플리케이션은 View에서 완성된 한 행을 조회합니다.

여기서 상세 대표 행과 누적 count의 책임은 분리됩니다.

- 이벤트 테이블의 `ReplacingMergeTree`는 최초 이벤트의 상세 행을 남깁니다.
- 입력 처리기의 `count_delta` 판정은 누적 count를 증가시킬지 결정합니다.
- 숫자 Summary는 `count_delta > 0`인 행의 delta를 `sum(count_delta)`로 저장합니다.
- 일반 증분 MV는 ReplacingMergeTree의 나중 merge 결과를 다시 읽지 않으므로, `count_delta`를 ClickHouse background merge가 결정해 주지는 않습니다.
- 동일 키를 동시에 처리하는 두 입력이 모두 `count_delta=1`이 되지 않도록 키 단위 직렬화와 재시도 멱등 처리가 필요합니다.

### 동시 입력과 재시도 처리 계약

`SELECT`로 기존 키를 확인한 뒤 `INSERT`하는 두 작업은 ClickHouse에서 하나의 트랜잭션으로 묶이지 않습니다. 여러 consumer가 같은 이벤트 키를 동시에 처리하면 둘 다 기존 키가 없다고 판단할 수 있습니다. A7-1은 다음 입력 계약으로 이 경쟁 조건을 막습니다.

1. 라우팅 키를 `(event_kind, product_id, journey_id)`로 고정합니다.
2. 같은 라우팅 키는 항상 같은 queue partition으로 전달합니다.
3. consumer group은 한 partition을 한 consumer만 처리하고, partition 내부 이벤트를 순차 처리합니다.
4. consumer는 해당 이벤트 테이블에 키가 이미 존재하는지 확인하여 최초 입력에만 `count_delta=1`을 설정합니다.
5. INSERT가 성공한 뒤 offset을 확정합니다. 장애로 같은 메시지를 재시도할 때는 `message_id`에서 만든 동일한 `insert_deduplication_token`을 사용합니다.
6. partition 재할당 시 이전 consumer의 진행 중 작업을 끝내거나 중단한 뒤 소유권을 넘겨, 같은 partition을 두 consumer가 겹쳐 처리하지 않게 합니다.

```text
같은 이벤트 키 A ─┐
같은 이벤트 키 A ─┼─ hash(event_kind, product_id, journey_id)
같은 이벤트 키 B ─┘                  │
                                      ▼
                              고정 queue partition
                                      │ 순차 처리
                                      ▼
                      기존 키 없음 → count_delta=1 → count +1
                      기존 키 있음 → count_delta=0 → count 유지
```

동일한 메시지의 재전송은 insert token으로 막을 수 있지만, 서로 다른 `message_id`를 가진 동일 이벤트 키의 경쟁은 token만으로 막을 수 없습니다. 이 경우에는 위의 partition 단위 직렬화가 필요합니다. 이 입력 계약을 적용할 수 없다면 숫자 delta Summary 대신 `uniqExactState`처럼 조회 시 중복을 합치는 구조를 사용해야 합니다.

### 파일

| 파일 | 역할 |
|---|---|
| [schema.sql](./a7-1-cumulative-summary/schema.sql) | 이벤트별 local/Distributed 테이블, 누적 summary와 MV 생성 |
| [backfill-from-common.sql](./a7-1-cumulative-summary/backfill-from-common.sql) | shard-local 공통 원본을 이벤트별 테이블에 분리 적재 |
| [run-backfill-from-common.sh](./a7-1-cumulative-summary/run-backfill-from-common.sh) | shard 대표 replica를 순차 처리 |
| [queries.sql](./a7-1-cumulative-summary/queries.sql) | 숫자 delta 합산을 감춘 API용 한 행 View 조회 |
| [check-results.sql](./a7-1-cumulative-summary/check-results.sql) | 일반·집중 상품, 최초 이벤트, 복제 상태 자동 판정 |
| [check-count-delta.sql](./a7-1-cumulative-summary/check-count-delta.sql) | 두 고유 journey와 한 중복 후보의 delta 및 Summary count 검증 |
| [check-realtime.sql](./a7-1-cumulative-summary/check-realtime.sql) | 한 건 INSERT, 중복, 늦게 도착한 최초 이벤트 검증 |
| [check-idempotent-retry.sql](./a7-1-cumulative-summary/check-idempotent-retry.sql) | 같은 메시지와 insert token으로 재시도할 때 한 번만 반영되는지 검증 |

### 실행 순서

공통 원본 22,200,000행이 준비된 상태에서 실행합니다. XML 기반 `cluster_internal` 사용자를 쓰는 free-operator 구성은 사용자 정의에 다음 권한을 추가하고 ClickHouse StatefulSet을 순차 재시작해야 합니다.

```xml
<query>GRANT SELECT, INSERT ON shop_a7_1.*</query>
```

기본 Operator 4-shard 구성은 스크립트 인자를 생략합니다. 현재 free-operator 3-shard 구성은 대표 replica 세 개를 명시합니다.

```bash
export LAB_CLICKHOUSE_POD=clickhouse-0

kubectl --context kind-clickhouse-lab -n clickhouse exec -i "$LAB_CLICKHOUSE_POD" -c clickhouse \
  -- clickhouse-client --multiquery \
  < examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/schema.sql

LAB_CLICKHOUSE_POD=clickhouse-0 \
  examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/run-backfill-from-common.sh \
  clickhouse-0 clickhouse-3 clickhouse-6

kubectl --context kind-clickhouse-lab -n clickhouse exec -i "$LAB_CLICKHOUSE_POD" -c clickhouse \
  -- clickhouse-client --multiquery --format PrettyCompact \
  < examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/check-results.sql

kubectl --context kind-clickhouse-lab -n clickhouse exec -i "$LAB_CLICKHOUSE_POD" -c clickhouse \
  -- clickhouse-client --multiquery --time --format PrettyCompact \
  < examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/check-count-delta.sql

kubectl --context kind-clickhouse-lab -n clickhouse exec -i "$LAB_CLICKHOUSE_POD" -c clickhouse \
  -- clickhouse-client --multiquery --time --format PrettyCompact \
  < examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/check-realtime.sql

kubectl --context kind-clickhouse-lab -n clickhouse exec -i "$LAB_CLICKHOUSE_POD" -c clickhouse \
  -- clickhouse-client --multiquery --time --format PrettyCompact \
  < examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/check-idempotent-retry.sql
```

### 실행 결과

2026-09-28, ClickHouse `26.9.4.3`, free-operator **3 shard × 3 replica**와 공통 원본 snapshot에서 확인했습니다. 동시성 1의 1차 예비 측정입니다.

| 항목 | 결과 |
|---|---:|
| 공통 원본 | 22,200,000행 |
| 최초 이벤트 판정 backfill + summary MV | 2분 04.14초 |
| 이벤트별 테이블 + summary 압축 크기 | 약 311.11 MiB, shard 합계·replica 1벌 기준 |
| 그중 숫자 누적 summary | 28.87 KiB, 304행 |
| replication queue / delay | 0 / 0 |
| 최소 active replica | 3 |

일반 상품과 집중 상품의 모든 이벤트 count가 기대값과 일치했습니다. 일반 상품 CLICK은 공통 원본 5,500행에서 `FINAL` 5,000행으로 정리됐고, 늦게 들어왔지만 발생 시각이 1분 빠른 retry 500행이 대표로 선택됐습니다.

backfill에는 공통 CLICK 중 최초 수집 행을 고르는 window 정렬 시간이 포함됩니다. 운영에서는 입력 처리기가 이미 `count_delta`를 결정한다고 가정하므로 이 시간을 실시간 INSERT 비용으로 해석하지 않습니다.

| 조회 | 반복 | p50 | p95 | p99 | 결과 |
|---|---:|---:|---:|---:|---|
| 조회 View, 분산 상품 1개, journey 100,000 · 1차 | 100회 | 9ms | 20ms | 40ms | 성공 |
| 조회 View, 분산 상품 1개, journey 100,000 · 재검증 | 100회 | 10ms | 28ms | 53ms | 성공 |
| 조회 View, 집중 상품 1개, journey 10,000,000 · 1차 | 100회 | 10ms | 21ms | 38ms | 성공 |
| 조회 View, 집중 상품 1개, journey 10,000,000 · 재검증 | 100회 | 15ms | 56ms | 109ms | 성공 |
| 직접 `sum + GROUP BY`, 집중 상품 | 100회 | 8ms | 22ms | 55ms | 성공 |

집중 상품 쿼리는 initiator 기준 약 303개의 숫자 delta 행을 읽었습니다. 원본 journey가 1,000만 개여도 Summary가 tracking ID 집합을 보관하지 않으므로 조회 비용이 cardinality에 비례해 증가하지 않았습니다. 두 번의 View 측정에서 집중 상품 p50은 10~15ms, p95는 21~56ms였습니다. 로컬 background 작업에 따른 편차는 있었지만 1초 목표에는 충분한 여유가 있습니다. 직접 집계와 비교해도 View가 유의미한 추가 비용을 만든다는 증거는 없었습니다.

문서의 Count Delta 예시도 실제 데이터로 검증했습니다.

| 입력 구성 | source delta | `FINAL` 고유 journey | Summary CLICK | 결과 |
|---|---:|---:|---:|---|
| `j-1: 1, 0`, `j-2: 1` | 2 | 2 | 2 | 성공 |

한 journey에 최초 후보, 더 늦은 중복, 늦게 도착한 더 이른 후보를 각각 한 행씩 동기 INSERT한 결과는 다음과 같습니다.

| 입력 | INSERT 완료 시간 | 누적 count |
|---|---:|---:|
| 최초 후보, `count_delta=1` | 234ms | 1 |
| 더 늦은 중복, `count_delta=0` | 149ms | 1 |
| 늦게 도착한 더 이른 후보, `count_delta=0` | 146ms | 1 |

최종 `FINAL` 대표는 가장 이른 `09:00` 행이었고 누적 summary는 계속 1이었습니다. 이 세 값은 로컬 단일 실행의 관측값이며 운영 INSERT SLA는 더 많은 반복과 동시성으로 다시 측정해야 합니다.

같은 메시지와 `insert_deduplication_token`을 두 번 전달한 재시도 검사도 통과했습니다.

| 검사 | 첫 INSERT | 재시도 INSERT | 저장된 상세 행 | 누적 count | 결과 |
|---|---:|---:|---:|---:|---|
| 동일 메시지·동일 token | 242ms | 151ms | 1 | 1 | 성공 |

이 검사는 네트워크 응답 유실 등에 따른 **같은 메시지 재시도**를 다룹니다. 서로 다른 `message_id`를 가진 동일 이벤트 키의 실제 동시 입력은 애플리케이션 입력 계층이 필요하므로 아직 자동 성능 검사에 포함하지 않았습니다. 해당 경우의 정확성 조건은 위의 partition 단위 직렬화 계약입니다.

### A7-1 결론

- 시간 bucket 보정 없이 누적 exact count를 실시간으로 유지할 수 있고, 중복·순서 역전에서도 정확했습니다.
- 집중 상품 누적 p50은 A4 dedup 직접 조회의 11.398초에서 10~15ms로 줄었고 1초 목표를 충족했습니다.
- 숫자 Summary는 tracking ID cardinality 대신 아직 merge되지 않은 delta 수와 shard 수에 영향을 받습니다.
- 시간별 조회가 없으므로 늦게 도착한 최초 이벤트의 시간 bucket 이동 문제도 A7-1에는 없습니다.
- 가장 큰 전제와 위험은 동일 키의 동시 입력에서 `count_delta=1`이 두 번 나오지 않도록 입력 처리기가 멱등성과 직렬성을 보장해야 한다는 점입니다.
- 1행 동기 INSERT는 이번 재검증에서 146~234ms로 조회보다 훨씬 느렸으므로 async insert와 microbatch의 처리량·Summary 반영 지연을 추가 측정해야 합니다.
- 동일 메시지의 token 기반 재시도는 상세 행과 Summary를 한 번만 반영했습니다. 서로 다른 메시지의 동일 키 경쟁은 partition 단위 직렬화 구현 후 별도 부하 검증이 필요합니다.
- 시간별 요구가 다시 필요해지면 먼저 이벤트별 Replacing 테이블을 `FINAL`로 직접 조회해 1초 목표를 측정하고, 실패할 때만 최근 구간 직접 조회 + 종료 구간 summary 구조를 추가합니다.

## 최초 이벤트 기준

A1~A6의 현재 기준은 `(journey_id, event_kind)`별 최소 `received_at`입니다. A7은 실제 발생 시각을 기준으로 귀속하기 위해 최소 `occurred_at`을 사용합니다. 동일 시각은 `received_at`, `message_id` 순서로 결정하는 방안을 검증합니다.

따라서 standalone 샘플에서 A1~A6의 대표 CLICK은 `demo-02`지만 A7의 대표 CLICK은 발생 시각이 더 빠른 `demo-03`입니다. 누적 count는 같지만 CLICK의 시간 귀속은 10시에서 9시로 이동합니다. A7의 정확성과 성능은 이 정책 차이를 반영한 전용 기대값으로 판정해야 합니다.

`ReplacingMergeTree`의 version은 **더 빠른 `occurred_at`일수록 더 큰 값**이 되도록 변환해야 합니다. A7-1은 1970년 이후 시각을 전제로 millisecond epoch의 bit 반전값을 사용합니다. 동일한 `occurred_at` 후보 사이의 `received_at`, `message_id` tie-break는 아직 version에 포함하지 않았습니다. 누적 count에는 영향이 없지만 상세 대표 행을 결정적으로 반환해야 하는 후속 구조에서는 별도 version 규칙이 필요합니다.

## 전체 A7 후보의 테이블 역할

| 대상 | 엔진 후보 | 키·상태 | 역할 |
|---|---|---|---|
| 이벤트별 최초 테이블 | ReplicatedReplacingMergeTree | `(product_id, journey_id)`, `first_version` | 이벤트 종류별 최초 후보 보관 |
| 누적 행동 지표 | ReplicatedSummingMergeTree | 상품별 숫자 count delta | `count_delta=1`인 이벤트 누적 합산 |
| 누적 발송 지표 | ReplicatedSummingMergeTree | 상품별 `notification_count` delta | S3에서 받은 발송 count 합산 |
| 시간 지표 | ReplicatedReplacingMergeTree | 상품·시간·그룹, `summary_version` | 재계산 결과로 기존 시간 bucket 교체 |

전체 후보의 최종 누적 summary 조회 열은 `mall_id`, `store_id`, `product_id`, 고객 그룹과 각 이벤트 count입니다. A7-1은 우선 상품 누적값만 구현했습니다. `journey_id`는 Summary에 저장하지 않으며 이벤트 입력 단계의 최초 이벤트 판정에만 사용합니다.

## 반드시 분리할 발송 경로

- S3의 `notification_count`는 이미 집계된 수치이므로 여정 단위 unique 계산 없이 누적 summary에 직접 더합니다.
- `notification_raw`는 최초 발생 시각과 고객 그룹 귀속이 필요한 상세 지표에 사용합니다.
- 동일 발송을 두 경로에서 누적 summary에 함께 더하면 이중 집계되므로 입력 계약으로 경로를 구분합니다.
- 같은 S3 파일이나 집계 batch가 재처리될 때 `notification_count`가 다시 더해지지 않도록 batch ID 기반 멱등 처리 또는 version 교체 규칙이 필요합니다.

## 후속 시간 지표의 실시간성과 보정

일반 MV는 새 INSERT 블록만 처리하며 `ReplacingMergeTree`의 나중 merge 또는 `FINAL` 결과 변화를 다시 전달하지 않습니다. 그러므로 이벤트 테이블에 중복 후보가 들어올 때마다 단순 count MV로 시간 summary를 증가시키면 정확하지 않습니다.

누적 숫자 Summary는 `count_delta` 판정으로 중복 증가를 막습니다. 반면 늦게 도착한 더 빠른 이벤트가 최초 시간을 변경하면 기존 시간 bucket을 빼고 새 bucket을 더해야 합니다. A7은 변경된 상품 범위를 `FINAL`로 다시 계산하고, 더 높은 `summary_version`으로 시간 summary를 교체하는 ClickHouse 내부 보정 절차를 후속 검증합니다.

이 보정이 완료되기 전에는 시간 지표가 잠시 이전 bucket을 가리킬 수 있습니다. 따라서 A7의 실시간 목표에는 INSERT 완료 시간, 누적 summary 반영 시간, 시간 summary 보정 완료 시간을 각각 포함해야 합니다.

## 후속 구현·검증 순서

1. A7-1에서 이벤트별 local/Distributed ReplacingMergeTree와 누적 숫자 delta summary를 구현했습니다.
2. 중복·순서 역전 입력에서 `FINAL` 대표 행과 누적 count를 검증했습니다.
3. S3 사전 집계 `notification_count`의 멱등 직접 합산 경로를 별도 검증합니다.
4. 1건 INSERT와 microbatch 입력을 각각 최소 100회 측정합니다.
5. 시간별 요구는 이벤트 테이블 직접 `FINAL` 조회부터 성능을 확인합니다.
6. 직접 조회가 목표를 충족하지 못할 때 최근 구간과 종료 구간을 나눈 summary를 검증합니다.
7. A2~A6과 저장 공간, INSERT 처리량, p50·p95·p99, 최대 메모리를 같은 자원에서 다시 비교합니다.

ClickHouse에 1행씩 동기 INSERT하면 part 생성과 MV 실행 오버헤드가 커집니다. 실제 적재 시험은 `async_insert` 또는 소비자 microbatch를 사용하되, 응답 시점과 summary 반영 시점을 별도로 측정합니다.

## 전체 A7 후속 합격 조건

- `(product_id, journey_id, event_kind)`별 최소 `occurred_at` 이벤트가 선택됩니다.
- 같은 입력을 재처리해도 누적 count가 증가하지 않습니다.
- 더 빠른 이벤트가 늦게 들어오면 누적 count는 유지되고 시간 bucket만 이동합니다.
- S3 발송 count와 raw 발송 이벤트가 이중 집계되지 않습니다.
- 보정 완료 후 누적·시간·고객 그룹 결과가 A7 전용 기대값과 일치합니다.
- 목표 동시성에서 INSERT 지연과 조회 p95·p99가 정한 SLA를 만족합니다.
