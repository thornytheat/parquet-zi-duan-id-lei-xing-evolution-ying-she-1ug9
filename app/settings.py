"""路径与常量：样例目录、发布存储目录（文件持久化，可重跑、可回看）。"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Parquet 字段演进映射 · schemaevo"
BASE_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = Path(os.environ.get("SAMPLE_DIR", BASE_DIR / "sample_data"))
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
RELEASE_DIR = DATA_DIR / "releases"
CURRENT_FILE = DATA_DIR / "current.json"
MATRIX_VERSION = "type-matrix/2026.11"
SAMPLE_KEYS = ("A", "B", "C")
MAX_RELEASES_KEPT = 200
