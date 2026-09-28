# A7-2: beta 사양 판단을 위한 입력·시간 조회·보정 시험

`main`의 A7-2(`56653f3`)를 기준으로, A7-1 용량 시험에 최초 상태 조회·저장, signed 시간 delta와 동시 상품 보정을 추가했습니다. 현재 beta의 ClickHouse `r7i.large`를 먼저 검증한다는 원칙은 같습니다. [A7-1 측정 기록과 증설 판단 기준](../a7-1-cumulative-summary/CAPACITY.md)은 이전 구조의 근거로 남깁니다.

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
