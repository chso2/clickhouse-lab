# A7-1: 현재 beta 사양을 유지할 수 있는지 검증

현재 beta의 ClickHouse는 `r7i.large`(2 vCPU, 16 GiB), Keeper는 `m7i.large`(2 vCPU, 8 GiB)입니다. 월 유입량만으로 `r7i.2xlarge`로 증설할 근거는 없습니다. 현재 사양에서 목표를 충족하는지 먼저 측정하고, 같은 부하에서 인스턴스 변경으로 병목이 해소되는지 비교합니다. 노드 그룹 3개와 실제 3레플리카는 별개입니다.

## 부하와 판정 기준

- 월 최대 10억 건 / 30일 = 평균 약 386건/초. 시험 단계는 400, 4,000건/초입니다. 10배 단계는 실제 피크 예측이 아니라 검증 가정입니다.
- 기본 조회 부하는 10, 50 QPS이며 운영 요구가 확정된 값이 아닙니다. 목표 QPS와 p95는 시험 전에 지정합니다. 기본 p95 목표는 기존 A7의 1초 목표를 사용합니다.
- 유입은 VIEW/CART/CLICK/PURCHASE/NOTIFY로 나눕니다. 동일 journey의 placement는 고정합니다. 기본 placement 2,000개와 50% 집중 키를 사용하고, 실제 cardinality/편중에 맞춰 바꿉니다.
- 메시지 11개마다 한 개는 이전 이벤트 키의 중복이면서 더 이른 발생 시각을 가집니다. 원본은 모두 저장하고, 최초 이벤트 조회 및 INSERT를 한 writer가 순차 실행합니다. 한 batch 내부 중복도 처리합니다.
- 원본은 공통 `schema.sql`, 이벤트/MV/summary는 A7-1 `schema.sql`을 사용합니다. HTTP endpoint는 `cluster1`이 정확히 1 shard × 1 replica인 격리 시험 서버여야 합니다.
- 합격 조건: 입력 오류 0, offered rows 전부 처리, 허용 drain 시간 이내, 조회 오류/동시성 한도로 탈락한 요청 0, 지정 p95 이내, 원본 행 수·이벤트 `FINAL` 고유 수·상품별 summary가 독립 fixture oracle과 일치, 모니터링 오류 0.
- 최대 메모리, query CPU, 읽은 행, active parts, 진행 중 merge, 복제 대기열과 CGroup 지표도 기록합니다. 짧은 시험의 합격만으로 merge의 장기 안정성이나 운영 처리량을 보장하지 않습니다.

## 실행

Python 표준 라이브러리만 사용합니다. 대상은 반드시 별도의 시험 배포로 지정합니다. 기존 `shop_a7_1`/`shop_benchmark`를 변경하지 않고 실행별 `a7_capacity_*` DB를 만듭니다. 생성한 DB만 `--cleanup`으로 정리할 수 있습니다. 오류가 나면 DB를 유지하고 보고서에 오류를 기록합니다.

```bash
python3 -B examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/benchmark-capacity.py \
  --endpoint http://127.0.0.1:18123 \
  --profile-label 'AWS r7i.large; actual Pod limits recorded separately' \
  --seed-rows 1000000 --rates 400 4000 --read-qps 10 50 \
  --duration 1800 --read-p95-ms 1000 --cleanup
```

`--password-env`로 비밀번호가 들어 있는 환경 변수 이름을 지정할 수 있습니다. 값이나 endpoint 인증 정보를 결과 파일에 저장하지 않습니다. 실제 Pod의 CPU/메모리 requests/limits, EC2 타입, AZ, EBS 용량/IOPS/throughput 및 이미지 digest를 별도로 함께 기록합니다. `max_threads`는 쿼리별 병렬성일 뿐 실제 CPU 제한을 대체하지 않습니다.

입력 batch를 기다리는 시간, 원본 쓰기, 최초 판정 SELECT, 이벤트 INSERT+MV를 포함한 전체 writer 처리량을 측정합니다. batch latency는 원본 쓰기부터 시작하며 fixture 생성 시간은 writer 전체 처리량에는 포함됩니다. `oldest_event_to_batch_commit_latency`는 batch의 첫 이벤트가 유입될 예정인 시점부터 batch 완료까지로, batch를 채우는 대기 시간과 writer가 밀린 시간도 포함합니다. 이 값에 대한 운영 freshness 목표는 아직 정해지지 않았으며 조회 p95와 구분해 판단합니다. 조회는 고정 arrival QPS로 보내며 작업자 한도를 넘으면 누락 요청을 기록하여 시험을 실패시킵니다. SQL별 서버 시간과 HTTP 왕복 시간을 분리합니다. 부하 종료 시 writer가 밀린 입력을 얼마나 늦게 완료했는지도 기록합니다.

결과는 `capacity-results/<database>/report.json`에 저장합니다. correctness용 `FINAL` 검사는 부하 종료 후 실행합니다. 단계별 데이터는 누적되므로 인스턴스 A/B 비교 시 seed/단계 순서/파라미터를 동일하게 유지하고, 새 DB에서 다시 실행합니다. 모든 로그·지표 수집은 시험 DB/쿼리 ID로 제한하며 CGroup 지표는 해당 ClickHouse 컨테이너의 자원입니다.

## 로컬 smoke test

`capacity-local.yaml`은 별도 namespace, ClickHouse 1개, Keeper 1개, CPU 2개 제한과 메모리 16 GiB 상한을 사용합니다. 원래 랩의 데이터/설정을 변경하지 않습니다. 기존 VM 전체가 약 16 GiB를 공유하므로 request는 1 GiB이며 ClickHouse 서버 메모리 상한은 4 GiB로 낮췄습니다. **전용 16 GiB RAM, Intel CPU, AWS EBS/네트워크를 재현하지 않습니다. 로컬 결과를 `r7i.large` 처리량으로 해석하지 않습니다.**

```bash
kubectl --context kind-clickhouse-lab apply -f examples/shopping-journey/cluster/a7-event-replacing_summary-count/a7-1-cumulative-summary/capacity-local.yaml
kubectl --context kind-clickhouse-lab -n a7-capacity wait --for=condition=Ready pod/keeper pod/clickhouse --timeout=120s
kubectl --context kind-clickhouse-lab -n a7-capacity port-forward pod/clickhouse 18123:8123
```

다른 터미널에서 위 Python 명령을 실행하되 `--profile-label local-2cpu-shared-vm --duration 30`처럼 로컬임을 표시합니다. 정리가 필요하면 **시험 전용 namespace만** 삭제합니다. emptyDir 데이터가 함께 제거됩니다.

```bash
kubectl --context kind-clickhouse-lab delete namespace a7-capacity
```

## 증설을 결정하는 증거

1. 동일 데이터·입력·조회 부하에서 현재 `r7i.large`의 CPU 포화/스로틀링과 지연/merge backlog 증가가 함께 나타나고, `r7i.xlarge`에서 해소되면 4 vCPU 증설 근거입니다.
2. 메모리 부족/OOM 또는 메모리 한도에 의한 쿼리 실패가 실제 working set에서 발생하면 RAM 증설을 검토합니다. 월 원본 행 수 자체는 RAM 요구량이 아닙니다.
3. EBS 지연과 처리량/IOPS 사용량, `EBSIOBalance%`/`EBSByteBalance%`를 함께 관측합니다. `r7i.large`의 지속 EBS 기본 한도는 81.25 MB/s와 3,600 IOPS이므로 볼륨을 키우는 것만으로 해결되지 않을 수 있습니다. 실제 지속 요구량이 한도를 넘는지 확인합니다.
4. 저장 공간만 부족하면 EBS 확장/원본 tiering을 먼저 검토합니다. 저장 증가량은 모든 저장 테이블 합계 / 유입 수로 측정하며 2년 보관은 최대 240억 건, 최소 6개월은 최대 60억 건입니다.
5. AZ 장애 허용은 replica/AZ 배치 변경의 근거입니다. 인스턴스 크기를 올릴 근거와 구분합니다. Keeper는 자체 CPU·메모리·디스크 지연/처리 대기열에 병목이 있을 때 조정합니다.

AWS 확정 시험은 seed를 실제 보관 working set에 가깝게 늘리고 최소 30분 이상 지속하며, 운영 입력 계약(consumer 재할당/재시도/replica 전환)과 원본 저장 경로를 검증합니다. 이 harness는 단일 writer의 batched 최초 판정 모델이며 실제 consumer 애플리케이션 및 Kafka 성능, AWS AZ 장애, 두 해의 상세 조회를 검증하지 않습니다.

공식 사양: [R7i CPU/RAM/EBS](https://docs.aws.amazon.com/ec2/latest/instancetypes/mo.html), [M7i](https://docs.aws.amazon.com/ec2/latest/instancetypes/gp.html).

## 2026-09-28 로컬 실행 결과

[측정 결과 JSON](./capacity-local-results.json)에 실행 파라미터, 이미지 digest, 서버 시간, 정확성 결과와 자원 요약을 보존했습니다. 첫 실행은 원본 100만 행에서 시작해 4개 단계를 순차 실행했습니다. 마지막 단계의 시작 원본은 114.4만 행이므로 batch 2,000 비교 실행도 114.4만 행으로 시작했습니다. 각 부하 단계는 30초입니다.

| 제공 입력/초 | 조회 QPS | batch | 실제 writer 처리량/초 | 조회 p95 | 종료 후 writer drain | count 정확성 | 부하 기준 |
|---:|---:|---:|---:|---:|---:|---|---|
| 400 | 10 | 1,000 | 395.28 | 20.85ms | 0.358초 | 통과 | 통과 |
| 400 | 50 | 1,000 | 395.09 | 11.99ms | 0.373초 | 통과 | 통과 |
| 4,000 | 50 | 1,000 | 2,778.54 | 13.47ms | 13.188초 | 통과 | 입력 drain 초과 |
| 4,000 | 50 | 2,000 | 3,948.08 | 13.37ms | 0.395초 | 통과 | 통과 |

실제 처리량은 입력 도착 대기와 종료 후 drain 시간을 포함하므로 제공 rate보다 조금 낮습니다. 이번 판정은 30초 제공 입력을 모두 처리하고 종료 후 drain이 1초 이내이며 조회 p95가 1초 이내인 조건입니다. 장기 지속 처리량을 측정한 값은 아닙니다.

동일 2 vCPU에서 batch만 2,000행으로 바꾸어 12만 건 제공 입력과 1,500개 조회 요청을 오류/누락 없이 처리했습니다. 이때 CGroup CPU 사용률은 평균 26.06%, 최대 29.99%(2 vCPU quota 기준), 관측 최대 CGroup 메모리는 약 775 MiB였습니다. 자원 표본 주기는 2초라 더 짧은 spike를 놓칠 수 있습니다. batch 처리 시간 p95는 약 384ms, batch 첫 이벤트의 예정 유입부터 완료까지 p95는 약 908ms였습니다. 낮은 입력량에서 같은 2,000행 batch를 채우면 기다리는 시간이 길어지므로 운영에서는 최대 batch 대기 시간도 함께 정해야 합니다.

이 시험에서는 더 큰 CPU/RAM을 쓰지 않고도 writer 지연을 해소했습니다. 따라서 현재 결과는 `r7i.2xlarge` 증설의 근거가 아닙니다. 인스턴스는 현재 사양을 기준으로 AWS에서 먼저 재현합니다. 실제 beta의 2년 working set, EBS 지속 성능, Intel CPU, consumer/Kafka, replica 전환은 검증하지 않았습니다.
