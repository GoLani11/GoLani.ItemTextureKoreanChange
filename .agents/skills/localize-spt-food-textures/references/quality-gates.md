# 품질 게이트

판정은 `pass`, `block`, `review`, `error`로 구분한다. 자동 다음 단계는 `pass`만 허용한다.
검사 누락과 도구 오류를 합격으로 처리하지 않는다.

## G0 원본·연결

- 원본 bundle과 Texture2D SHA-256, 크기, 포맷, 밉 수를 고정한다.
- target Texture2D가 정확히 하나여야 한다.
- 실제 Material PPtr에서 diffuse, normal, gloss와 모든 공유 소비자를 확인한다.
- 기존 승인·derived·bundle은 현재 입력 해시가 모두 일치할 때만 재사용한다.

하나라도 미확정이면 `block` 또는 `error`다.

## G1 원본 판독·번역

- OCR과 Codex 독립 판독이 모든 보이는 문자 영역을 각각 기록해야 한다.
- 각 영역의 문자열, bbox, 회전, 방향, 면, 반복 횟수와 그림 방향을 합의해야 한다.
- 교차검증 conflict와 unresolved가 0이어야 한다.
- 원문 뜻과 최종 번역이 확정되고 profile의 `exact_text`가 모두 포함되어야 한다.
- 읽지 못한 문구를 추측하거나 생략하면 안 된다.

OCR 무검출은 통과 근거가 아니다.

## G2 편집 설계

- 마스크는 원본과 같은 크기의 lossless 단일 채널이어야 한다.
- `old_text ∪ new_text ⊆ editable`이어야 한다.
- `editable ∩ protected = ∅`이어야 한다.
- `seam_guard ⊆ protected`이어야 한다.
- 마스크 SHA와 한글 조판 recipe를 고정해야 한다.

## G3 후보 이미지

- 원본과 폭·높이·비율·색 모드가 같고 리사이즈 이력이 없어야 한다.
- 알파가 바이트 단위로 같아야 한다.
- editable 밖 RGB 변경 픽셀은 0이어야 한다.
- protected와 seam guard 안 변경 픽셀은 0이어야 한다.
- 글자 위치·크기·회전·읽는 방향·색·시각적 위계가 승인 명세와 같아야 한다.
- 비문자 로고, 그림, 주름, 오염, 반사, 마모와 절취선을 보존해야 한다.
- 흐림, 과도한 샤픈, 추가 장식, 워터마크와 임의 문구가 없어야 한다.

전역 edge F1이나 전체 평균 점수만으로 통과시키지 않는다.

## G4 생성 후 문자 검사

- 결정적 조판 recipe의 문자열과 횟수가 명세와 같아야 한다.
- 후보 OCR과 Codex 시각 비교를 둘 다 수행해야 한다.
- 금지된 라틴·키릴 잔상, 한국어 누락·오자·중복이 없어야 한다.
- OCR의 probable·detector-only는 `review`, 엔진 오류는 `error`다.
- 허용 외국어·숫자는 target, 정확한 문자열과 ROI에 한정해야 한다.

## G5 Diffuse·Normal·Gloss

- 파일명 family가 아니라 실제 Material 연결을 사용해야 한다.
- 같은 문자 효과는 같은 확정 글리프 마스크·좌표·회전을 사용해야 한다.
- 평면 인쇄는 N/G를 원본 그대로 보존해야 한다.
- 기존 원문 relief·광택 제거는 명시한 old-text 영역 안에서만 수행해야 한다.
- normal packing·극성·벡터 길이와 gloss 사용 채널을 유지해야 한다.
- 허용 마스크 밖 변경은 0이어야 한다.
- 공유 보조맵의 모든 소비자를 확인하고 충돌이 없어야 한다.
- 사광, normal-only, gloss-only 렌더에서 기존 외국어가 보이지 않아야 한다.

## G6 밉·압축

- diffuse, normal, gloss에 역할별 축소 방식을 사용해야 한다.
- 각 UV island를 padding하고 필수 밉까지 seam이 이어져야 한다.
- 모든 밉에서 글자 ROI의 획·경계·방향·중복을 검사해야 한다.
- normal은 매 단계 재정규화하고 gloss는 선형 scalar로 처리해야 한다.
- 전역 MAE 외에 글자·seam ROI의 p95, p99, 최대 오차와 edge 보존을 검사해야 한다.
- 임계값은 정상 no-op 왕복과 의도적 불량 fixture로 교정해야 한다.

## G7 번들

- Texture2D width, height, format, mip count와 complete image size가 같아야 한다.
- stream path, offset, size가 같아야 한다.
- 대상 payload 밖 serialized object와 resource byte가 같아야 한다.
- UnityFS header, block·directory table, padding과 전체 크기가 같아야 한다.
- 결과 bundle을 다시 열어 대상과 모든 밉을 추출할 수 있어야 한다.
- 현재 승인·derived 입력 SHA가 bundle report와 일치해야 한다.

## G8 실제 렌더

- 실제 셰이더·Material에서 정면·회전·seam 근접면을 검사해야 한다.
- 정면광, 좌우·상하 사광, 그림자·역광을 검사해야 한다.
- 근거리와 필수 밉 거리, diffuse/normal/gloss 진단 pass를 검사해야 한다.
- 영문 요철·광택, 한글 중복, seam, 흐림과 마젠타 오류가 없어야 한다.

캡처를 수행하지 못하면 자동 release는 불가하다.

## G9 Release·배포

- source, profile, 판독, 번역, 마스크, 후보, D/N/G, 밉, bundle과 렌더 보고서 SHA를
  release 기록에 묶어야 한다.
- 어느 입력이든 변경되면 downstream 결과와 기존 승인을 무효화해야 한다.
- 실패·검토 대기·누락·오래된 캐시가 하나라도 있으면 release를 만들지 않는다.
- manifest에 명시된 정확한 bundle만 배포한다.
- 실제 설치는 사용자 요청과 백업·복원 계획이 있을 때만 수행한다.
