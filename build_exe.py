"""
BilibiliGIFMaker 打包脚本（PyInstaller --onedir 模式）
=======================================================
功能:
  1. 安装/检查 PyInstaller
  2. 以 --noconsole --onedir 模式打包 GUI 程序
  3. 自动包含 ffmpeg.exe / ffprobe.exe 到输出目录
  4. 确保打包后的子进程（ffmpeg/ffprobe）不弹出控制台窗口
     （已在 BilibiliGIFMaker_GUI.py 中通过 creationflags=CREATE_NO_WINDOW 处理）
  5. 输出到 dist/BilibiliGIFMaker 目录
  6. 打包完成后自动清理临时构建文件
"""

import subprocess
import sys
import os
import shutil
from pathlib import Path

# ── 项目根目录 ──
BASE_DIR = Path(__file__).parent.resolve()

# ── 打包配置 ──
APP_NAME = "BilibiliGIFMaker"
MAIN_SCRIPT = BASE_DIR / "BilibiliGIFMaker_GUI.py"
DIST_DIR = BASE_DIR / "dist"
WORK_DIR = BASE_DIR / "build"  # PyInstaller 临时工作目录（打包后自动删除）
SPEC_FILE = BASE_DIR / f"{APP_NAME}.spec"

# 需要随 exe 分发的额外文件（ffmpeg/ffprobe 需要在运行时路径中能找到）
EXTRA_DATA_FILES = [
    ("ffmpeg.exe", "."),
    ("ffprobe.exe", "."),
]

# PyInstaller 需要了解的额外 hidden imports
HIDDEN_IMPORTS = [
    "you_get",
    "you_get.extractors",
]

# ── 颜色输出辅助（可选） ──
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init()
except ImportError:
    # 降级：空对象
    class Fore:
        GREEN = CYAN = YELLOW = RED = RESET = ""
    class Style:
        BRIGHT = RESET_ALL = ""


def info(msg: str):
    print(f"{Fore.CYAN}[INFO]{Fore.RESET} {msg}")


def ok(msg: str):
    print(f"{Fore.GREEN}[ OK ]{Fore.RESET} {msg}")


def warn(msg: str):
    print(f"{Fore.YELLOW}[WARN]{Fore.RESET} {msg}")


def error(msg: str):
    print(f"{Fore.RED}[FAIL]{Fore.RESET} {msg}")


# ══════════════════════════════════════════════════════════
#  步骤1: 检查 PyInstaller
# ══════════════════════════════════════════════════════════

def check_pyinstaller():
    """确保 PyInstaller 已安装，未安装则自动安装"""
    info("检查 PyInstaller...")
    try:
        import PyInstaller  # noqa: F401
        ok("PyInstaller 已安装")
    except ImportError:
        warn("PyInstaller 未安装，正在自动安装...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            stdout=sys.stdout, stderr=sys.stderr,
        )
        ok("PyInstaller 安装完成")


# ══════════════════════════════════════════════════════════
#  步骤2: 验证依赖文件
# ══════════════════════════════════════════════════════════

def check_dependencies():
    """检查编译所需文件是否存在"""
    info("检查项目依赖...")

    issues = []

    # 主脚本
    if not MAIN_SCRIPT.exists():
        issues.append(f"主脚本文件不存在: {MAIN_SCRIPT}")

    # ffmpeg/ffprobe
    for name, _ in EXTRA_DATA_FILES:
        f = BASE_DIR / name
        if not f.exists():
            warn(f"未找到 {name}，打包后的程序运行时将依赖系统 PATH 中的同名程序")
        else:
            ok(f"  找到 {name} ({f.stat().st_size / 1024:.1f} KB)")

    if issues:
        for msg in issues:
            error(msg)
        sys.exit(1)


# ══════════════════════════════════════════════════════════
#  步骤3: 生成 PyInstaller 打包命令
# ══════════════════════════════════════════════════════════

def build_pyinstaller_command() -> list:
    """构建 PyInstaller 参数列表"""
    cmd = [
        "pyinstaller",
        # ── GUI 模式：无控制台 ──
        "--noconsole",
        # ── 非单文件模式 ──
        "--onedir",
        # ── 名称 ──
        "--name", APP_NAME,
        # ── 输出目录 ──
        "--distpath", str(DIST_DIR),
        "--workpath", str(WORK_DIR),
        # ── 清空上次构建的缓存（避免残留文件污染） ──
        "--clean",
        # ── 将 ffmpeg/ffprobe 添加到输出目录（与 exe 同目录） ──
    ]

    for src, dst in EXTRA_DATA_FILES:
        src_path = BASE_DIR / src
        if src_path.exists():
            cmd.extend(["--add-data", f"{src_path};{dst}"])

    # ── hidden imports ──
    for mod in HIDDEN_IMPORTS:
        cmd.extend(["--hidden-import", mod])

    # ── 收集 PyQt6 必需的 DLL
    #    PyInstaller 通常会自动处理，但显式指定更可靠
    cmd.extend(["--collect-submodules", "PyQt6"])

    # ── 主入口脚本（必须放在最后） ──
    cmd.append(str(MAIN_SCRIPT))

    return cmd


# ══════════════════════════════════════════════════════════
#  步骤4: 执行打包
# ══════════════════════════════════════════════════════════

def run_pyinstaller(cmd: list):
    """执行 PyInstaller 命令"""
    print()
    info("开始打包，命令:")
    print(f"  {' '.join(cmd)}")
    print()

    try:
        subprocess.check_call(cmd, cwd=str(BASE_DIR))
    except subprocess.CalledProcessError as e:
        error(f"打包失败，返回码: {e.returncode}")
        sys.exit(1)
    except FileNotFoundError:
        error("未找到 pyinstaller 命令，请确保 PyInstaller 已正确安装")
        sys.exit(1)


# ══════════════════════════════════════════════════════════
#  步骤5: 清理临时构建文件
# ══════════════════════════════════════════════════════════

def cleanup():
    """删除 PyInstaller 临时工作目录和 .spec 文件"""
    info("清理临时构建文件...")

    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        ok(f"已删除临时工作目录: {WORK_DIR}")

    if SPEC_FILE.exists():
        SPEC_FILE.unlink()
        ok(f"已删除 spec 文件: {SPEC_FILE}")


# ══════════════════════════════════════════════════════════
#  步骤6: 输出结果摘要
# ══════════════════════════════════════════════════════════

def print_summary():
    """打包成功后输出摘要信息"""
    output_dir = DIST_DIR / APP_NAME
    exe_path = output_dir / f"{APP_NAME}.exe"

    if not exe_path.exists():
        warn(f"预期输出 EXE 不存在: {exe_path}")
        return

    exe_size_kb = exe_path.stat().st_size / 1024

    print()
    print("=" * 60)
    print(f"  {Style.BRIGHT}{Fore.GREEN}打包完成!{Style.RESET_ALL}")
    print(f"  {Fore.CYAN}输出目录:{Fore.RESET}  {output_dir}")
    print(f"  {Fore.CYAN}主程序:{Fore.RESET}    {exe_path}")
    print(f"  {Fore.CYAN}EXE 大小:{Fore.RESET}  {exe_size_kb:.1f} KB")
    print(f"  {Fore.CYAN}运行模式:{Fore.RESET}  GUI 模式（无控制台窗口）")
    print(f"  {Fore.CYAN}子进程:{Fore.RESET}    ffmpeg/ffprobe 使用 CREATE_NO_WINDOW 隐藏控制台")
    print(f"  {Fore.CYAN}分发方式:{Fore.RESET}  将 {output_dir} 整个目录打包分发")
    print("=" * 60)
    print()

    # 运行时注意事项
    print(f"{Fore.YELLOW}重要提示:{Fore.RESET}")
    print(f"  - 运行 {APP_NAME}.exe 不会弹出任何命令提示符窗口")
    print(f"  - ffmpeg.exe 和 ffprobe.exe 已复制到 exe 同目录，程序会自动找到它们")
    print(f"  - 视频缓存目录 (BilibiliTempVideos) 和输出目录 (output) 会在运行时自动创建")
    print(f"  - 如果移动 exe 到其他位置，请确保 ffmpeg.exe/ffprobe.exe 在同目录下")


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

def main():
    print(f"{Style.BRIGHT}{'=' * 60}{Style.RESET_ALL}")
    print(f"{Style.BRIGHT}  BilibiliGIFMaker 一键打包工具{Style.RESET_ALL}")
    print(f"{Style.BRIGHT}{'=' * 60}{Style.RESET_ALL}")
    print()

    check_pyinstaller()
    check_dependencies()

    cmd = build_pyinstaller_command()
    run_pyinstaller(cmd)

    cleanup()
    print_summary()

    info("Build script completed successfully.")


if __name__ == "__main__":
    main()
