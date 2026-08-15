# 판독·검증 기록

품목별 기록은 `workspace/reviews/<target-id>/review.json` 한 파일을 중심으로 관리한다.
워크스페이스는 Git에 추가하지 않는다.

## 생성과 검사

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py init <target-id>
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py check \
  workspace/reviews/<target-id>/review.json --through analysis
```

`init`은 profile의 target ID, bundle key, Texture2D 이름과 확정 문구를 복사하고 모든 단계를
`pending`으로 만든다. 기존 파일은 덮어쓰지 않는다.

단계 결과와 증거 파일은 다음 명령으로 기록한다. `--data`는 해당 단계 측정값을 담은 JSON
객체이며, `--evidence`는 여러 번 지정할 수 있다. 도구가 프로젝트 상대 경로와 현재 SHA를
자동 기록한다.

```bash
<python> .agents/skills/localize-spt-food-textures/scripts/review_record.py stage \
  workspace/reviews/<target-id>/review.json source_ocr --status pass \
  --data workspace/reviews/<target-id>/source-ocr-data.json \
  --evidence workspace/reviews/<target-id>/source-ocr-report.json
```

`check`의 단계는 다음과 같다.

- `analysis`: 원본 OCR, Codex 독립 판독, 교차검증과 번역
- `candidate`: 편집 마스크, 픽셀 검사, 후보 OCR과 후보 시각 비교
- `material`: 실제 D/N/G 연결·공유 관계·좌표 정렬
- `release`: 모든 밉, 압축·번들, 실제 렌더와 release 해시

요구 단계가 `pass`가 아니거나 `unresolved`가 비어 있지 않으면 명령이 실패한다.

## 좌표 규칙

- 모든 bbox는 원본 mip 0 픽셀의 `[x0, y0, x1, y1]`로 기록한다.
- 원점은 좌측 상단이며 `x1`, `y1`은 영역에 포함하지 않는다.
- 회전은 화면에서 시계 방향 각도(`rotation_deg`)로 기록한다.
- `direction`은 `left-to-right`, `right-to-left`, `top-to-bottom`, `bottom-to-top` 중 하나다.
- `face`에는 `front`, `side`, `lid`, `bottom` 또는 확인한 UV island ID를 기록한다.
- `artwork_direction`에는 제품 그림·화살표·로고가 향하는 방향과 기준물을 기록한다.

## 판독 영역

`source_ocr.data.detections`와 `source_visual.data.regions`는 서로 독립적으로 작성한다.
각 항목에는 다음 필드가 필요하다.

```json
{
  "region_id": "front-brand-01",
  "text": "DEVILDOG'S",
  "script": "latin",
  "bbox": [120, 84, 418, 148],
  "rotation_deg": 0,
  "direction": "left-to-right",
  "face": "lid",
  "artwork_direction": "뚜껑 위쪽을 향함"
}
```

OCR 항목에는 추가로 `engine`, `model_signature`, `confidence`를 기록한다.

## 교차검증과 번역 영역

`cross_validation.data.regions`는 두 판독에서 발견한 region ID 합집합을 모두 포함한다. 각
항목은 `region_id`, `ocr_region_id`, `visual_region_id`, `agreed_text`, `matched: true`와 합의한
bbox·회전·방향·면·그림 방향을 기록한다.
`cross_validation.data.conflicts`에는 아직 합의하지 못한 항목만 둔다. 충돌이 하나라도 있으면
`pass`로 만들지 않는다.

`translation.data.regions`에는 다음 정보를 기록한다.

```json
{
  "region_id": "front-brand-01",
  "source_text": "DEVILDOG'S",
  "meaning_ko": "브랜드명",
  "final_text_ko": "데블독",
  "occurrences": 1,
  "bbox": [120, 84, 418, 148],
  "rotation_deg": 0,
  "direction": "left-to-right",
  "face": "lid",
  "visual_role": "제품명",
  "artwork_direction": "뚜껑 위쪽을 향함"
}
```

profile의 `exact_text`가 번역 영역에 모두 있어야 한다. 보이는 문구가 profile에 없다면
기록에서 누락하지 말고 profile을 먼저 갱신한다.

## 단계 증거

각 stage는 다음 구조를 사용한다.

```json
{
  "status": "pending",
  "evidence": [],
  "data": {}
}
```

- `evidence`: `{"path": "프로젝트 상대 경로", "sha256": "..."}` 형식으로 이미지·JSON·HTML
  보고서를 기록한다. 검사 시 현재 파일 SHA가 달라지면 실패한다.
- `data`: 해당 단계의 측정값, 도구·모델 서명, 판정 근거를 기록한다.
- 결과를 실행하지 못했으면 빈 `pass`를 만들지 말고 `error`와 이유를 기록한다.
- 확률형 검사만 애매하면 `review`로 두고, 승인 대상 산출물 SHA와 검토 근거를 남긴다.

## 필수 측정값

`candidate_validation.data`에는 최소한 다음 값을 둔다.

- `resized: false`
- `alpha_equal: true`
- `changed_outside_editable: 0`
- `changed_inside_protected: 0`
- `changed_inside_seam_guard: 0`
- 원본·후보 크기, 색 모드와 SHA-256

보존 대상은 추가로 `rgba_equal: true`여야 한다.

`edit_plan.data.compositor`에는 `mode: "ai-reference-lettering"`,
`fixed_font_used: false`, `background_locked: true`와 영역별 `region_id`, `exact_text`, bbox,
회전·방향, 모델 서명, 2개 이상의 후보 수, OCR 일치 여부를 기록한다. 각 영역에는 원본 스타일 crop,
깨끗한 배경, 후보 시트, 선택 레터링과 레터링 마스크의 프로젝트 상대 경로·SHA-256을 묶는다.
형태·크기·방향·간격·효과·표면 통합·옛 로고 실루엣 제거 판정을 영역별 `style_checks`에
모두 `true`로 남긴다. 고정 폰트 SHA나 결정적 폰트 조판 기록은 통과 근거가 아니다.

`post_ocr.data`에는 `candidate_sha256`, `engine_signature`,
`forbidden_foreign_detected: false`, `expected_text_matched: true`,
`duplicate_text_detected: false`를 둔다. `post_visual.data`에는 같은 `candidate_sha256`과
`translation_matched`, `text_orientation_matched`, `artwork_orientation_matched`,
`color_preserved`, `sharpness_passed`, `seams_preserved` 외에도 `lettering_shape_matched`,
`lettering_size_matched`, `lettering_direction_matched`, `lettering_spacing_matched`,
`lettering_effects_matched`, `surface_integration_matched`, `old_logo_silhouette_absent`를 모두
`true`로 둔다.

`material_validation.data`에는 실제 Material binding, 공유 소비자, D/N/G별 정책, 공통 글리프
마스크 SHA, 마스크 밖 변경 수와 조명 진단 증거를 둔다. `neutralize_old_text`인 보조맵은
`material_masks["material::property"]`에 재질 전용 마스크의 프로젝트 상대 경로, SHA-256과
`method: "inpaint"` 또는 `method: "patch"`를 반드시 기록한다. `patch` 방식에는 복원 이미지의
프로젝트 상대 `patch` 경로와 `patch_sha256`도 기록한다.

필수 키는 `bindings`, `shared_consumers_resolved`, `text_mask_sha256`, `alignment_passed`,
`foreign_relief_detected`, `changed_outside_masks`, `graph_scope`다. `graph_scope`는 게임
카탈로그의 역의존성까지 따라 실제 연결을 모두 찾았다는 `resolved`여야 한다. 연결이 없거나
일부만 확인된 상태를 파일명 추정으로 대신하지 않는다.

`mip_validation`, `bundle_validation`, `runtime_validation`에는 검사한 모든 단계·밉·캡처가
무엇인지와 누락 수를 기록한다. `release_validation`은 모든 입력과 보고서 SHA를 묶는다.

- `mip_validation.data`: `checked_mips`, `missing_mips`
- `bundle_validation.data`: `layout_equal`, `bytes_equal_outside_payloads`, `roundtrip_passed`
- `runtime_validation.data`: `capture_matrix`, `foreign_text_detected`, `alignment_passed`,
  `seam_passed`
- `release_validation.data`: `input_hashes`, `report_hashes`, `bundle_hashes`
