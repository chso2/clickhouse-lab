# A1 · a1-event-hll_rds

[전체 비교](../README.md) · [공통 기준](../common/README.md)

| 구분 | 경로 |
|---|---|
| 전체 누적 | event → HLL |
| 시간·고객 그룹별 | RDS → 통계 조회 |
| 생성 흐름 | event → dedup → 배치 → RDS |
| 필요한 저장 대상 | event, dedup, recent, RDS |
| 계획된 독립 DB | shop_a1 |

## 구현할 범위

RDS 스키마, 배치 대상·재처리·결과 교체 정책.

현재는 케이스 정의만 작성했습니다. 클러스터용 DDL, 배포 스크립트, 실행 결과는 없습니다.

MySQL 참고 DDL은 [standalone](../../standalone/schema/rds-mysql.sql)에 있습니다. MySQL용이며 배치 로더는 아직 없습니다.
