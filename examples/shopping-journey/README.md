# 쇼핑몰 구매 여정 이벤트 예제

기존 clickhouse-lab의 추가 예제입니다. **단일 노드 기준 모델**과 **클러스터 조회 구조별 비교 케이스**를 분리합니다. 기존 앱과 매니페스트는 유지합니다.

## 구성

```text
shopping-journey/
├─ standalone/                       # 기존 DDL·샘플·쿼리 보존
│  ├─ README.md
│  ├─ schema/
│  │  ├─ clickhouse.sql
│  │  └─ rds-mysql.sql
│  ├─ sample-data.sql
│  └─ queries.sql
├─ cluster/
│  ├─ README.md
│  ├─ common/
│  ├─ a1-event-hll_rds/
│  ├─ a2-event-hll_dedup-query/
│  ├─ a4-event-query/
│  ├─ a5-dedup-query/
│  ├─ a6-summary-query/
│  └─ a7-event-hll_summary-query/
└─ docs/
   ├─ architecture.md
   └─ ddl-notes.md
```

## 실험 범위

모든 케이스의 입력은 정규화된 shopping_events입니다. JSON 수집·파싱·규칙 평가·매핑 JOIN은 제외합니다. 각 케이스는 필요한 하위 상태·통계 테이블만 추가합니다. 전체 ERD의 상위 입력 경로는 참고용입니다.

## 조회 대상과 방식

- **event**: 정규화된 구매 여정 이벤트인 shopping_events. JSON 원시 로그가 아닙니다.
- **dedup**: 최초 이벤트 집계 상태인 first_event_states. 조회 시 상태를 합칩니다.
- **summary**: dedup에서 파생한 사전 집계 테이블.
- **상세**: 최초 이벤트 기준의 시간별·고객 그룹별 통계.

| 케이스 | 전체 누적 조회 | 상세 조회 |
|---|---|---|
| [A1](./cluster/a1-event-hll_rds/README.md) | event → HLL | RDS → 통계 조회 |
| [A2](./cluster/a2-event-hll_dedup-query/README.md) | event → HLL | dedup → 직접 집계 |
| [A4](./cluster/a4-event-query/README.md) | event → 최초 선택·집계 | event → 최초 선택·집계 |
| [A5](./cluster/a5-dedup-query/README.md) | dedup → 직접 집계 | dedup → 직접 집계 |
| [A6](./cluster/a6-summary-query/README.md) | summary → 통계 조회 | summary → 통계 조회 |
| [A7](./cluster/a7-event-hll_summary-query/README.md) | event → HLL | summary → 통계 조회 |

A3은 비교 대상에서 제외했습니다. RDS는 클러스터 비교 케이스 중 A1에만 사용합니다. standalone의 RDS DDL은 기존 모델 보존용입니다.

## 현재 상태

- [standalone](./standalone/README.md): shopping_events부터 시작하는 테이블 3개·MV 2개·View 1개와 샘플을 제공합니다. 서버 실행 검증은 아직 없습니다.
- [cluster](./cluster/README.md): 케이스별 디렉터리와 구현·검증 범위를 정리했습니다. **클러스터용 DDL·실행 스크립트는 아직 작성하지 않았습니다.**
- [ERD](./docs/architecture.md): 기존 구조를 쇼핑몰 도메인으로 설명합니다. 모든 클러스터 케이스에 RDS가 필요하다는 의미는 아닙니다.
- [DDL 가정](./docs/ddl-notes.md): 현재 단일 노드 SQL의 타입·키·미구현 부분을 설명합니다.

다음 구현은 A2·A4·A5의 클러스터 DDL과 비교 쿼리부터 진행합니다. A1 배치와 A6·A7 실시간 summary 갱신은 별도 구현 대상입니다.
