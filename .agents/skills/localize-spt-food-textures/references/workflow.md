# 작업 흐름

## 1. 범위와 원본 고정

`profiles/food/collection.json`의 target ID, bundle key와 Texture2D 이름을 기준으로 삼는다.

```bash
<python> localize.py --spt-root <SPT 경로> inventory
<python> localize.py --spt-root <SPT 경로> extract
<python> localize.py status
```

다음 조건을 모두 확인한다.

- `missing_bundles`가 비어 있다.
- target과 추출 Texture2D가 1:1로 연결된다.
- 원본 PNG와 source bundle SHA-256, 크기, 포맷, 밉 수를 기록한다.
- Material의 `_MainTex`, `_BumpMap`, `_SpecMap` 등 실제 PPtr를 따라 D/N/G 소비 관계를
  기록한다. 이름이나 접미사만으로 family를 추정하지 않는다.
- 공유 normal/gloss의 모든 diffuse 소비자를 기록한다.

원본 추출물은 수정하지 않는다. 기존 승인본·derived·bundle이 있어도 입력 해시가 현재
원본과 같은지 증명하기 전에는 재사용하지 않는다.

## 2. OCR 1차 판독

원본을 원본 해상도에서 검사한다. OCR 도구가 제공되면 회전·타일·복수 배경과 최소 두
엔진을 사용한다. 도구가 없거나 실행 오류가 나면 `source_ocr=error`로 기록하고 멈춘다.

각 검출에 다음 정보를 남긴다.

- 안정적인 `region_id`
- 검출 문자열과 문자권(`latin`, `cyrillic`, `digits`, `symbol`, `unknown`)
- 원본 픽셀 기준 bbox 또는 polygon
- 시계 방향 회전각, 읽는 진행 방향과 포장 면/UV 섬
- 엔진, 모델·설정 서명, confidence
- 원본 이미지 SHA-256

OCR이 아무것도 찾지 못했다는 사실은 외국어가 없다는 증거가 아니다. 약한 검출,
detector-only와 엔진 간 불일치는 삭제하지 말고 검토 대상으로 보존한다.

## 3. Codex 2차 독립 판독

OCR 결과의 문자열을 정답으로 옮기지 말고 원본 이미지를 다시 확대해 직접 판독한다.
다음을 OCR과 별도로 `source_visual.data.regions`에 기록한다.

- 실제 보이는 문자열 또는 읽을 수 없음 표시
- 문자 영역의 bbox/polygon, 회전과 읽는 방향
- 앞면·옆면·뚜껑·바닥 등 보이는 면과 UV 섬
- 제품·그림·로고·화살표의 방향
- 절취선, 접힘, 이음선과 문자 사이 거리
- 글자 높이, 기준선, 정렬, 색, 마모·인쇄 질감

읽을 수 없는 부분을 문맥으로 지어내지 않는다.

## 4. 교차검증과 텍스트 명세

OCR과 독립 판독의 모든 region ID 합집합을 하나씩 대조한다. 다음 중 하나라도 다르면
`cross_validation.data.conflicts`에 남기고 중단한다.

- 문자열 또는 문자권
- 영역 위치·크기
- 회전각·읽는 방향·포장 면
- 반복 횟수
- 그림·로고 방향과의 관계

충돌이 없을 때 각 영역에 원문, 한국어 의미, 최종 표기, 횟수, bbox, 회전, 방향, 면과
시각적 위계를 기록한다. profile의 `exact_text`가 모두 포함되는지 확인하되, profile에 없는
보이는 문구가 발견되면 생략하지 말고 profile을 먼저 보완한다.

분석 기록을 검사한다.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through analysis
```

## 5. 편집 설계

원본과 같은 크기의 lossless 단일 채널 마스크를 만든다.

- `old_text`: 지워야 할 원문 픽셀
- `new_text`: 확정된 한글 글리프 픽셀
- `editable`: 배경 복구·안티앨리어싱을 포함한 유일한 변경 허용 영역
- `protected`: 로고, 그림, 주름, 오염, 반사, 구조와 보존 문구
- `seam_guard`: UV 경계, 절취선과 밉 번짐 보호 띠

`old_text ∪ new_text ⊆ editable`, `editable ∩ protected = ∅`,
`seam_guard ⊆ protected`를 만족시킨다. 모든 마스크의 크기와 SHA-256을 기록한다.

## 6. 생성과 결정적 합성

AI 이미지 편집은 `old_text` 안의 배경 복구 초안에만 사용한다. 다음 순서로 최종 후보를
재현할 수 있어야 한다.

```text
원본 + editable 안의 배경 복구 patch + 확정 좌표의 한글 glyph layer
```

폰트 파일 SHA, shaping 엔진·버전, 글리프, 크기, 자간, 색, 회전과 합성 좌표를 기록한다.
원본 전체 재생성, 자동 리사이즈, 잘못된 크기의 후보를 LANCZOS로 맞추는 처리를 금지한다.

## 7. 생성 후 이중 검증

후보를 stage하기 전에 다음 두 검사를 별도로 수행한다.

1. 후보 OCR: 금지된 라틴·키릴 잔상, 한글 누락·중복·오자를 탐지한다.
2. Codex 시각 비교: 원본과 후보를 같은 배율로 비교하고 글자·그림 방향, 색, 질감, 흐림,
   seam·절취선, 로고와 비문자 구조를 확인한다.

픽셀 검사에서는 크기·알파 동일, 리사이즈 없음, editable 밖 변경 0, protected와 seam
변경 0을 요구한다. OCR 무검출만으로 통과시키지 않는다.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through candidate
<python> localize.py stage <target-id> <candidate.png>
```

현재 `localize.py stage`의 전역 edge 검사는 전체 재생성을 차단하기에 충분하지 않다.
위 마스크·픽셀 검사를 별도로 통과하지 못하면 stage 명령이 성공해도 승인하지 않는다.

## 8. D/N/G 정렬

실제 Material graph에서 연결된 맵을 대상으로 한다.

- 평면 인쇄는 normal/gloss 원본을 그대로 보존한다.
- 기존 외국어 relief나 광택이 있을 때만 각 보조맵의 명시적 `old_text` 영역을 중립화한다.
- 새 한글 효과가 필요한 경우 diffuse와 같은 `new_text` 마스크·좌표·회전을 사용한다.
- normal은 packing을 해석한 의미 공간에서 벡터를 재정규화한다.
- gloss는 실제 사용 채널만 변경하고 영역 밖 byte를 보존한다.
- 공유 맵의 소비자가 서로 다른 디자인을 요구하면 자동 파생하지 않는다.

정면, 좌우·상하 사광, normal-only, gloss-only 진단 이미지에서 기존 외국어가 보이지 않고
한글 효과가 diffuse와 정렬되는지 검사한다.

## 9. 밉·압축·번들 검증

- diffuse는 선형광에서 축소 후 sRGB로 되돌린다.
- normal은 벡터 평균 후 매 단계 재정규화한다.
- gloss는 선형 스칼라로 축소한다.
- UV 섬별 padding과 각 밉의 seam guard를 적용한다.
- 모든 밉에서 글자 ROI, seam, normal 각도, gloss 오차를 분리해 검사한다.
- 압축 전후 전역 MAE뿐 아니라 ROI p95·p99·최대 오차와 edge 보존을 검사한다.
- bundle의 Texture2D metadata, stream 경로·offset·size, UnityFS 레이아웃과 대상 payload 밖
  byte가 원본과 같은지 확인한다.

임시 번들을 다시 열어 실제 mip payload를 추출한 뒤 검사한다. 결과 파일이 존재한다는
이유만으로 성공으로 간주하지 않는다.

## 10. 실제 렌더와 release

가능하면 실제 SPT 클라이언트의 동일 셰이더에서 다음을 고정 캡처한다.

- 정면과 회전면, seam 근접면
- 정면광, 좌우·상하 사광, 그림자와 역광
- 근거리와 필수 밉이 선택되는 거리
- diffuse-only, normal-only, gloss-only, 최종 합성

원본과 패치본을 같은 실행·카메라·광원에서 A/B 비교한다. 캡처가 불가능하면 검사를
통과시키지 말고 `review` 또는 `error`로 남긴다.

release는 source, profile, 판독, 마스크, 후보, D/N/G, 밉, bundle, 렌더 보고서의 SHA를
묶어 기록한다. 어느 입력이든 바뀌면 기존 승인과 downstream 산출물을 무효화한다.

설치는 사용자가 명시적으로 요청한 경우에만 백업과 복원 계획을 확인한 뒤 수행한다.
