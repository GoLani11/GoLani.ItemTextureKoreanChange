# GoLani.ItemTextureKoreanChange

SPT 음식·음료의 포장 글자를 한국어로 바꾸는 제작 도구와 SPT 4.1용 번들 모드입니다.

**한 품목씩 원본 추출 → 이미지 편집 → 파일 검증 → 테스트 패키지 → 게임 확인 → 배포본** 순서로 진행합니다.
원본 해상도·UV 배치·Diffuse 알파를 유지하고, 지정한 문자 영역만 편집합니다.
게임 폴더는 읽기 전용 입력이며 명령이 게임 설치를 변경하지 않습니다.

## 시작하기

Python 3.11 이상과 .NET SDK 10이 필요합니다. Windows에서는 `0_설치.bat`으로 Python 환경을 준비합니다.
OCR은 기본 설치에 포함되지 않습니다.

```bat
.venv\Scripts\python.exe localize.py list
.venv\Scripts\python.exe localize.py prepare mayo --spt-root D:/SPT
```

WSL에서는 별도 Linux 가상환경에서 같은 명령을 실행할 수 있습니다. `D:/SPT`와 `/mnt/d/SPT` 모두 해석합니다.
다른 작업을 만들려면 `prepare`에 `--output workspace/items/mayo-v2`를 지정합니다. 기존 작업은 덮어쓰지 않습니다.

## 품목 편집

`workspace/items/mayo/EDITING.md`와 `job.json`을 기준으로 진행합니다.

- `source.json`, 원본 번들, `maps/*/source.png`: 원본 기준과 실제 Material 연결입니다.
- `maps/*/candidate.png`: 원본 크기 RGBA 편집본입니다. 처음에는 원본과 같습니다.
- `maps/*/editable.png`: 수정할 문자 영역을 흰색으로 표시한 0/255 마스크입니다. 처음에는 전부 검정입니다.
- `job.json`: 확정할 한글 문구와 메모입니다. 프로필의 문구는 과거 제안이므로 원본과 대조합니다.

이미지 편집 도구에 원본과 정확한 한글 문구를 주고 글꼴 인상·비율·색·배치를 지정합니다.
`compose`는 생성 결과에서 선택한 글자 레이어와 원문 제거 배경을 합성합니다.
`derive`는 같은 글자 알파에서 Normal/Gloss 효과를 계산합니다. 형식은 [편집 레시피](docs/editing.md)에 있습니다.

```bat
.venv\Scripts\python.exe localize.py validate workspace/items/mayo/job.json
.venv\Scripts\python.exe localize.py package workspace/items/mayo/job.json
```

검사는 크기·보존 채널·마스크 밖 픽셀·UV 경계·공유 맵을 확인합니다. 비교 이미지는 `reports/`에 저장합니다.
패키징은 모든 원본 밉과 압축 포맷을 유지하며, 편집한 압축 블록 밖 바이트를 보존하고 번들을 다시 열어 검증합니다.
변경 없는 텍스처는 재인코딩하지 않습니다. 압축 블록 안의 비문자 픽셀에는 압축 오차가 있을 수 있으며 그 범위를 검사합니다.

## 테스트 패키지와 배포본

`packages/<id>/GoLani-ItemTextureKoreanChange/`가 게임 확인용 모드입니다.
원본과 같은 초기 후보도 무편집 왕복 확인용 테스트 패키지를 만들 수 있습니다. 이는 번역 완료 판정이 아닙니다.

게임 설치 위치는 `SPT/SPT_Runtime/user/mods/GoLani-ItemTextureKoreanChange`입니다.
기존 설치를 별도 보관한 후 테스트 패키지를 사용하고, 런처의 임시 파일을 삭제합니다.
이 도구는 설치·기존 설치 덮어쓰기를 자동 실행하지 않습니다.

파일 검증은 한글 정확성이나 게임 외형을 승인하지 않습니다. 시각·게임 확인 기록이 있는 패키지만
`release`로 복사합니다. [검증과 배포](docs/validation.md)를 참고하세요.

## 구성과 검사

```text
mod/          SPT 4.1 서버 모드
localizer/    품목별 제작 코드
profiles/     품목 식별 정보와 번역 제안
tests/        기본 검사와 선택적인 실제 자산 검사
docs/         작업 방법과 변경 내역
diagnostics/  선택적인 OCR 선별 도구와 AssetPicker
workspace/    원본·편집본·패키지·검사 결과 (Git 제외)
```

```bat
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m pytest -m game_assets --spt-root D:/SPT
```

실제 자산 검사는 마요네즈와 소시지의 무편집 왕복 및 합성 시험 패치를 확인하며 게임 폴더를 변경하지 않습니다.
산출물은 `workspace/checks/`에 남습니다. `--dotnet`으로 .NET 10 실행 파일을 지정할 수 있습니다.

[재구축 내역](docs/rebuild.md) · [선택적 진단 도구](diagnostics/README.md)

소스 코드는 출처와 주소를 남기면 자유롭게 활용할 수 있습니다.
