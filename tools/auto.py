"""
아이템 텍스처 한글화 원클릭 파이프라인.

SPT_Asset_Editor의 핵심 로직(UnityPy 추출/교체 + 원본 포맷 유지)만 가져온 CLI.
GUI 클릭 없이 명령 한 줄(또는 .bat 더블클릭)로 동작.

사용법:
  python tools/auto.py extract [필터]   # 게임 번들 → work/1_raw/ 로 PNG 추출
  python tools/auto.py repack  [필터]   # work/2_edited/ PNG → bundles/ 로 번들 생성
  python tools/auto.py deploy           # DLL 빌드 + SPT user/mods 에 모드 설치

흐름:
  1. extract  → work/1_raw/ 에 PNG들 + map.json 생성
  2. (사람) GPT로 한글화 → 같은 파일명으로 work/2_edited/ 에 저장
  3. repack   → bundles/<게임경로>/...bundle 생성 (SPT가 이걸로 원본 덮음)
  4. deploy   → C# DLL 빌드 + bundles/ 전체를 user/mods 에 설치 (런처 임시파일 삭제 필요)

[필터]: 번들 key에 포함된 문자열로 일부만 처리 (예: item_food_mayo). 생략 시 전체.
"""
import json
import os
import shutil
import subprocess
import sys

import UnityPy
from PIL import Image

# ── 설정 ───────────────────────────────────────────────
SPT_DIR = os.environ.get("SPT_DIR", "D:/SPT")
GAME_ROOT = os.path.join(SPT_DIR, "EscapeFromTarkov_Data/StreamingAssets/Windows")

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(PROJ, "work", "1_raw")
EDITED_DIR = os.path.join(PROJ, "work", "2_edited")
OUT_DIR = os.path.join(PROJ, "bundles")
MAP_PATH = os.path.join(PROJ, "tools", "map.json")
BUNDLES_JSON = os.path.join(PROJ, "bundles.json")

# 배포 대상 (SPT 4.x: 서버는 D:/SPT/SPT, 모드는 그 아래 user/mods)
MOD_NAME = "GoLani-ItemTextureKoreanChange"
MODS_DIR = os.path.join(SPT_DIR, "SPT", "user", "mods", MOD_NAME)
CSPROJ = os.path.join(PROJ, "GoLani.ItemTextureKoreanChange.csproj")
DLL_OUT = os.path.join(PROJ, "bin", "Release", "net9.0")

DXT5 = 12  # TextureFormat.DXT5 (BC3) 폴백용


def _keys(flt=None):
    """bundles.json에서 번들 key 목록을 읽음 (필터 적용)."""
    with open(BUNDLES_JSON, encoding="utf-8") as f:
        manifest = json.load(f)["manifest"]
    keys = [m["key"] for m in manifest]
    return [k for k in keys if not flt or flt in k]


def _png_name(key, tex):
    """번들 key + 텍스처 이름 → 충돌 없는 PNG 파일명. ('/'는 키마다 중복되니 인코딩)"""
    return key.replace("/", "@") + "__" + tex + ".png"


def extract(flt=None):
    os.makedirs(RAW_DIR, exist_ok=True)
    entries = []
    for key in _keys(flt):
        path = os.path.join(GAME_ROOT, key)
        if not os.path.exists(path):
            print(f"[건너뜀] 게임에 없음: {key}")
            continue
        env = UnityPy.load(path)
        n = 0
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            fn = _png_name(key, data.m_Name)
            data.image.save(os.path.join(RAW_DIR, fn))
            entries.append({"png": fn, "key": key, "texture": data.m_Name})
            n += 1
        print(f"[추출] {key}  (텍스처 {n}개)")
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"\n완료: PNG {len(entries)}개 → {RAW_DIR}")
    print(f"다음: work/2_edited/ 에 같은 파일명으로 한글화 PNG 저장 후 repack")


def _replace(bundle_path, out_path, replacements):
    """원본 번들 로드 → 해당 텍스처를 새 PNG로 교체(원본 포맷 유지) → out_path 저장."""
    env = UnityPy.load(bundle_path)
    changed = False
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        data = obj.read()
        new_png = replacements.get(data.m_Name)
        if not new_png:
            continue
        img = Image.open(new_png)
        if (img.width, img.height) != (data.m_Width, data.m_Height):
            img = img.resize((data.m_Width, data.m_Height), Image.LANCZOS)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        mip = max(1, int(getattr(data, "m_MipCount", 1) or 1))
        fmt = data.m_TextureFormat
        # 핵심: target_format=원본 으로 재인코딩해야 밝아짐/깨짐 안 생김.
        # 원본 포맷 재압축 실패 시에만 DXT5로 폴백.
        try:
            data.set_image(img, target_format=fmt, mipmap_count=mip)
            data.save()
        except Exception as e:
            print(f"   원본포맷({fmt}) 실패 → DXT5 폴백: {e}")
            data.set_image(img, target_format=DXT5, mipmap_count=mip)
            data.save()
        changed = True
        print(f"   교체: {data.m_Name} (포맷 {fmt})")
    if changed:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(env.file.save())
    return changed


def repack(flt=None):
    if not os.path.exists(MAP_PATH):
        sys.exit("map.json 없음. 먼저 extract 실행.")
    with open(MAP_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    # key별로, work/2_edited/ 에 실제로 있는 편집본만 모음
    by_key = {}
    for e in entries:
        if flt and flt not in e["key"]:
            continue
        edited = os.path.join(EDITED_DIR, e["png"])
        if os.path.exists(edited):
            by_key.setdefault(e["key"], {})[e["texture"]] = edited

    if not by_key:
        sys.exit(f"교체할 PNG 없음. work/2_edited/ 에 한글화 PNG를 넣었는지 확인.")

    done = 0
    for key, repl in by_key.items():
        src = os.path.join(GAME_ROOT, key)
        out = os.path.join(OUT_DIR, key)
        print(f"[리팩] {key}")
        if _replace(src, out, repl):
            done += 1
    print(f"\n완료: 번들 {done}개 → {OUT_DIR}")
    print(f"다음: SPT 런처에서 '임시 파일 삭제' 후 게임 실행")


def deploy():
    # 1. C# DLL 빌드 (SPT 4.x 번들 모드는 DLL 필수)
    print("[빌드] dotnet build ...")
    subprocess.run(["dotnet", "build", CSPROJ, "-c", "Release"], check=True)

    # 2. 배포할 번들 = bundles/ 안의 모든 .bundle (실제로 만든 것만)
    keys = []
    for root, _, files in os.walk(OUT_DIR):
        for fn in files:
            if fn.endswith(".bundle"):
                rel = os.path.relpath(os.path.join(root, fn), OUT_DIR).replace("\\", "/")
                keys.append(rel)
    if not keys:
        sys.exit("bundles/ 에 .bundle 없음. 먼저 repack 실행.")

    # 3. 모드 폴더 구성: DLL + deps + bundles.json + bundles/
    os.makedirs(MODS_DIR, exist_ok=True)
    for f in (f"{MOD_NAME.replace('-', '.')}.dll", f"{MOD_NAME.replace('-', '.')}.deps.json"):
        shutil.copy2(os.path.join(DLL_OUT, f), MODS_DIR)
    manifest = {"manifest": [{"key": k, "dependencyKeys": []} for k in keys]}
    with open(os.path.join(MODS_DIR, "bundles.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=4)
    dst = os.path.join(MODS_DIR, "bundles")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(OUT_DIR, dst)

    print(f"\n완료: 번들 {len(keys)}개 설치 → {MODS_DIR}")
    print("다음: SPT 런처에서 '임시 파일 삭제' 후 게임 실행")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    flt = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "extract":
        extract(flt)
    elif cmd == "repack":
        repack(flt)
    elif cmd == "deploy":
        deploy()
    else:
        sys.exit("사용법: python tools/auto.py [extract|repack|deploy] [필터]")
