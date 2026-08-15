# AI 맞춤 레터링

AI는 글자 내용을 결정하지 않고 원본과 가장 가까운 한글 시각 형태를 설계한다. 확정 한국어는
불변 입력이며, 고정 한글 폰트나 일반 글꼴 조판은 최종 후보로 사용하지 않는다.

## 입력 고정

각 번역 영역마다 다음 파일과 값을 해시로 고정한다.

- 원본 mip 0의 글자 확대 crop
- AI로 원문만 제거해 승인한 깨끗한 배경
- 정확한 한국어 문자열과 반복 횟수
- 원본 bbox, 회전, 읽는 방향, 포장 면과 artwork 방향
- `old_text`, `new_text`, `editable`, `protected`, `seam_guard` 마스크

배경 제거와 레터링 생성을 같은 호출에서 수행하지 않는다. 배경이 승인되면 문구·스타일을
수정해도 재생성하지 않는다.

## 생성

이미지 생성 도구에 원본 crop, 고정 배경과 확정 문구를 함께 제공하고 다음을 원본에서
추론하게 한다.

- 글자의 실루엣과 획 성격
- 크기, 장평, 기울기, 회전, 기준선, 자간과 행간
- 직선·원호·곡면 배치
- 채움색, 외곽선, 그림자, 입체감과 레이어 순서
- 잉크 번짐, 마모, 노이즈, 표면 반사와 가장자리 선명도

AI가 배경·그림·로고 도형·UV 경계를 다시 그리지 못하게 수정 범위를 문자 영역으로 제한한다.
가능하면 한 호출에서 영역별 복수 후보를 만들고, 동일한 반복 로고는 한 번 승인한 패치와
마스크를 좌표 변환만 적용해 재사용한다.

작은 본문은 해당 영역을 확대해 생성하고 원본 해상도로 한 번만 축소한다. 전체 Texture2D를
확대·축소하지 않는다.

## 선별

먼저 자동 검사로 다음 후보를 폐기한다.

- 확정 한국어와 OCR이 다름
- 글자 누락·중복·추가 문자·워터마크가 있음
- bbox·방향·반복 횟수가 다름
- 마스크 밖 또는 보호·seam 영역이 바뀜
- 원문 로고의 글자·외곽선·그림자 실루엣이 남음

남은 후보는 원본과 같은 배율의 영역 비교 시트에서 다음을 각각 판정한다.

- 실루엣·획 성격
- 크기·장평·여백
- 회전·기울기·기준선·읽는 방향
- 자간·행간·곡선
- 색·외곽선·그림자·입체감
- 마모·노이즈·인쇄 질감과 주변 표면의 연속성

어느 항목도 원본과 맞지 않으면 통과시키지 않는다. AI가 정확한 철자를 만들지 못해도 고정
폰트로 대체하지 말고 해당 영역만 다시 생성하거나 `review`/`block`으로 남긴다.

## 합성과 캐시

선택한 레터링 패치에서 승인한 `new_text` 마스크 안의 픽셀만 고정 배경에 합성한다. 비문자
픽셀과 AI가 임의로 다시 그린 배경은 버린다. 다음 산출물을 품목·영역별로 해시 고정해
수정 반복 시 재사용한다.

- 원본 스타일 참조 crop
- 깨끗한 배경
- 후보 비교 시트
- 선택한 레터링 패치와 마스크
- 이미지 생성 모델·설정 서명
- OCR 보고서와 시각 판정

`edit_plan.data.compositor`에는 다음 형태의 증거를 기록한다.

```json
{
  "mode": "ai-reference-lettering",
  "fixed_font_used": false,
  "background_locked": true,
  "regions": [
    {
      "region_id": "front-brand-01",
      "exact_text": "한글 제품명",
      "bbox": [120, 84, 418, 148],
      "rotation_deg": 0,
      "direction": "left-to-right",
      "model_signature": "image-model-and-settings",
      "candidate_count": 4,
      "ocr_exact_match": true,
      "style_match_passed": true,
      "style_checks": {
        "shape_matched": true,
        "size_matched": true,
        "direction_matched": true,
        "spacing_matched": true,
        "effects_matched": true,
        "surface_integration_matched": true,
        "old_logo_silhouette_absent": true
      },
      "source_style_reference": {"path": "...", "sha256": "..."},
      "clean_background": {"path": "...", "sha256": "..."},
      "candidate_sheet": {"path": "...", "sha256": "..."},
      "selected_lettering": {"path": "...", "sha256": "..."},
      "lettering_mask": {"path": "...", "sha256": "..."}
    }
  ]
}
```

최종 `post_visual.data`에는 글자 형태·크기·방향·간격·효과·표면 통합과 원문 실루엣 제거를
각각 `true`로 기록하고, 현재 후보 SHA에 묶인 비교 시트를 증거로 남긴다.
