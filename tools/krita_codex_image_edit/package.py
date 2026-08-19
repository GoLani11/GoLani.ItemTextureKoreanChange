from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


TOOL_ROOT = Path(__file__).resolve().parent
PYKRITA_ROOT = TOOL_ROOT / "pykrita"
PLUGIN_NAME = "golani_codex_image_edit"


def _project_root() -> Path:
    for parent in TOOL_ROOT.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "localize.py").is_file():
            return parent
    raise RuntimeError("프로젝트 루트를 찾지 못했어요")


def package_plugin(output: Path, *, force: bool = False) -> Path:
    output = output.expanduser().resolve()
    if output.exists() and not force:
        raise FileExistsError(f"이미 파일이 있어요. --force로 교체하세요: {output}")

    desktop = PYKRITA_ROOT / f"{PLUGIN_NAME}.desktop"
    package = PYKRITA_ROOT / PLUGIN_NAME
    license_path = _project_root() / "LICENSE"
    sources = [desktop, *sorted(package.glob("*.py"))]
    if not desktop.is_file() or not sources[1:] or not license_path.is_file():
        raise FileNotFoundError("Krita 플러그인 소스가 불완전해요")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"임시 패키지 파일이 이미 있어요: {temporary}")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive_sources = [
                *((source, source.relative_to(PYKRITA_ROOT).as_posix()) for source in sources),
                (license_path, "LICENSE"),
            ]
            for source, relative in archive_sources:
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, source.read_bytes())
        temporary.replace(output)
    except Exception:
        if temporary.is_file():
            temporary.unlink()
        raise
    return output


def main() -> int:
    default_output = (
        _project_root()
        / "workspace"
        / "krita-codex"
        / "dist"
        / "golani-codex-image-edit.zip"
    )
    parser = argparse.ArgumentParser(
        description="Krita의 Python Plugin Importer용 Codex 선택 편집 ZIP을 만들어요."
    )
    parser.add_argument("--out", type=Path, default=default_output)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = package_plugin(args.out, force=args.force)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
