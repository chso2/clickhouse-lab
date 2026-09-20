# A5 · a5-dedup-query

[전체 비교](../README.md) · [공통 기준](../common/README.md)

| 구분 | 경로 |
|---|---|
| 전체 누적 | dedup → 직접 집계 |
| 시간·고객 그룹별 | dedup → 직접 집계 |
| 생성 흐름 | event → dedup |
| 필요한 저장 대상 | event, dedup |
| 계획된 독립 DB | shop_a5 |

## 구현할 범위

클러스터용 테이블·로컬 MV, 상태 병합 후 모든 집계.

현재는 케이스 정의만 작성했습니다. 클러스터용 DDL, 배포 스크립트, 실행 결과는 없습니다.
