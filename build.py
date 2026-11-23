"""一键打包：onedir 产物 → 自检 → 便携 zip →（可选）Inno Setup 安装包。

用法：
    python build.py                 # 打包 + 自检 + 生成 release/Kwey-<ver>-win64.zip
    python build.py --console       # 带控制台的调试版
    python build.py --installer     # 额外编译安装包（需要 Inno Setup 6，找不到会给出安装指引）
    python build.py --skip-build    # 跳过 PyInstaller，只用现有 release/Kwey 重打 zip/安装包

产物（都在 release/）：
    Kwey/                              onedir 目录（分发需整目录）
    Kwey-<ver>-win64.zip               便携版
    Kwey-<ver>-win64-setup.exe         安装包（--installer）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DIST = "release"

ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
]

# winget 默认按用户安装（%LOCALAPPDATA%\Programs\Inno Setup 6），不进 Program Files
ISCC_USER_CANDIDATES = [
    ("LOCALAPPDATA", "Programs", "Inno Setup 6", "ISCC.exe"),
    ("LOCALAPPDATA", "Programs", "Inno Setup 5", "ISCC.exe"),
]


def project_version() -> str:
    text = (ROOT / "app" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def make_zip(folder: Path, target: Path) -> Path:
    """打便携 zip（保持顶层目录名 Kwey/）。"""
    if target.exists():
        target.unlink()
    total = 0
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            zf.write(path, Path(folder.name) / path.relative_to(folder))
            total += path.stat().st_size
    print(f"[build] zip: {target} ({target.stat().st_size / 1024 / 1024:.1f} MB, 源 {total / 1024 / 1024:.1f} MB)")
    return target


def find_iscc() -> str | None:
    env = os.environ.get("ISCC_PATH")
    if env and Path(env).is_file():
        return env
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    for candidate in ISCC_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for parts in ISCC_USER_CANDIDATES:
        base = os.environ.get(parts[0])
        if not base:
            continue
        path = Path(base).joinpath(*parts[1:])
        if path.is_file():
            return str(path)
    return None


def build_installer(version: str) -> int:
    iscc = find_iscc()
    if not iscc:
        print("[build] 未找到 Inno Setup（ISCC.exe）。安装后重试：")
        print("        winget install --id JRSoftware.InnoSetup -e")
        print("        或下载 https://jrsoftware.org/isdl.php")
        print(f"[build] 脚本已就绪：installer/kwey.iss（版本 {version}）")
        return 2
    cmd = [iscc, f"/DMyAppVersion={version}", str(ROOT / "installer" / "kwey.iss")]
    print("[build]", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("[build] 安装包编译失败")
        return result.returncode
    setup = ROOT / "release" / f"Kwey-{version}-win64-setup.exe"
    print(f"[build] 安装包: {setup} ({setup.stat().st_size / 1024 / 1024:.1f} MB)" if setup.is_file() else "[build] 未找到安装包产物")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 Kwey 桌面版")
    parser.add_argument("--console", action="store_true", help="保留控制台（调试用）")
    parser.add_argument("--dist", default=DEFAULT_DIST, help=f"产物目录，默认 {DEFAULT_DIST}")
    parser.add_argument("--clean", action="store_true", help="丢弃 PyInstaller 缓存重新分析")
    parser.add_argument("--installer", action="store_true", help="编译 Inno Setup 安装包")
    parser.add_argument("--skip-build", action="store_true", help="复用已有 release/Kwey")
    parser.add_argument("--no-zip", action="store_true", help="不生成便携 zip")
    args = parser.parse_args()

    version = project_version()
    print(f"[build] Kwey {version}")

    if not args.skip_build:
        env = dict(os.environ)
        env["KWEY_CONSOLE"] = "1" if args.console else "0"
        cmd = [
            sys.executable, "-m", "PyInstaller", "kwey.spec",
            "--noconfirm", "--distpath", args.dist, "--workpath", "build",
        ]
        if args.clean:
            cmd.append("--clean")
        print("[build]", " ".join(cmd))
        result = subprocess.run(cmd, cwd=ROOT, env=env)
        if result.returncode != 0:
            print("[build] 打包失败")
            return result.returncode

    exe = (ROOT / args.dist / "Kwey" / "Kwey.exe").resolve()
    if not exe.is_file():
        print(f"[build] 未找到产物: {exe}")
        return 1

    print(f"[build] 产物: {exe}")
    size_mb = sum(f.stat().st_size for f in exe.parent.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"[build] 体积: {size_mb:.1f} MB")

    # 自检：冻结包里的资源与桌面壳依赖是否齐备
    diag = subprocess.run([str(exe), "--diag"], capture_output=True, text=True, timeout=180)
    try:
        info = json.loads(diag.stdout[diag.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        print("[build] 自检输出无法解析，请手工运行 Kwey.exe --diag")
        return 1

    checks = {
        "static": info["static"]["exists"] and info["static"]["index_html"],
        "webview": info["webview"]["ok"],
        "edgechromium": info["edgechromium"]["ok"],
        "clr": info["clr"]["ok"],
    }
    optional = {
        "sqlite_vec": (info.get("sqlite_vec") or {}).get("ok", False),
        "onnxruntime": (info.get("onnxruntime") or {}).get("ok", False),
        "embed_model": (info.get("embed_model") or {}).get("ok", False),
    }
    for name, ok in checks.items():
        print(f"[build] {name:14s} {'OK' if ok else 'FAIL'}")
    for name, ok in optional.items():
        print(f"[build] {name:14s} {'OK' if ok else '缺失（语义检索会降级/按需下载）'}")
    if not all(checks.values()):
        print("[build] 自检未通过：该产物在目标机器上可能无法启动")
        return 1

    if not args.no_zip:
        make_zip(exe.parent, ROOT / args.dist / f"Kwey-{version}-win64.zip")

    if args.installer:
        return build_installer(version)

    print("[build] 完成。分发时请整目录打包（Kwey.exe 与 _internal 必须同级），或用 --installer 出安装包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
