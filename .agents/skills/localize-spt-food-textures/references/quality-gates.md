# 품질 게이트

판정은 `pass`, `block`, `review`, `error`로 구분한다. 자동 다음 단계는 `pass`만 허용한다.
검사 누락과 도구 오류를 합격으로 처리하지 않는다.

## G0 원본·연결

- 원본 bundle과 Texture2D SHA-256, 크기, 포맷, 밉 수를 고정한다.
- 실제 Material PPtr에서 diffuse, normal, gloss와 모든 공유 소비자를 확인한다.
- 기존 산출물은 현재 입력 해시가 모두 일치할 때만 재사용한다.

## G1 비전 우선 판독·번역

- Codex가 원본 mip 0을 원본 상세도로 먼저 보고 편집 허용 목록의 모든 대상 영역을 기록해야 한다.
- 제외한 미세 인쇄는 bbox와 종류가 보호 영역으로 기록되고 전사·OCR 대상에서 빠져야 한다.
- 각 영역에 문자열·bbox·회전·방향·면·그림 방향과 원본 typography signature가 있어야 한다.
- `needs_ocr_fallback`이 있는 영역에만 원본 OCR과 교차검증을 요구한다.
- OCR fallback의 conflict와 unresolved는 0이어야 한다.
- 원문 뜻과 확정 한국어가 profile의 `exact_text`와 일치해야 한다.

명확한 원본에 OCR을 반복 실행하지 않는다. 모호한 문구를 OCR 없이 추측하면 실패다.

## G2 편집 설계·생성 방식

- 모든 마스크는 원본과 같은 크기의 lossless 단일 채널이어야 한다.
- `old_text ∪ new_text ⊆ editable`, `editable ∩ protected = ∅`,
  `seam_guard ⊆ protected`여야 한다.
- `compositor.mode`는 `vision-panel-localization`, `fixed_font_used`는 `false`,
  `single_pass_panels`는 `true`여야 한다.
- 연결된 라벨 면은 한 생성 호출에서 편집하고 글자별 호출을 금지한다.
- 비정방향 문구에 정방향 작업 패널을 사용했다면 crop 원점·여백·원본 회전각·정방향 변환과
  역변환이 기록되어야 한다. 생성·OCR은 정방향 좌표에서, 승인 글자 패치·마스크 복원은 원본
  좌표에서 수행되어야 하며 원본·최종 Texture2D 전체 회전·재표본화 이력이 없어야 한다.
- 각 영역에 모델 서명, 1회 이상의 생성 시도, 원본/결과 typography signature, 원본 스타일
  참조, 생성 패널과 패널 OCR, 선택 글자 패치와 마스크를 해시 고정해야 한다.
- 배경 복원은 `old_text` 안에서만 이루어져야 하며 원본·마스크·복원 설정과 외부 patch/model
  signature를 포함한 캐시 fingerprint가 기록되어야 한다.
- 결과 OCR에서 모든 확정 한국어의 철자·지정 횟수가 맞을 때까지 실패 원인 하나씩 보정한
  생성·합성 기록이 있어야 한다. 패널 OCR은 시각 채택 생성 패널 SHA에, 후보 OCR은 공식
  합성·픽셀·시각 사전 검사를 통과한 SHA에 묶여야 하며 OCR 실패본을 최종 후보로 승인하면 안 된다.
- 연결된 라벨 면의 생성은 기본 2회 이내여야 한다. 이를 넘겼다면 사용자의 명시적 추가 요청을
  증거로 남겨야 한다.

## G3 typography lock

- 글꼴 인상, 획 굵기·대비·끝 모양과 스타일 계열이 일치해야 한다.
- 장평·기울기·실루엣과 시각적 위계가 일치해야 한다.
- ink bbox 높이·폭·영역 점유율 차이가 각각 10% 이하여야 한다.
- 기준선·정렬·자간·행간·곡선 흐름과 여백이 일치해야 한다.
- 읽는 방향은 완전히 같고 회전 차이는 2° 이하여야 한다.
- 채움·외곽선·그림자·입체감·마모·인쇄 질감과 레이어 순서가 일치해야 한다.
- 한글이 자연스럽게 읽혀야 하며 음절을 강제로 찌그러뜨리면 안 된다.
- bbox 수치만 맞추기 위한 과도한 음절 간격이나 장식 획 확장을 허용하지 않는다.

전역 유사도나 주관적인 “비슷함” 하나로 위 항목을 대신하지 않는다.

## G4 후보 이미지·결과 OCR

- 원본과 폭·높이·비율·색 모드가 같고 리사이즈 이력이 없어야 한다.
- 알파가 바이트 단위로 같아야 한다.
- editable 밖 RGB, protected와 seam guard 변경 픽셀은 0이어야 한다.
- 결과 OCR에서 확정 한국어의 철자·횟수가 맞고 누락·중복과 `editable` 안의 금지 외국어
  잔상이 없어야 한다. `protected`의 외국어 미세 인쇄는 원본 픽셀이 같으면 허용한다.
- 결과 OCR 문자열은 NFC와 줄바꿈 형식만 정규화하고 공백·줄바꿈·구두점·숫자·단위를 보존한
  완전일치여야 한다. 부분 문자열·접두/접미·편집거리 일치를 통과로 인정하지 않는다.
- Codex 최종 시각 비교가 현재 후보 SHA에 묶여 typography lock과 비문자 보존을 모두
  통과해야 한다.
- 흐림, 과도한 샤픈, 추가 장식, 워터마크와 임의 문구가 없어야 한다.

## G5 Diffuse·Normal·Gloss

- G4 후보가 현재 SHA로 통과하기 전에는 이 게이트를 실행하지 않는다.
- 파일명 family가 아니라 실제 Material 연결을 사용해야 한다.
- 보조맵 전체 이미지 생성 이력이 없어야 하고 원본 N/G를 불변 base로 사용해야 한다.
- 같은 문자 효과는 승인된 `selected_lettering` 연속 알파 하나에서 파생해야 한다. 영역별
  selected-lettering/mask SHA가 candidate와 같아야 한다.
- 해상도나 UV ST가 다르면 binary `new_text`를 resize하지 말고 같은 master alpha/SDF를 Mesh
  UV·texel-center 규칙으로 재래스터화해야 한다.
- 평면 인쇄는 N/G payload를 byte 보존해야 한다.
- 기존 원문 relief·광택 제거는 재질 전용 old-effect 안에서만 수행하고 중립 base의 입력
  fingerprint를 고정해야 한다.
- 새 요철·광택은 확인된 channel semantics와 algorithm signature로만 절차 파생해야 한다.
- normal packing·극성·벡터 길이와 gloss 사용 채널을 유지하고 사용하지 않은 채널 변경은
  0이어야 한다.
- aux 기준 중심 오차 0.5 texel, bbox 오차 1 texel 이내, 회전 오차 0°여야 한다.
- 공유 보조맵 소비자를 모두 확인하고 사광 진단에서 편집 대상의 기존 외국어가 보이지 않아야
  한다. 보호 미세 인쇄 효과는 원본과 같아야 한다.

현재 producer가 `neutralize_and_derive`를 구현하지 않았다면 수동 생성 맵을 대신 통과시키지
않고 material을 `block`한다.

## G6 밉·압축

- diffuse, normal, gloss에 역할별 축소 방식을 사용해야 한다.
- 각 UV island를 padding하고 모든 필수 밉에서 글자 ROI와 seam을 검사해야 한다.
- normal은 매 단계 재정규화하고 gloss는 선형 scalar로 처리해야 한다.
- 변경하지 않은 채널은 같은 source mip 0에서 만든 no-op 역할별 mip chain과 pre-compression
  값이 같아야 한다. 압축 뒤 차이는 편집과 교차하는 BC 블록 안에서만 평가한다.
- 글자·seam ROI의 p95, p99, 최대 오차와 edge 보존을 검사해야 한다.

## G7 번들

- Texture2D width, height, format, mip count와 complete image size가 같아야 한다.
- stream path, offset, size와 대상 payload 밖 byte가 같아야 한다.
- 결과 bundle을 다시 열어 대상과 모든 밉을 추출할 수 있어야 한다.

## G8 실제 렌더

- 실제 셰이더에서 정면·회전·seam 근접면과 정면광·사광·그림자·필수 밉 거리를 검사한다.
- D/N/G 진단에서 편집 대상의 외국어 효과, 중복, seam, 흐림과 정렬 오류가 없어야 한다.
  보호 미세 인쇄는 원본과 동일해야 한다.

캡처를 수행하지 못하면 자동 release는 불가하다.

## G9 Release·배포

- source, profile, 판독, 번역, 마스크, 후보, D/N/G, 밉, bundle과 렌더 보고서 SHA를 묶는다.
- repack 보고서에 현재 `workspace/derived.json` SHA를 포함하고 release·배포까지 다시 확인한다.
- 어느 입력이든 바뀌면 downstream 결과와 기존 승인을 무효화한다.
- 실패·검토 대기·누락·오래된 캐시가 하나라도 있으면 release를 만들지 않는다.
- 실제 설치는 사용자 요청과 백업·복원 계획이 있을 때만 수행한다.
