# v0.4.26 옵션별 노출상품 ID 보완 결과

대상: 등록상품 `16407315252`, 로켓그로스 옵션 `96294832572`.

## 확인 결과

- 2026-11-17 실제 로켓그로스 목록·상세 응답 확인: 대상 옵션은 존재하지만 해당 옵션 객체에 productId가 없습니다.
  목록 상단 productId `9740760821`은 상품 묶음 값이라 옵션 링크로 사용하지 않았습니다.
- 2026-11-18 수정한 매출내역 조회 실행: 2026-05-16~2026-11-17, 186일·125회 요청 완료.
  날짜 범위 오류 없이 완료했지만 대상 vendorItemId의 productId 연결은 발견하지 못했습니다.
- 현재 운영 DB에는 대상의 노출ID `9816792954`가 `manual` 출처로 저장되어 있습니다.
  이 수동 확정 링크를 그대로 보존합니다:
  https://www.coupang.com/vp/products/9816792954?vendorItemId=96294832572
- 이 링크의 ID·옵션 조합과 보존 동작을 검증했습니다. API가 이 연결을 독립적으로 확인해 준 것은 아닙니다.
  고객용 상품 페이지의 실제 표시 내용은 이번 검증에 포함하지 않았습니다.

## 변경 파일

- `coupang_manager.pyw`: 원본 로켓그로스 목록·상세 교차 확인, 동일 옵션만 병합,
  기존 ID·링크 우선, 빈 ID만 보완, 충돌 보류, 불확실한 과거 이력 복원 방지.
- `exposure_lookup.py`: 옵션별 연결 추출 및 목록 페이징 검증.
- `revenue_exposure.py`: 7일 날짜 분할·어제 종료 제한·정확한 매출 옵션 매칭.
- `product_table.py`: 미확인 옵션에 '링크 확인 필요' 표시.
- `tests/test_exposure_lookup.py`, `tests/test_revenue_exposure.py`, `tests/test_exposure_preservation.py`:
  날짜 경계·다른 옵션·다른 판매자·페이지 오류·충돌·기존 링크 보존 테스트.
- `tests/test_api_query.py`, `tests/test_query_integration.py`: 공통 조회·캐시·통합 테스트 갱신.
- `README.txt`, `VERSION_HISTORY.txt`, `QUERY_LAYER.md`: 변경 동작과 경계 기록.

## 검증

- 전체 테스트 216개 통과, GUI 테스트 포함.
- 운영 DB 복사본에서 상품 2,284개를 유지하고, 기존에 저장된 비어 있지 않은 노출ID와 링크가 보존됨을 대조했습니다.
- 매출 6,365건, 주문 항목 23,308건, 재고 스냅샷 202건, 입고중 기록의 전체 내용 해시가 수정 전후 동일했습니다.
- 대상의 실제 목록·상세 응답을 복사본에 적용한 후에도 수동 링크가 그대로 유지됐습니다.
- 별도의 미확인 상태를 복사본에서 만들어 화면의 '링크 확인 필요' 표시를 확인했습니다.
- 운영 DB에는 수정하지 않았고, API에는 조회 요청만 보냈습니다.
- 공통 요청 계층의 호출 간격·재시도·429 처리 코드는 변경하지 않았습니다.

## 남은 제한

대상 링크의 자동 확인은 미해결입니다. 목록·상세에 옵션별 productId가 없고 조회한 매출내역에도 연결이 없습니다.
기존 수동 확정값은 보존하며, 수동값도 없는 옵션은 다른 상품으로 연결하지 않습니다.

## 참고한 공식 문서

- [로켓그로스 상품 목록 페이징](https://developers.coupang.com/ko/api/rocket-growth/product-list-paging-query-rocket-growth-rocket-growthmarketplace-hybrid-products)
- [매출내역 조회](https://developers.coupang.com/ko/api/settlement/sales-detail-query?market=KR)
