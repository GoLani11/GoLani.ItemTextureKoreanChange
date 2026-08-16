# 작업 흐름

## 1. 범위와 원본 고정

`profiles/food/collection.json`의 target ID, bundle key와 Texture2D 이름을 기준으로 삼는다.

```bash
<python> localize.py --spt-root <SPT 경로> inventory
<python> localize.py --spt-root <SPT 경로> extract
<python> localize.py uv-review
<python> localize.py status
```

원본 PNG와 source bundle의 SHA-256·크기·포맷·밉 수를 기록하고 실제 Material PPtr에서 D/N/G
연결과 공유 소비자를 확인한다. 원본 추출물은 수정하지 않는다.

## 2. Codex 비전 우선 원본 판독

원본 mip 0을 `view_image`의 원본 상세도로 먼저 본다. 전체 배치와 연결된 라벨 면을 이해한 뒤
각 대상 영역을 `source_visual.data.regions`에 기록한다.

- 문자열 또는 읽을 수 없음 표시
- 원본 픽셀 bbox/polygon, 회전, 읽는 방향, 면과 UV 섬
- 제품 그림·로고·화살표의 방향, seam·절취선·접힘과의 관계
- 글꼴 인상과 스타일 계열, 획 굵기·대비·끝 모양
- ink bbox, 글자 높이·폭, 장평·기울기, 기준선·정렬·자간·행간
- 채움·외곽선·그림자·입체감·마모·노이즈·인쇄 질감과 레이어 순서

`source_visual.data.vision_first`는 `true`여야 한다. 영역별 `needs_ocr_fallback`은 확대해도
문자가 모호하거나 누락 가능성이 있을 때만 `true`로 한다. 읽을 수 없는 부분을 문맥으로
지어내지 않는다.

## 3. 조건부 원본 OCR

모든 원본에 OCR을 일괄 실행하지 않는다. `needs_ocr_fallback: true`인 영역이 하나라도 있을
때만 `source_visual.data.ocr_fallback_required: true`로 기록하고 그 영역 crop에 OCR을 실행한다.
OCR에는 문자열·bbox·회전·방향·engine·model signature·confidence를 남긴다.

OCR을 사용한 영역만 `cross_validation`에서 시각 판독과 대조한다. 문자열·위치·회전·방향·
반복 횟수 중 하나라도 충돌하거나 OCR 오류가 나면 중단한다. 명확한 영역은 OCR이 없어도
분석 게이트를 통과할 수 있다.

## 4. 번역과 글자 명세

각 영역에 원문, 뜻, 확정 한국어, 횟수, bbox, 회전, 읽는 방향, 면과 시각적 위계를 기록한다.
profile의 `exact_text`를 글자 그대로 사용한다. 보이는 대상 문구가 profile에 없다면 생략하지
말고 profile을 먼저 보완한다.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through analysis
```

## 5. 편집 설계

원본과 같은 크기의 lossless 단일 채널 마스크를 만든다.

- `old_text`: 지워야 할 원문 픽셀
- `new_text`: 확정된 한글 글리프 픽셀
- `editable`: 안티앨리어싱을 포함한 유일한 변경 허용 영역
- `protected`: 그림, 로고 도형, 주름, 오염, 반사, 구조와 보존 문구
- `seam_guard`: UV 경계와 밉 번짐 보호 띠

`old_text ∪ new_text ⊆ editable`, `editable ∩ protected = ∅`,
`seam_guard ⊆ protected`를 만족시키고 크기와 SHA-256을 기록한다.

## 6. 연결된 라벨 면 단위 생성

[ai-lettering.md](ai-lettering.md)를 따른다. 원본 전체는 보존 기준으로 제공하고, 한 번에 이해할
수 있는 연결된 라벨 면을 하나의 편집 단위로 삼는다. 같은 면의 문구를 글자별로 쪼개 여러 번
생성하지 않는다. 최소 두 후보를 의무화하지 않고 첫 결과를 바로 검사한다. 실패 원인이
명확하면 그 한 항목만 고쳐 한 번 재시도할 수 있다.

이미지 생성은 정확한 한국어로 문자만 바꾸고 배경·그림·로고 도형·재질·오염·주름·UV 경계는
그대로 보존해야 한다. 일반 한글 폰트 오버레이, 전체 Texture2D 재생성, 원문 폭에 맞춘 사후
왜곡과 전체 리사이즈를 금지한다.

각 영역은 원본과 같은 배율에서 다음 typography lock을 통과해야 한다.

- 글꼴 인상·스타일·획·끝 모양·장평·기울기 일치
- ink bbox 높이·폭·영역 점유율 차이 각각 10% 이내
- 기준선·정렬·자간·행간·곡선 흐름과 여백 일치
- 읽는 방향 완전 일치, 회전 차이 2° 이내
- 채움·외곽선·그림자·입체감·마모·인쇄 질감·레이어 순서 일치

한글 음절을 읽기 어렵게 찌그러뜨리지 않는다. 글자 수가 달라도 전체 시각 무게와 실루엣을
원문에 가깝게 설계한다. 승인한 문자 패치·마스크 안 픽셀만 원본에 합성하고 나머지는 원본
픽셀을 유지한다.

## 7. 결과 OCR과 최종 시각 비교

후보를 stage하기 전에 두 검사를 별도로 수행한다.

1. 결과 OCR: 확정 한국어의 철자·횟수, 누락·중복과 금지 외국어 잔상을 검사한다.
2. Codex 시각 비교: 원본과 후보를 같은 배율로 비교해 typography lock과 비문자 보존을
   영역별로 판정한다.

픽셀 검사는 크기·색 모드·알파 동일, 리사이즈 없음, editable 밖 변경 0, protected와
seam guard 변경 0을 요구한다. OCR 통과만으로 시각 품질을 통과시키지 않는다.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through candidate
<python> localize.py stage <target-id> <candidate.png>
```

## 8. D/N/G 정렬

실제 Material graph에 연결된 맵을 대상으로 한다. 평면 인쇄는 normal/gloss 원본을 보존한다.
기존 외국어 relief나 광택이 있을 때만 각 보조맵의 재질 전용 `old_text` 마스크 안에서
중립화하고, 새 한글 효과가 필요하면 diffuse와 같은 `new_text` 좌표·회전을 사용한다.
공유 맵 소비자가 충돌하면 자동 파생하지 않는다.

## 9. 밉·압축·번들 검증

- diffuse는 선형광에서 축소 후 sRGB로 되돌린다.
- normal은 벡터 평균 후 매 단계 재정규화한다.
- gloss는 선형 스칼라로 축소한다.
- 모든 밉에서 글자 ROI, seam, normal 각도와 gloss 오차를 검사한다.
- 임시 bundle을 다시 열어 Texture2D metadata, stream과 payload 밖 byte를 검사한다.

## 10. 실제 렌더와 release

동일 셰이더에서 정면·회전·seam 근접면, 정면광·사광·그림자, 근거리·원거리와 D/N/G 진단
캡처를 원본과 A/B 비교한다. 캡처가 불가능하면 release를 통과시키지 않는다. source, profile,
판독, 번역, 마스크, 후보, D/N/G, 밉, bundle과 렌더 SHA를 묶는다.

설치는 사용자가 명시적으로 요청한 경우에만 백업·복원 계획을 확인한 뒤 수행한다.
