"""发布持久化：data/releases/*.json + data/current.json（当前生效版指针）。

一致性约定：
- 所有写入都走「临时文件 + os.replace」原子替换，读到的一半文件不存在；
- 「发布」= 原子替换 current.json 里该表的指针，这是唯一的切换点：
  切换成功则结果页 / 历史 / 下载同时指向新版本；切换失败 current.json 原样不动，
  上一版继续生效；
- 相同指纹的冻结输入只对应一个 release，重试不新增版本。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone

from . import settings

RELEASE_ID_RE = re.compile(r"^rel-\d{8}T\d{6}-[0-9a-f]{6}$")


def new_release_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"rel-{stamp}-{uuid.uuid4().hex[:6]}"


def _ensure_dir() -> None:
    settings.RELEASE_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write(path, text: str) -> None:
    """先写临时文件再 os.replace：同一文件系统内替换是原子的。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _path(release_id: str):
    if not RELEASE_ID_RE.match(release_id or ""):
        raise KeyError(f"发布记录 {release_id!r} 不存在")
    return settings.RELEASE_DIR / f"{release_id}.json"


def save_release(release: dict) -> dict:
    _ensure_dir()
    release = dict(release)
    release["release_id"] = release.get("release_id") or new_release_id()
    _atomic_write(_path(release["release_id"]), json.dumps(release, ensure_ascii=False, indent=2))
    _prune()
    return release


def load_release(release_id: str) -> dict:
    path = _path(release_id)
    if not path.exists():
        raise KeyError(f"发布记录 {release_id!r} 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_releases() -> list[dict]:
    _ensure_dir()
    files = sorted(settings.RELEASE_DIR.glob("rel-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    releases = []
    for path in files:
        try:
            releases.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return releases


def list_releases(limit: int = 50) -> list[dict]:
    return _iter_releases()[:limit]


def find_by_fingerprint(fp: str) -> dict | None:
    """幂等键查找：同一冻结输入（同指纹）永远找到同一个 release。"""
    if not fp:
        return None
    for release in _iter_releases():
        if release.get("fingerprint") == fp:
            return release
    return None


def next_version(table: str) -> int:
    """每表单调递增的版本号；同指纹重试不会走到这里（先被 find_by_fingerprint 截获）。"""
    versions = [int(r.get("version") or 0) for r in _iter_releases() if r.get("table") == table]
    return (max(versions) if versions else 0) + 1


# ---------------------------------------------------------------- 当前生效版指针

def load_current() -> dict:
    """{表名: {"release_id": ..., "switched_at": ...}}；文件缺失或损坏都视为无生效版。"""
    try:
        doc = json.loads(settings.CURRENT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def set_current(table: str, release_id: str) -> dict:
    """原子切换当前生效版：替换成功即发布完成；抛异常则上一版原样保留。"""
    _ensure_dir()
    current = load_current()
    current[table] = {
        "release_id": release_id,
        "switched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    _atomic_write(settings.CURRENT_FILE, json.dumps(current, ensure_ascii=False, indent=2))
    return current


def _prune() -> None:
    """裁剪历史，但任何表的当前生效版永远保留。"""
    keep_ids = {entry.get("release_id") for entry in load_current().values()}
    files = sorted(settings.RELEASE_DIR.glob("rel-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    kept = 0
    for path in files:
        release_id = path.stem
        if kept < settings.MAX_RELEASES_KEPT or release_id in keep_ids:
            kept += 1
            continue
        try:
            path.unlink()
        except OSError:
            pass
