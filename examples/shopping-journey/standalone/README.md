# 단일 노드 기준 모델

기존 clickhouse-lab에 추가하는 독립 SQL 예제입니다. 기존 push-click-service나 Kubernetes 매니페스트를 교체하지 않습니다. 예제 데이터베이스는 `shop_analytics`, 선택적인 MySQL 통계 스키마는 `shop_reporting`입니다.

[전체 케이스 안내](../README.md)

## 무엇을 다루는가

쇼핑몰 → 매장 → 상품에 연결된 구매 여정에서 알림 발송, 링크 클릭, 상품 조회, 장바구니 담기, 구매 이벤트를 수집합니다. 알림 발송은 고객 행동이 아니므로 전체를 **구매 여정 이벤트**라고 부릅니다.

```text
정규화·속성 보강이 완료된 공통 테스트 데이터
                    ↓
             shopping_events
                    ├─ 누적 HLL 조회
                    ├─ recent_event_keys
                    └─ first_event_states
                               ↓
                         first_events
                               ↓
                  시간별·고객 그룹별 조회
```

상위 입력을 포함한 전체 참고 구조와 RDS 배치는 [ERD](../docs/architecture.md)에 설명되어 있습니다.

## 파일

| 파일 | 내용 |
|---|---|
| [schema/clickhouse.sql](./schema/clickhouse.sql) | ClickHouse 테이블 3개, MV 2개, 일반 View 1개 |
| [schema/rds-mysql.sql](./schema/rds-mysql.sql) | 선택적 RDS 통계 테이블 예제 |
| [sample-data.sql](./sample-data.sql) | 정규화된 가상 구매 여정 이벤트 6행 |
| [queries.sql](./queries.sql) | 누적 HLL, 시간별·고객 그룹별 정확 집계 |
| [DDL 가정과 범위](../docs/ddl-notes.md) | 타입·키·미구현 경로·정책 변경 설명 |

## 실습 순서

선택한 **실습용 ClickHouse 단일 서버**에 연결한 클라이언트에서 다음 순서로 파일을 실행합니다.

1. schema/clickhouse.sql — 빈 shop_analytics 데이터베이스에 예제 스키마 생성.
2. sample-data.sql — shopping_events에 정규화된 샘플 입력.
3. queries.sql — 원본·대표 개수와 각 통계 확인.

연결 명령은 사용하는 실습 서버의 주소·인증에 맞춥니다. 이 예제는 ON CLUSTER, Distributed, Replicated 엔진을 적용하지 않았으므로 현재 여러 노드의 모든 Pod에 각각 실행하지 않습니다.

RDS 파일은 MySQL용이며 ClickHouse에서 실행하지 않습니다. RDS 테이블은 생성만으로 채워지지 않으며 배치 로더는 아직 없습니다.

MySQL 8.0.16 이상을 대상으로 합니다. 배치 연결의 시간대는 UTC로 설정하고, 계산한 전체 카운트로 기존 값을 교체해야 합니다. 재시도마다 더하는 방식은 중복 집계를 일으킵니다.

## 샘플 기대 결과

다음은 설계상 기대값이며 서버 실행 결과는 아닙니다.

| 항목 | 기대값 |
|---|---|
| 원본 행 수 | 6 |
| 고유 여정·이벤트 조합 | 5 |
| 09시 | NOTIFY 1 |
| 10시 | CLICK 1, VIEW 1, CART 1, PURCHASE 1 |
| 고객 그룹 1 | 5개 이벤트 종류 각각 1 |
| 고객 그룹 2 | VIEW·CART·PURCHASE 각각 1 |

CLICK은 발생 시각 10시의 이벤트가 먼저 수집되고, 09:30 이벤트가 나중에 수집됩니다. 현재 예제는 **최초 수집 기준**을 유지하므로 대표 CLICK은 10시입니다. 최소 발생 시각 기준으로 전환하면 이 기대값도 달라집니다.

같은 샘플을 다시 입력하면 원본 행은 증가합니다. 동일한 키와 내용이 유지되므로 대표 이벤트의 고유 건수는 그대로여야 합니다. 물리적 INSERT 중복 차단 기능을 구현한 예제는 아닙니다.

샘플은 최초 5행과 늦게 수집된 CLICK 1행을 별도 INSERT로 입력합니다. 서로 다른 입력 블록 사이에서도 대표 CLICK이 유지되는지 확인하기 위한 구성입니다. 위 기대값은 샘플을 별도로 계산해 확인했으며, 실제 DB 실행 검증은 아직 하지 않았습니다.

고객 그룹 조회는 RDS 그룹 키에 맞춰 매장 내 **전체 상품·전체 기간**을 집계합니다. 상품별 조회 결과를 이 그룹 키에 저장하면 다른 상품의 카운트를 덮어쓸 수 있습니다. HLL 조회는 근사값이므로 정확 집계와 구분합니다.

## 현재 구현 범위

- 문서, 테이블 정의, 원본 → recent/최초 상태 MV, 일반 조회 View, 샘플과 조회 SQL을 제공합니다.
- 실험은 shopping_events에서 시작합니다. JSON 수집·파싱·규칙 평가·매핑 JOIN 테이블과 MV는 범위 밖이며 이 DDL에는 없습니다.
- 성능 측정 대상은 shopping_events 이후의 저장·집계·조회이며 전체 수집 파이프라인 성능이 아닙니다.
- 독립 API 앱, RDS 배치, 실시간 통계 갱신기, 성능 테스트 결과는 아직 없습니다.
- 최초 선택은 received_at 기준이고 동률은 message_id로 결정합니다. 최소 occurred_at 기준 개선안과 혼동하지 않습니다.
- 새로운 SQL은 DB에 적용하거나 실행 검증하지 않았습니다.
