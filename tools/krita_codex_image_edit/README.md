# Krita Codex 선택 영역 AI 편집

Krita의 현재 선택을 문맥 crop과 흑백 가이드로 내보내고, 장기 실행 중인
`codex app-server`에서 기본 `$imagegen` 스킬을 호출한 뒤 결과를 원래 마스크로 잘라 새
paint layer에 넣는 플러그인이에요.

- `pykrita/golani_codex_image_edit/core.py`: crop·프롬프트·픽셀 불변성
- `pykrita/golani_codex_image_edit/app_server.py`: stdio JSONL App Server 클라이언트
- `pykrita/golani_codex_image_edit/spt.py`: SPT analysis·원본·마스크 게이트와 라벨 면 명세
- `pykrita/golani_codex_image_edit/docker.py`: Krita/PyQt5·PyQt6 UI와 문서 어댑터
- `package.py`: Krita Plugin Importer용 재현 가능한 ZIP 생성기

설치와 사용법은 [Krita Codex 선택 영역 편집 문서](../../docs/krita-codex-image-edit.md)를
따라 주세요. 일반 이미지 편집과 SPT 준비 작업 모드를 지원해요. SPT 준비가 덜 된 품목은
원본과 안전하게 확인된 참고 선택만 열고, 공식 analysis와 5종 마스크 게이트가 통과하기
전까지 생성 버튼을 잠가요.
