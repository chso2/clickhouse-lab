# A7-2: beta 사양 판단을 위한 입력·시간 조회·보정 시험

`main`의 A7-2(`56653f3`)를 기준으로, A7-1 용량 시험에 최초 상태 조회·저장, signed 시간 delta와 동시 상품 보정을 추가했습니다. 현재 beta의 ClickHouse `r7i.large`를 먼저 검증한다는 원칙은 같습니다. [A7-1 측정 기록과 증설 판단 기준](../a7-1-cumulative-summary/CAPACITY.md)은 이전 구조의 근거로 남깁니다.

## 현재 판단: 사양 유지 확정과 실시간 정확성은 미검증

이번 결과는 **beta의 현재 구성을 그대로 유지해도 충분하다는 운영 승인 근거가 아닙니다.** 현재 ClickHouse `r7i.large`와 Keeper `m7i.large`를 다음 AWS 시험의 기준으로 유지하되, 증설 필요 여부는 실제 환경에서 판단합니다.

로컬 CPU 2 제한에서 평균 수준의 400건/초와 조회 50 QPS를 처리했고, 4,000건/초도 batch를 5,000으로 조정해 30초 제공 부하를 처리했습니다. 따라서 월 최대 10억 건이라는 유입량만으로 `r7i.2xlarge` 증설을 필수로 단정할 측정 근거는 없습니다. 반대로 ARM64 공유 VM, 원본 seed 100만~114.4만 행, 단계당 30초 시험은 beta EC2/EBS, 6개월·2년 보관 데이터, 실제 피크와 merge의 장기 안정성을 입증하지 않습니다. Keeper는 시험에서 1개만 사용했으므로 실제 3개 구성의 처리량·장애 허용도 승인하지 않습니다. 노드 그룹 수, 실제 Pod 수와 shard/replica 수는 따로 확인해야 합니다.

**현재 A7-2에는 실제 중복 count 및 보정 중 count 오차가 있습니다.** 아래 표의 `최종 정확성 통과`는 입력 종료 후 불일치 상품을 추가 보정한 결과입니다. 실시간 조회에서 오차가 없다는 뜻이 아닙니다. CPU/RAM 증설은 이 경쟁 조건과 여러 테이블 쓰기의 원자성 문제를 해결하지 않습니다.

## 측정 범위

- 원본: 공통 `schema.sql`의 월 partition 원본을 모두 저장합니다.
- 정답 이벤트와 누적 MV: A7-1 `schema.sql`의 다섯 이벤트 테이블을 사용합니다.
- 최초 상태·시간 Summary: A7-2 `schema.sql`을 그대로 사용합니다. 격리 DB 이름과 단일 서버 DDL의 `ON CLUSTER`만 치환합니다.
- seed: A7-1에 먼저 적재하고, A7-2의 기존 `backfill-from-a7-1.sql`로 최초 상태와 시간 Summary를 만듭니다.
- 실시간: 원본 INSERT → batch의 모든 `(product_id,event_type,journey_id)` 상태를 한 번에 `FINAL` 조회 → 이벤트 다섯 테이블 INSERT/MV → 상태 INSERT → 시간 delta INSERT입니다. 한 batch 안의 동일 키도 입력 순서대로 판정합니다. 입력마다 개별 SELECT를 보내는 consumer보다 왕복을 줄인 시험 구현입니다.
- 보정: 기존 `reconcile-product.sql`을 한 worker에서 지정 상품에 순차 실행합니다. `join_use_nulls=1`, `distributed_foreground_insert=1`을 명시해 보정 INSERT의 완료까지 측정합니다. 기본 5초 간격, 상품 1(집중)·2(일반)이며 전체 변경 상품 backlog 시험은 아닙니다.
- 서비스: 시간 Summary View에서 한 상품의 90일 데이터를 조회합니다. 절반은 집중 상품, 절반은 일반 상품입니다. 원본 `FINAL` 검사는 부하 종료 후 실행하므로 일반 서비스 조회 비용과 분리합니다.

입력 종류는 다섯 종류를 동일 비중으로 생성합니다. synthetic placement는 2,000개, 집중 상품은 유입의 약 50%입니다. 고유 journey/message ID는 hash 문자열을 사용하고 발생 시각은 90일에 분산합니다. 메시지 11개마다 한 개는 앞선 이벤트와 같은 논리 키지만 다른 message ID이며 발생 시각은 한 시간 빠릅니다. A7-1 용량 시험은 1분 빠른 중복이고 시간 분포도 다르므로, 두 결과를 그대로 구조 간 성능 배율로 비교하지 않습니다.

batch writer는 한 개입니다. **실제 queue·consumer의 동시 경쟁 빈도, 운영 peak, campaign 다중 상품 조회, Keeper 3노드 quorum, AWS EC2/EBS, 6개월·2년 데이터는 재현하지 않습니다.** 동일 키의 두 writer 경쟁은 아래 계약 검사에서 별도로 재현합니다.

## 정확성 계약

부하 전 별도의 실행 고유 `_contracts` DB에서 다음을 자동 검증하고 성공하면 제거합니다.

1. `11:20` 최초, `12:30` 후속, `09:10` 늦은 최초: 11시 count는 0, 9시 count는 1, 전체 count는 1입니다. 같은 시간 이동 batch를 같은 token으로 재시도해도 결과가 유지됩니다.
2. 같은 메시지·같은 테이블별 `insert_deduplication_token` 재시도: 최초 상태와 Summary가 한 번만 반영됩니다.
3. 다른 message ID의 두 writer가 barrier 이전에 모두 상태 없음으로 조회: 보정 전 시간 count 2가 실제로 관측되어야 합니다. 상품 보정 후 1이 되고, 입력이 멈춘 상태에서 보정을 다시 실행해도 1을 유지합니다.
4. 원본 이벤트와 최초 상태만 성공하고 시간 delta 쓰기가 빠진 부분 실패: 이벤트 `FINAL` 기반 보정 후 누락된 시간 count가 복구됩니다.

상품 보정은 시간 Summary를 수리합니다. 최초 상태 누락을 채우거나 A7-1 누적 count의 동시 입력 오차를 수리하는 SQL은 아닙니다. 실제 운영에서는 테이블별 안정된 batch token, 장애 시 작업 재실행·변경 상품 기록·단일 보정 실행자를 별도로 구현해야 합니다.

각 부하 종료 후 원본 행 수·다섯 이벤트 `FINAL` 개수·누적 count·모든 상품/시간/이벤트 count를 독립 fixture 기대값과 비교합니다. 최초 상태는 이벤트 `FINAL`과 모든 논리 키 및 최소 시각을 SQL에서 전체 비교합니다.

실시간 writer와 보정 snapshot이 겹치면 최종 일관성 오차가 남을 수 있습니다. 이 경우 **종료 직후 불일치 bucket 수를 먼저 기록**하고 해당 상품만 입력이 멈춘 상태에서 다시 보정합니다. 보정 횟수와 총 소요 시간, 보정 후 정확성을 따로 저장합니다. 최종 정확성 통과는 즉시 강한 일관성 통과를 뜻하지 않습니다. 실시간 오차율·최대 오차 유지 시간의 운영 SLA는 이 시험으로 확정하지 않습니다.

## 확인한 count 문제와 보정의 한계

| 문제 | 확인 수준 | 현재 영향·보정 범위 |
|---|---|---|
| 서로 다른 메시지의 같은 논리 키를 두 writer가 동시에 최초로 판단 | 두 writer가 모두 상태 없음을 읽게 하는 barrier 시험에서 실제 재현 | 정답은 1인데 시간 count가 2. 입력을 멈춘 후 상품 보정으로 1 복구 |
| 실시간 이벤트 쓰기와 상품 보정의 읽기·쓰기가 겹침 | writer 1개인 부하 시험에서도 실제 관측 | 종료 직후 179~2,007개의 시간/event bucket이 기대값과 불일치. 입력 종료 후 추가 보정으로 0 |
| 이벤트·최초 상태 저장 후 시간 delta 저장 누락 | 부분 실패를 주입해 실제 재현 | 시간 count 과소 집계. 이벤트 `FINAL`을 기준으로 시간 Summary 보정 후 복구 |
| A7-1 누적 Summary에도 중복 `count_delta=1`이 전달됨 | 입력 코드와 누적 MV에서 확인되는 구조상 가능성; 경쟁 시험의 누적값은 별도로 검증하지 않음 | 시간 Summary 보정 SQL은 누적 Summary를 수정하지 않으므로 누적 오차 복구를 보장하지 않음 |
| 이벤트는 저장됐지만 최초 상태 저장이 누락됨 | 분리된 INSERT 경로에서 확인되는 구조상 가능성; 이 장애 구간은 미시험 | 시간 보정 SQL은 최초 상태를 채우지 않음. 상태 복구 없이 다음 입력을 다시 최초로 판단할 수 있음 |

### 두 writer의 최초 판정 경쟁

집계 키는 `(event_type, product_id, journey_id)`입니다. writer A와 B가 동일 키에 대해 모두 `first_event_state FINAL`에서 상태 없음으로 읽으면, 서로 다른 INSERT token을 가진 두 입력이 각각 `+1`을 저장합니다. 최초 이벤트 테이블의 `ReplacingMergeTree`가 상세를 한 행으로 정리해도 이미 저장된 시간 delta 두 개를 취소하지 않습니다. Summary는 숫자 합계를 유지하므로 정답 1과 현재 count 2가 남습니다. 실제 계약 시험은 이 오차와 입력 정지 후 `2 → 1` 복구를 확인했습니다.

같은 메시지·같은 token의 즉시 재시도 시험 통과와 이 경쟁은 별개입니다. 서로 다른 token의 같은 논리 키에는 그 재시도 보호가 적용되지 않습니다. 또한 token 기반 중복 제거는 보관된 deduplication 기록 범위에 의존하며 영구 멱등성을 뜻하지 않습니다. [공식 재시도 중복 제거 문서](https://clickhouse.com/docs/concepts/features/operations/insert/deduplicating-inserts-on-retries)의 조건과 window 제한을 운영 재시도 정책에 반영해야 합니다.

### writer 한 개에서도 보정이 count를 중복 반영할 수 있음

현재 실시간 입력은 이벤트, 최초 상태, 시간 delta를 서로 다른 INSERT로 저장합니다. 예를 들어 이벤트 `FINAL` 정답이 10에서 11로 증가했지만 정상 시간 delta `+1`이 아직 저장되지 않은 사이에 보정이 실행되면, 보정은 `expected 11 - actual 10 = +1`을 기록합니다. 이어서 정상 writer가 자신의 `+1`을 저장하면 시간 count는 12가 됩니다. 입력 중 보정이 항상 정답을 만드는 구조가 아닙니다.

이는 독립된 쓰기들이 하나의 원자적 작업으로 묶이지 않은 입력 코드에서 가능한 실행 순서입니다. [ClickHouse INSERT·트랜잭션 보장](https://clickhouse.com/docs/concepts/features/operations/insert/transactions)은 개별 INSERT의 보장과 여러 문장에 대한 트랜잭션을 구분합니다. 현재 Replicated 테이블 경로에 여러 문장을 묶는 트랜잭션은 구현돼 있지 않습니다.

4,000건/초·50 QPS·batch 5,000 단계는 처리량 기준을 통과했지만 **추가 보정 직전에는 2,007개 bucket이 틀렸습니다.** 입력이 멈춘 뒤 해당 상품을 보정하는 데 총 0.126초가 걸렸고 그 후 불일치는 0이었습니다. 이는 정지 후 복구 증거입니다. 입력이 계속되는 운영 환경에서 오차가 사라지는 최대 시간이나 항상 정확한 조회를 입증한 결과가 아닙니다.

### 현재 보정 SQL이 복구하지 않는 대상

`reconcile-product.sql`은 이벤트 `FINAL`과 **시간 Summary** 차이만 계산합니다. A7-1 누적 Summary, 누락된 최초 상태, 아직 저장되지 않은 원본/이벤트, 변경 상품 목록은 복구하지 않습니다. 경쟁 시험의 시간 count 복구를 근거로 이 대상들도 정상이라고 결론내리면 안 됩니다. 누적 count의 동시 입력과 상태 누락 후 재처리는 후속 검증 대상입니다.

성능 harness는 독립 기대값으로 불일치 상품을 찾아 입력 종료 후 추가 보정합니다. 운영에서는 이 기대값이 자동으로 존재하지 않으므로 변경 상품 기록과 재처리 목록을 내구성 있게 관리해야 합니다. 같은 상품의 보정 작업 여러 개가 동시에 같은 차이를 계산하면 보정 delta도 중복될 수 있으므로 단일 실행이 필요합니다. 단일 보정 실행자만 두는 것으로 정상 writer와의 겹침까지 제거되지는 않습니다.

### 운영 적용 전에 충족해야 하는 조건

- 실시간에 정확한 count가 필요하면 동일 키의 최초 판정·갱신을 직렬화하고, 실시간 delta와 상품 보정이 같은 입력을 동시에 반영하지 않도록 상품별 실행 순서나 일관된 처리 기준점을 설계·검증해야 합니다. 키 직렬화만으로 보정 경쟁이 해결되지는 않습니다.
- 보정 전 오차를 허용하면 허용 오차량·최대 유지 시간과 변경 상품 처리 backlog를 정하고, 입력이 계속되는 상태에서 보정이 그 목표를 지키는지 검증해야 합니다. 현재 시험은 이 운영 합격 기준을 정하지 않았습니다.
- 장애 후 이벤트·상태·시간 delta 쓰기를 빠짐없이 완료할 복구 경로와 안정된 테이블별 token을 마련하고, 최초 상태 누락 및 누적 Summary 중복을 따로 시험해야 합니다.
- AWS의 현재 사양에서 실제 보관 데이터 규모와 피크·조회량으로 장시간 측정해야 합니다. 성능 합격과 count 정확성 합격을 각각 판단합니다.

## 재현

Python 표준 라이브러리만 사용하며 [A7-1 harness](../a7-1-cumulative-summary/benchmark-capacity.py)의 HTTP client, arrival QPS scheduler, 자원 수집을 재사용합니다. `cluster1`이 정확히 1 shard × 1 replica인 격리 서버만 허용합니다. 기존 `shop_a7_1`, `shop_a7_2`, `shop_benchmark`는 수정하지 않고 실행 고유 DB를 생성합니다. 실행 중 예외가 발생하면 DB를 남겨 진단할 수 있습니다. 모든 단계 수집을 완료하면 `--cleanup`은 기준 불합격 단계가 있어도 이번 실행 DB를 제거하며, 보고서는 유지합니다.

로컬 배포·정리는 [A7-1 로컬 안내](../a7-1-cumulative-summary/CAPACITY.md#로컬-smoke-test)와 같은 manifest를 사용합니다. localhost port-forward 포트는 사용 가능한 값으로 정합니다.

```bash
kubectl --context kind-clickhouse-lab apply -f examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/capacity-local.yaml
kubectl --context kind-clickhouse-lab -n a7-capacity wait --for=condition=Ready pod/keeper pod/clickhouse --timeout=120s
kubectl --context kind-clickhouse-lab -n a7-capacity port-forward pod/clickhouse 18124:8123 --address 127.0.0.1
```

다른 터미널에서 실행합니다.

```bash
python3 -B examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-2-hourly-summary/benchmark-capacity.py \
  --endpoint http://127.0.0.1:18124 \
  --profile-label local-arm64-2cpu-shared-vm-ch-server-memory-4Gi \
  --seed-rows 1000000 --seed-batch-size 10000 --batch-size 2000 \
  --rates 400 4000 --read-qps 10 50 --duration 30 \
  --reconcile-interval 5 --reconcile-products 1 2 \
  --allowed-drain-seconds 1 --read-p95-ms 1000 --cleanup
```

`--help`로 나머지 인자를 확인합니다. AWS 시험에서는 실제 시험 endpoint와 profile label을 지정하고, 동일 파라미터와 seed/단계 순서로 인스턴스를 비교합니다. `--password-env`는 비밀번호가 든 환경 변수 이름을 받으며 인증 값은 보고서에 저장하지 않습니다. 보고서는 무시되는 `capacity-results/<database>/report.json`에 저장하고, 실행한 소스들의 SHA-256을 포함합니다.

합격 기준은 모든 제공 입력 처리, 종료 후 writer drain 1초 이내, 조회 p95 1초 이내, 요청 오류·누락 0, 최종 정확성 일치, 모니터링·보정 오류 0입니다. 400건/초는 월 최대 10억 건의 평균 약 386건/초에 대응합니다. 4,000건/초와 10·50 QPS는 아직 확정되지 않은 피크·조회량을 시험하는 가정입니다.

writer 처리량에는 fixture 생성, 입력 도착 대기, 원본·이벤트·상태·시간 delta 쓰기를 포함합니다. batch latency는 fixture 생성 이후 원본 쓰기부터 시작합니다. 첫 이벤트의 예정 유입부터 시간 Summary 쓰기 완료까지 지연은 batch를 채우는 시간과 backlog를 포함합니다. 조회 지연은 별도입니다. 최종 보정 지연은 writer drain에 포함하지 않고 정확성 결과에 따로 기록합니다. 기본 batch 2,000은 400건/초에서 5초를 채우고 기다리므로 실제 운영은 최대 batch 대기 시간도 설정해야 합니다.

로컬 ClickHouse는 CPU 2 제한, Pod 메모리 16 GiB 상한, 서버 메모리 4 GiB 상한입니다. ARM64 공유 VM과 emptyDir 디스크이며 전용 16 GiB RAM·Intel CPU·AWS EBS를 재현하지 않습니다. 현재 EC2 사양 유지·증설 결정은 실제 AWS에서 보관 working set과 EBS 지속 한도를 반영한 최소 30분 이상 시험으로 확정합니다.

## 2026-09-28 로컬 실행 결과

[결과 JSON](./capacity-local-results.json)에 파라미터, 소스/원본 보고서 hash, 이미지 digest, 자원 요약, 정합성 계약과 각 단계 결과를 저장했습니다. 첫 실행은 원본 100만 행에서 네 단계를 순차 실행했습니다. 비교 대상인 마지막 단계의 시작 원본은 114.4만 행이므로 batch 5,000 비교 실행도 114.4만 행을 seed로 사용했습니다. 각 단계는 30초 제공 부하이며 같은 CPU 제한과 데이터 생성 규칙입니다. 첫 실행의 4,000건/초 두 단계는 drain 기준을 실패하므로 프로세스가 의도대로 exit 1을 반환합니다.

| 제공 입력/초 | 조회 QPS | batch | 처리량/초, 도착 대기·drain 포함 | 조회 p95 | writer drain | 최종 정확성 | 부하 기준 |
|---:|---:|---:|---:|---:|---:|---|---|
| 400 | 10 | 2,000 | 390.57 | 20.967ms | 0.724초 | 통과 | 통과 |
| 4,000 | 10 | 2,000 | 2,903.06 | 18.247ms | 11.336초 | 통과 | drain 초과 |
| 400 | 50 | 2,000 | 391.65 | 13.625ms | 0.640초 | 통과 | 통과 |
| 4,000 | 50 | 2,000 | 2,832.07 | 19.411ms | 12.372초 | 통과 | drain 초과 |
| 4,000 | 50 | 5,000 | 3,884.87 | 21.517ms | 0.889초 | 통과 | 통과 |

batch 5,000에서는 제공 입력 120,000건과 조회 1,500개를 오류·누락 없이 처리했습니다. batch 처리 p95는 897.159ms, batch 최초 이벤트의 예정 유입부터 시간 Summary 반영 완료까지 p95는 2,181.241ms입니다. 조회 p95가 빠르더라도 지표 반영에는 batch 대기 시간이 있으므로 실시간성 목표가 1초라면 이 batch 설정을 그대로 채택하면 안 됩니다. 조회 목표 1초와 입력 반영 SLA는 별개입니다.

실시간 입력과 상품 보정을 겹친 단계에서는 입력 종료 직후 시간 bucket count 불일치가 남았습니다. 아래 숫자는 `(product, hour, event_type)`별 count가 독립 기대값과 다른 bucket 수이며 잘못된 메시지 수나 서비스 오류율이 아닙니다. 입력이 멈춘 뒤 해당 상품을 다시 보정해 모두 기대값으로 수렴했습니다. 이 결과는 A7-2의 최종 일관성 특성을 확인하며 보정 전 즉시 정확성을 보장하지 않습니다.

| 입력/초 · QPS · batch | 종료 후 재보정 전 불일치 bucket | 종료 후 보정 총 시간 | 보정 후 불일치 |
|---|---:|---:|---:|
| 400 · 10 · 2,000 | 559 | 0.079초 | 0 |
| 4,000 · 10 · 2,000 | 179 | 0.146초 | 0 |
| 400 · 50 · 2,000 | 251 | 0.085초 | 0 |
| 4,000 · 50 · 2,000 | 823 | 0.117초 | 0 |
| 4,000 · 50 · 5,000 | 2,007 | 0.126초 | 0 |

batch 5,000 단계의 동시 보정 p95는 123.672ms, 관측 CGroup CPU는 평균 31.699%·최대 44.773%(2 CPU quota 기준), CGroup 메모리 최대 약 1,034 MiB였습니다. 2초 표본이므로 짧은 CPU spike를 놓칠 수 있습니다. active parts는 표본 첫 52개에서 마지막 54개였으며 30초만으로 장기 merge 안정성을 보장하지 않습니다.

이 단계에서 원본 126.4만 행의 압축 크기는 약 51.49 MiB, 최초 상태 약 31.91 MiB, 시간 Summary 약 2.16 MiB였습니다. 이벤트별 정답 테이블·누적 Summary도 함께 저장하므로 전체 저장량을 모두 합쳐야 합니다. synthetic 열·ID 분포의 압축률을 실제 6개월·2년 EBS 용량으로 확정하지 않습니다.

상태 조회 비용도 별도로 봐야 합니다. batch 2,000의 4,000건/초·10 QPS 단계는 상태 lookup 60회에서 합계 약 5,918만 행을 읽었습니다. 이번 넓은 batch의 조건은 한 번에 많은 상품/키를 조회하므로 seed 100만 행의 상태 테이블 상당 부분을 읽었습니다. Summary 조회가 빠르다는 사실만으로 더 큰 최초 상태 working set에서도 현재 CPU를 유지할 수 있다고 결론내리면 안 됩니다. 실제 보관량과 키 편중으로 상태 lookup의 읽은 행·CPU·지연을 재측정해야 합니다.

현재 로컬 결과에는 CPU/RAM 증설 없이 batch 조정으로 제공 고부하를 처리한 사례가 있습니다. 따라서 `r7i.2xlarge` 증설을 요구할 근거는 아직 없습니다. 현재 `r7i.large`를 AWS 시험의 기준으로 두고, 더 큰 working set·피크에서 CPU/EBS/메모리 병목이 확인되면 동일 부하의 `r7i.xlarge`부터 비교합니다. Keeper의 사양·quorum 판단은 별도 Keeper 지표와 실제 3노드 구성에서 검증해야 합니다.
