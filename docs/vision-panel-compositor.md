# 비전 패널 레이어 합성 계약

최종 Texture2D는 전체 생성 결과가 아니라 다음 세 입력을 원본 위에 합성해 만든다.

1. 원본 mip 0 RGBA
2. `old_text` 안에서만 복원한 배경판
3. 원본 좌표로 되돌린 승인 한글 RGBA와 `lettering_mask`

생성 패널의 배경·그림·재질 픽셀은 증거로만 보관하고 최종 후보에 복사하지 않는다. Krita
SPT 작업 뷰와 생성 패널의 알파가 표시용 255여도 이를 후보 알파로 사용하지 않는다. 합성기는
마지막에 `editable` 밖 RGB와 전체 알파를 불변 원본에서 byte 그대로 복원하고 변경 픽셀을
실측한다.

## 준비 산출물

- `source`: 불변 원본 RGBA PNG
- `old_text`, `new_text`, `editable`, `protected`, `seam_guard`: 원본 크기 0/255 L PNG
- `source_style_reference`: 원본 해상도 스타일 crop
- `generated_panel`: 연결된 라벨 면을 한 호출에서 편집한 전체 결과. 표시용 불투명 알파는
  증거 패널에만 유효하며 최종 Texture2D의 재질 알파를 대체하지 않음
- `panel_ocr`: 생성 패널에서 확정 한글을 NFC 완전일치로 확인한 보고서
- `selected_lettering`: 원본 크기 RGBA. 승인 글자 밖 알파는 0
- `lettering_mask`: `selected_lettering`의 0보다 큰 알파와 정확히 같은 원본 크기 마스크

비정방향 문구는 기록된 원문 각도의 역각으로 작업 패널을 정방향화한다. 최종 recipe를 만들기
전에 승인 글자 RGBA와 마스크만 정확한 역변환으로 `source-mip0` 좌표에 되돌린다. 원본이나 최종
Texture2D 전체를 회전·재표본화하지 않는다.

## schema v2 예시

모든 파일 경로는 프로젝트 상대 경로이고 현재 SHA-256을 함께 기록한다.

```json
{
  "schema_version": 2,
  "target_id": "sample",
  "mode": "vision-panel-localization",
  "source": {"path": "workspace/source/sample.png", "sha256": "..."},
  "masks": {
    "old_text": {"path": "workspace/reviews/sample/masks/old_text.png", "sha256": "..."},
    "new_text": {"path": "workspace/reviews/sample/masks/new_text.png", "sha256": "..."},
    "editable": {"path": "workspace/reviews/sample/masks/editable.png", "sha256": "..."},
    "protected": {"path": "workspace/reviews/sample/masks/protected.png", "sha256": "..."},
    "seam_guard": {"path": "workspace/reviews/sample/masks/seam_guard.png", "sha256": "..."}
  },
  "background": {
    "method": "telea",
    "inpaint_radius": 2,
    "patches": [
      {
        "region_id": "textured-logo",
        "generator_signature": "lama-onnx:model-sha+runtime-version",
        "patch": {"path": "workspace/reviews/sample/background/lama.png", "sha256": "..."},
        "mask": {"path": "workspace/reviews/sample/background/lama-mask.png", "sha256": "..."}
      }
    ]
  },
  "panels": [
    {
      "panel_id": "front-label",
      "model_signature": "korean-lettering-model:checkpoint+settings-sha",
      "generation_attempts": 1,
      "single_generation_panel": true,
      "ocr_exact_match": true,
      "panel_ocr": {"path": "workspace/reviews/sample/front-panel-ocr.json", "sha256": "..."},
      "source_style_reference": {"path": "workspace/reviews/sample/front-source.png", "sha256": "..."},
      "generated_panel": {"path": "workspace/reviews/sample/front-generated.png", "sha256": "..."},
      "regions": [
        {
          "region_id": "front-brand",
          "exact_text": "한글 제품명",
          "occurrences": 1,
          "bbox": [120, 84, 418, 148],
          "rotation_deg": 17.5,
          "direction": "left-to-right",
          "ocr_exact_match": true,
          "panel_transform": {
            "coordinate_space": "source-mip0",
            "crop_bbox": [96, 60, 442, 172],
            "padding_px": 24,
            "source_rotation_deg": 17.5,
            "deskew_rotation_deg": 17.5,
            "inverse_rotation_deg": -17.5,
            "selected_lettering_restored_to_source": true,
            "source_texture_resampled": false,
            "final_texture_resampled": false
          },
          "source_typography": {"...": "ai-lettering.md 필드"},
          "result_typography": {"...": "ai-lettering.md 필드"},
          "typography_checks": {"...": "quality-gates.md 필드"},
          "selected_lettering": {"path": "workspace/reviews/sample/front-lettering.png", "sha256": "..."},
          "lettering_mask": {"path": "workspace/reviews/sample/front-lettering-mask.png", "sha256": "..."}
        }
      ]
    }
  ]
}
```

`background.method`는 다음 두 가지다.

- `telea`: 빠른 기본 복원. 선택적으로 `patches`를 뒤에 덮어 복잡한 무늬만 LaMa 등의 결과로
  교체할 수 있다. 각 patch mask는 `old_text`의 부분집합이어야 한다.
- `hash-pinned-patch`: 외부에서 완성한 원본 크기 RGBA `patch` 중 `old_text` 픽셀만 사용한다.

배경판은 원본 SHA, `old_text` SHA, 복원 방식·반경·모든 patch/mask/model signature SHA로
fingerprint를 만들고 `workspace/drafts/<target-id>/clean-background.png`에 캐시한다. 번역·글자
시안을 다시 만들 때 이 fingerprint가 같으면 배경 복원을 반복하지 않는다.

## 생성기 연결 원칙

합성기는 특정 모델을 설치하거나 실행하지 않는다. STELLAR, Krita/ComfyUI 또는 다른 이미지
편집 도구의 결과를 동일한 해시 고정 산출물 계약으로 받는다. 짧은 한글 장식 문구에는 STELLAR를
먼저 별도 환경에서 벤치마크하되, 모델·가중치 라이선스와 GPU 조건을 확인하기 전에는 저장소의
필수 의존성이나 release 입력으로 추가하지 않는다.

schema v1 고정 폰트 조판은 배치 참고를 위한 레거시다. `candidate_gate_eligible: false`로 기록되며
`candidate-check`와 최신 review gate를 통과할 수 없다.
