# 클러스터 조회 구조 비교

[전체 안내](../README.md) · [공통 실험 기준](./common/README.md)

폴더 이름은 **누적 조회 경로_상세 조회 경로**를 표현합니다. 두 조회가 같은 경로이면 하나만 적습니다.

| 디렉터리 | 전체 누적 | 시간·그룹별 |
|---|---|---|
| [a1-event-hll_dedup_rds](./a1-event-hll_dedup_rds/README.md) | event → HLL | dedup → 배치 → RDS 통계 조회 |
| [a2-event-hll_dedup-count](./a2-event-hll_dedup-count/README.md) | event → HLL | dedup → 직접 count |
| [a3-event-count](./a3-event-count/README.md) | event → 최초 선택 후 count | event → 최초 선택 후 count |
| [a4-dedup-count](./a4-dedup-count/README.md) | dedup → 직접 count | dedup → 직접 count |
| [a5-dedup_summary-count](./a5-dedup_summary-count/README.md) | dedup → summary → count | dedup → summary → count |
| [a6-event-hll_dedup_summary-count](./a6-event-hll_dedup_summary-count/README.md) | event → HLL | dedup → summary → count |

## 구현 순서

1. A2·A3·A4: 정규화된 이벤트 입력부터 분산 저장·복제·상태 집계와 직접 조회를 비교.
2. A1: RDS 배치를 연결해 기존 방식과 비교.
3. A5·A6: summary의 실시간 갱신·보정 방식이 정해진 뒤 구현.

현재는 디렉터리와 설계 범위만 준비했습니다. 빈 SQL을 실행 가능한 구현처럼 배치하지 않았으며, 실제 DB에 적용한 변경도 없습니다.
