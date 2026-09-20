# 클러스터 조회 구조 비교

[전체 안내](../README.md) · [공통 실험 기준](./common/README.md)

폴더 이름은 **누적 조회 경로_상세 조회 경로**를 표현합니다. 두 조회가 같은 경로이면 하나만 적습니다.

| 디렉터리 | 전체 누적 | 시간·그룹별 |
|---|---|---|
| [a1-event-hll_rds](./a1-event-hll_rds/README.md) | event → HLL | RDS → 통계 조회 |
| [a2-event-hll_dedup-query](./a2-event-hll_dedup-query/README.md) | event → HLL | dedup → 직접 집계 |
| [a4-event-query](./a4-event-query/README.md) | event → 최초 선택·집계 | event → 최초 선택·집계 |
| [a5-dedup-query](./a5-dedup-query/README.md) | dedup → 직접 집계 | dedup → 직접 집계 |
| [a6-summary-query](./a6-summary-query/README.md) | summary → 통계 조회 | summary → 통계 조회 |
| [a7-event-hll_summary-query](./a7-event-hll_summary-query/README.md) | event → HLL | summary → 통계 조회 |

## 구현 순서

1. A2·A4·A5: 정규화된 이벤트 입력부터 분산 저장·복제·상태 집계와 직접 조회를 비교.
2. A1: RDS 배치를 연결해 기존 방식과 비교.
3. A6·A7: summary의 실시간 갱신·보정 방식이 정해진 뒤 구현.

현재는 디렉터리와 설계 범위만 준비했습니다. 빈 SQL을 실행 가능한 구현처럼 배치하지 않았으며, 실제 DB에 적용한 변경도 없습니다.
