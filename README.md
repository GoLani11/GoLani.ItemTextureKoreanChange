# GoLani.ItemTextureKoreanChange

## English
Mod to change Tarkov item texture to Korean. Not in "add" mode.

It is manufactured upscale to a size 4096x4096.
There may be lags during loading and playing.

Currently, only food items have been changed, but other items will also be changed in the future.

### How to apply it?

SPT / user / mods / GoLani-ItemTextureKoreanChange

Please apply it so that it becomes a path like this.

Before you start, run "Clean Temp File" in the launcher before you start, or the icon may not apply.


You can use the source code freely if you just leave a comment and an address.


## 한국어
타르코프 아이템 텍스처를 한국어로 변경하는 모드입니다. "추가"모드가 아닙니다.

4096x4096사이즈로 업스케일되어 제작됬습니다.
로딩 및 플레이 중 렉이 발생할 수 있습니다.

현재는 음식품만 변경되었으나 추후 다른 아이템들도 변경될 예정입니다.

### 모드 적용 방법

SPT 설치 파일 / user / mods / GoLani-ItemTextureKoreanChange

이렇게 경로가 되도록 적용해주세요.

시작하기 전 런처에서 임시 파일 삭제를 한 후 시작하세요. 그렇지 않으면 아이콘이 적용되지 않을 수 있습니다.


댓글과 출처만 남겨주시면 자유롭게 소스 코드 활용하셔도 됩니다.


## 개발자용 — 자동화 파이프라인 (SPT 4.x)

SPT 4.x(C# 서버)용 번들 모드입니다. 텍스처 추출·교체·설치를 스크립트로 자동화합니다.

| 단계 | 명령 / 더블클릭 | 내용 |
|------|----------------|------|
| 0 | `0_설치.bat` | 파이썬 라이브러리 설치 (UnityPy 등) |
| 1 | `1_추출.bat [필터]` | 게임 번들 → `work/1_raw/` PNG 추출 |
| — | (사람) | GPT 등으로 한글화 → 같은 파일명으로 `work/2_edited/` 저장 |
| 2 | `2_적용.bat [필터]` | `work/2_edited/` → `bundles/` 번들 생성 (원본 포맷 유지) |
| 3 | `3_배포.bat` | C# DLL 빌드 + `bundles/` 전체를 `user/mods` 에 설치 |

- 빌드 요구: .NET 9 SDK (`dotnet`), 파이썬 3 + UnityPy 1.25.0.
- 게임 경로는 `D:\SPT` 기준. 다르면 환경변수 `SPT_DIR` 로 지정.
- 적용 후 **런처에서 "임시 파일 삭제"** 필수.
- 설계·주의사항: [docs/automation-design.md](docs/automation-design.md)
