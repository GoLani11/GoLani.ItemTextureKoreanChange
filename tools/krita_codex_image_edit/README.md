# Krita Codex 선택 영역 AI 편집

Krita의 현재 선택을 문맥 crop과 흑백 가이드로 내보내고, 장기 실행 중인
`codex app-server`에서 기본 `$imagegen` 스킬을 호출한 뒤 결과를 원래 마스크로 잘라 새
paint layer에 넣는 플러그인이에요.

- `pykrita/golani_codex_image_edit/core.py`: crop·프롬프트·픽셀 불변성
- `pykrita/golani_codex_image_edit/app_server.py`: stdio JSONL App Server 클라이언트
- `pykrita/golani_codex_image_edit/spt.py`: SPT 원본 identity·작업 뷰와 최종 검증 준비 기록
- `pykrita/golani_codex_image_edit/docker.py`: Krita/PyQt5·PyQt6 UI와 문서 어댑터
- `package.py`: Krita Plugin Importer용 재현 가능한 ZIP 생성기

설치와 사용법은 [Krita Codex 선택 영역 편집 문서](../../docs/krita-codex-image-edit.md)를
따라 주세요. `일반 생성형 채우기`와 `SPT 자유 선택 편집` 모드를 지원해요.
SPT에서는 `SPT RGB 작업 뷰 열기`로 불변 원본과 RGB가 같고 표시용 알파만
255인 Git-ignored 작업 뷰를 열어요. 플러그인은 추천 선택을 자동 적용하지 않고
라벨 면이나 `editable` 마스크 안에 선택을 제한하지도 않아요. 사용자의 현재 Krita
선택과 프롬프트가 해당 시도의 편집 범위와 내용을 결정하며, 기존 미리보기 레이어 위에서도
다른 선택으로 계속 작업할 수 있어요.

SPT Diffuse의 원본 알파는 재질 데이터로 고정되며, 작업 뷰와 미리보기는 원본
PNG를 수정하지 않아요. 각 결과는 `free-selection-preview`이고
`candidate_approved: false`인 검증 전 산출물이에요. `최종 검증 준비 요청 만들기`는
정식 승격 준비를 별도로 기록할 뿐 선택 권한이 아니에요. 최종 stage 전에는 기존처럼
analysis, 원본 크기의 5종 마스크, 패널·후보 OCR, 공식 compositor와 모든 저장소
품질 게이트를 통과해야 해요.
