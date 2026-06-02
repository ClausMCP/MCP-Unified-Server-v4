#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP Setup Helper v9.0 – полная портативная установка всех зависимостей (включая когнитивные модули).
Все зависимости скачиваются в python_deps/, внешние инструменты – в tools/.
Работает полностью offline после первого скачивания.
"""
import os
import sys
import ast
import json
import shutil
import subprocess
import argparse
import time
import platform
import zipfile
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY_EXE = str(VENV / "Scripts" / "python.exe") if sys.platform == "win32" else str(VENV / "bin" / "python3")
PIP_CMD = [PY_EXE, "-m", "pip", "--no-input"]
DEPS_DIR = ROOT / "python_deps"
TOOLS_DIR = ROOT / "tools"
INSTALLERS_DIR = TOOLS_DIR / "installers"

# Пути к внешним инструментам (портативные)
TOOLS_PYTHON = TOOLS_DIR / "python" / "python.exe"
TOOLS_TESSERACT = TOOLS_DIR / "tesseract"
TOOLS_FFMPEG = TOOLS_DIR / "ffmpeg"
TOOLS_PANDOC = TOOLS_DIR / "pandoc"
TOOLS_WKHTMLTOPDF = TOOLS_DIR / "wkhtmltopdf"

# Добавляем инструменты в PATH для текущего процесса
for p in [TOOLS_PYTHON.parent, TOOLS_TESSERACT, TOOLS_FFMPEG, TOOLS_PANDOC, TOOLS_WKHTMLTOPDF]:
    if p.exists():
        os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")

# ========== ПОЛНЫЙ СПИСОК ЗАВИСИМОСТЕЙ ==========
BASE_DEPS = {
    # Базовые
    "pip", "setuptools", "wheel",
    # Сеть и системное
    "watchdog", "psutil", "requests", "xxhash", "cryptography", "keyring",
    # Парсинг и документы
    "beautifulsoup4", "feedparser", "icalendar", "openpyxl", "python-docx",
    "python-pptx", "pytesseract", "Pillow", "mutagen",
    # Базы данных и SQL
    "duckdb", "pyodbc", "sqlalchemy", "apscheduler",
    # Архивация
    "patool", "py7zr", "rarfile",
    # Веб-автоматизация
    "playwright",
    # Data science / ML (для RAG и эпизодической памяти)
    "pandas", "numpy", "scikit-learn", "sentence-transformers", "chromadb",
    # PDF и электронные книги
    "pypdf", "PyPDF2", "pdfplumber", "ebooklib",
    # Извлечение текста из веба
    "trafilatura", "readability-lxml", "html-table-takeout",
    # Память и утилиты
    "mempalace", "tiktoken",
    # Экспорт
    "pypandoc", "markdown", "tabulate",
    # Конфигурация и планирование
    "python-dotenv", "schedule",
    # MCP протокол (для когнитивных плагинов)
    "mcp",
    # Дополнительно для эпизодической памяти
    "numpy",  # уже есть, но для уверенности
}

# Зеркала PyPI
PIP_MIRRORS = [
    "",   # официальный
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple",
]

def get_base_python():
    """Возвращает путь к Python (системный или портативный)."""
    if TOOLS_PYTHON.exists():
        return str(TOOLS_PYTHON)
    sys_python = shutil.which("python")
    if sys_python:
        return sys_python
    return None

def load_dotenv_if_exists():
    try:
        from dotenv import load_dotenv
        env_file = ROOT / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass

def find_plugin_deps():
    """Сканирует плагины на наличие секции __mcp_plugin__ и извлекает dependencies."""
    deps = set()
    for search_dir in [ROOT, ROOT / "mcp_plugins"]:
        if not search_dir.is_dir(): continue
        for py_file in search_dir.glob("*.py"):
            if py_file.name.startswith("_"): continue
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name) and target.id == "__mcp_plugin__":
                                if isinstance(node.value, ast.Dict):
                                    keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
                                    if "dependencies" in keys:
                                        idx = keys.index("dependencies")
                                        val = node.value.values[idx]
                                        if isinstance(val, ast.List):
                                            for elem in val.elts:
                                                raw = getattr(elem, 'value', getattr(elem, 's', ''))
                                                if isinstance(raw, str):
                                                    deps.add(raw.split("^")[0].split("=")[0].strip())
            except Exception:
                pass
    return deps

def get_full_deps():
    return sorted(BASE_DEPS | find_plugin_deps())

def run(cmd, check=False, env=None, timeout=600, silent=False):
    try:
        if not silent:
            print(f"[RUN] {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        if proc.stdout and not silent:
            print(proc.stdout)
        if proc.stderr and not silent:
            print(proc.stderr, file=sys.stderr)
        if check and proc.returncode != 0:
            sys.exit(proc.returncode)
        return proc
    except subprocess.TimeoutExpired:
        print("Command timed out")
        return type('Proc', (), {'returncode': 1, 'stdout': '', 'stderr': 'Timeout'})()
    except Exception as e:
        print(f"Run error: {e}")
        return type('Proc', (), {'returncode': 1, 'stdout': '', 'stderr': str(e)})()

def ensure_venv():
    venv_py = Path(PY_EXE)
    if VENV.exists():
        if not venv_py.exists():
            shutil.rmtree(VENV, ignore_errors=True)
        else:
            try:
                subprocess.run([str(venv_py), "--version"], capture_output=True, check=True, timeout=10)
            except Exception:
                print("⚠️ Virtual environment is broken. Recreating...")
                shutil.rmtree(VENV, ignore_errors=True)

    if not VENV.exists():
        base_py = get_base_python()
        if not base_py:
            print("❌ No Python found! Please run setup.bat and choose 'F' then 'G' to install portable Python.")
            sys.exit(1)
        print(f"📦 Creating virtual environment from {base_py}...")
        run([base_py, "-m", "venv", str(VENV)], check=True)
        if not venv_py.exists():
            print(f"❌ Error: {venv_py} not found")
            sys.exit(1)

def check_pip():
    if subprocess.run([PY_EXE, "-m", "pip", "--version"], capture_output=True).returncode != 0:
        print("❌ Pip not available in venv.")
        sys.exit(1)

def check_import(package: str) -> bool:
    import_name = package.split('[')[0].replace('-', '_')
    mapping = {
        "beautifulsoup4": "bs4",
        "Pillow": "PIL",
        "python_docx": "docx",
        "python_pptx": "pptx",
        "patool": None,
        "readability_lxml": "readability",
        "sentence-transformers": "sentence_transformers",
        "scikit-learn": "sklearn",
    }
    if package in mapping:
        if mapping[package] is None:
            return True
        import_name = mapping[package]
    if not import_name.isidentifier():
        return True  # пропускаем
    try:
        subprocess.run([PY_EXE, "-c", f"import {import_name}"], capture_output=True, check=True, timeout=30)
        return True
    except subprocess.CalledProcessError:
        return False

def install_with_mirrors(packages, upgrade=False):
    if not packages:
        return True
    cmd_base = PIP_CMD + (["install", "--upgrade"] if upgrade else ["install"])
    for mirror in PIP_MIRRORS:
        cmd = cmd_base.copy()
        if mirror:
            cmd += ["--index-url", mirror]
        cmd += packages
        if run(cmd, silent=True).returncode == 0:
            return True
    return False

def download_with_mirrors(packages):
    if not packages:
        return True
    DEPS_DIR.mkdir(exist_ok=True)
    for mirror in PIP_MIRRORS:
        cmd = PIP_CMD + ["download", "-d", str(DEPS_DIR), "--prefer-binary"]
        if mirror:
            cmd += ["--index-url", mirror]
        cmd += packages
        if run(cmd, silent=True).returncode == 0:
            return True
    return False

def ensure_playwright_browsers():
    try:
        subprocess.run([PY_EXE, "-m", "playwright", "install", "chromium"], capture_output=True, check=True, timeout=300)
        print("✅ Playwright browser installed.")
    except Exception as e:
        print(f"⚠️ Playwright browser install failed: {e}")

def check_and_install_missing():
    ensure_venv()
    check_pip()
    load_dotenv_if_exists()
    deps = get_full_deps()
    missing = [dep for dep in deps if not check_import(dep)]
    if not missing:
        print("✅ All dependencies are already installed.")
        ensure_playwright_browsers()
        return

    print(f"⚠️ Missing: {', '.join(missing)}")
    local_whls = {whl.stem.split('-')[0].lower() for whl in DEPS_DIR.glob("*.whl")} if DEPS_DIR.exists() else set()

    offline_pkgs = [pkg for pkg in missing if pkg.lower() in local_whls]
    online_pkgs = [pkg for pkg in missing if pkg.lower() not in local_whls]

    if offline_pkgs:
        run(PIP_CMD + ["install", "--no-index", "--find-links", str(DEPS_DIR), "--no-build-isolation"] + offline_pkgs)
    if online_pkgs:
        install_with_mirrors(online_pkgs)

    ensure_playwright_browsers()
    print("✅ Done.")

def online_mode():
    ensure_venv()
    check_pip()
    install_with_mirrors(["pip", "setuptools", "wheel"], upgrade=True)
    all_deps = get_full_deps()
    if not download_with_mirrors(all_deps):
        print("❌ Failed to download dependencies. Check internet connection and mirrors.")
        sys.exit(1)
    print(f"✅ Downloaded {len(all_deps)} packages to {DEPS_DIR}")

def offline_mode():
    if not DEPS_DIR.exists() or not any(DEPS_DIR.glob("*.whl")):
        print("❌ python_deps is empty or missing. Run --online first to download packages.")
        sys.exit(1)
    ensure_venv()
    check_pip()
    run(PIP_CMD + ["install", "--no-index", "--find-links", str(DEPS_DIR), "--no-build-isolation"] + get_full_deps())
    ensure_playwright_browsers()
    print("✅ Installed all dependencies from local cache.")

def fix_config(config_path: str, python_exe: str):
    config_file = Path(config_path)
    if not config_file.exists():
        return 1
    backup = config_file.with_name(f"{config_file.name}.backup_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(config_file, backup)
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except Exception:
        return 1

    counter = {"n": 0}
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k == "command" and isinstance(v, str) and "python" in v.lower():
                    obj[k] = python_exe
                    counter["n"] += 1
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)
    walk(data)
    config_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✅ Updated {counter['n']} entries in {config_file}")
    return 0

def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--online", action="store_true", help="Скачать все зависимости в python_deps")
    group.add_argument("--offline", action="store_true", help="Установить зависимости из python_deps")
    group.add_argument("--check", action="store_true", help="Проверить и установить недостающие")
    parser.add_argument("--fix-config", nargs=2, metavar=("CONFIG_FILE", "PYTHON_EXE"), help="Исправить пути в JSON конфиге")
    args = parser.parse_args()

    if args.fix_config:
        sys.exit(fix_config(args.fix_config[0], args.fix_config[1]))
    elif args.online:
        online_mode()
    elif args.offline:
        offline_mode()
    elif args.check:
        check_and_install_missing()
    else:
        parser.print_help()

if __name__ == "__main__":
    main()