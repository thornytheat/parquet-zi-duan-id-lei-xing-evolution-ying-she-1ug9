"""受控发布（纯函数）：冻结输入 → 指纹 → 预演 → 派生状态 → 发布闸门。

一次对比 = 一个 release。release 冻结本次规则快照（矩阵版本 + 完整矩阵内容）、
两版 schema 与读取方清单，指纹相同即同一次发布，重试不新增版本。

状态机（存储态只有 preview / failed，其余由 current 指针与规则摘要派生）：
    preview    预演中，未发布
    published  当前生效版（current 指针指向它，且规则未变）——唯一可下载脚本的状态
    failed     发布失败，上一版仍生效；可用同一冻结输入重试，不新增版本
    stale      规则/口径已更新（冻结的规则摘要与当前不一致）→ 旧方案失效
    superseded 曾发布，但已被同表更新版本取代 → 旧方案失效
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import report, settings
from .types import DECIMAL_RULES, matrix_table

STATE_LABELS = {
    "preview": "预演（未发布）",
    "published": "当前生效版",
    "failed": "发布失败（上一版仍生效）",
    "stale": "已失效（规则已更新）",
    "superseded": "已失效（被更新版本取代）",
}

STATE_TAGS = {
    "preview": "info",
    "published": "ok",
    "failed": "err",
    "stale": "err",
    "superseded": "warn",
}

#: 只有「当前生效版」的脚本才可作为可执行方案下载
DOWNLOADABLE_STATES = frozenset({"published"})


class ReleaseError(Exception):
    """受控发布流程异常的基类。"""

    status_code = 400

    def __init__(self, detail: str, **extra):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class ReleaseBlocked(ReleaseError):
    """阻断未全部消除，不允许发布。"""

    status_code = 409


class ReleaseConflict(ReleaseError):
    """版本已失效（规则更新 / 已被取代），需重新预演。"""

    status_code = 409


class PublishFailed(ReleaseError):
    """发布动作本身失败（IO 等），上一版保留。"""

    status_code = 500


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 冻结与指纹

def rules_snapshot() -> dict:
    """当前规则快照：矩阵版本 + 完整矩阵内容（内容变了即使版本号没动也算规则变化）。"""
    return {
        "matrix_version": settings.MATRIX_VERSION,
        "promotions": matrix_table(),
        "decimal_rules": list(DECIMAL_RULES),
    }


def freeze_inputs(old: dict, new: dict, consumers: dict) -> dict:
    """冻结本次发布的全部判定输入：规则快照 + 两版 schema + 读取方清单。

    注意只放规范化后的语义内容（与粘贴文本的缩进/键序无关），
    source 等展示用元数据不进冻结区，避免同一输入换来源描述就生成新版本。
    """
    rules = rules_snapshot()
    return {
        "rules": {
            **rules,
            "digest": _digest(rules),
        },
        "old": old,
        "new": new,
        "consumers": consumers,
    }


def fingerprint(frozen: dict) -> str:
    """冻结输入的全量指纹；相同指纹 = 同一次发布（幂等键）。"""
    return _digest(frozen)


def current_rules_digest() -> str:
    return _digest(rules_snapshot())


# ---------------------------------------------------------------- 构建

def build_release(old: dict, new: dict, consumers: dict, source: str,
                  frozen: dict, fp: str, version: int) -> dict:
    """生成预演中的 release：冻结输入 + 一次性算出的完整结果（回填与影响范围同一时刻）。"""
    result = report.build_run(old, new, consumers, source)
    return {
        "release_id": None,  # 由 store 落库时分配
        "table": new["table"],
        "version": version,
        "fingerprint": fp,
        "source": source,
        "status": "preview",
        "created_at": now_iso(),
        "published_at": None,
        "attempts": 0,
        "error": None,
        "frozen": frozen,
        "result": result,
    }


# ---------------------------------------------------------------- 派生状态

def _rules_stale(release: dict, rules_now: str) -> bool:
    return release.get("frozen", {}).get("rules", {}).get("digest") != rules_now


def derive_state(release: dict, current_id: str | None, rules_now: str) -> str:
    """由 current 指针与当前规则摘要派生展示状态（不落库，永远按读取时刻计算）。"""
    if current_id and current_id == release.get("release_id"):
        return "stale" if _rules_stale(release, rules_now) else "published"
    if release.get("published_at"):
        return "superseded"
    if _rules_stale(release, rules_now):
        return "stale"
    status = release.get("status")
    return status if status in ("preview", "failed") else "preview"


def release_view(release: dict, current_map: dict, rules_now: str) -> dict:
    """API / 模板共用的视图：release + 派生状态 + 展示辅助字段。"""
    table = release.get("table")
    current_entry = (current_map or {}).get(table) or {}
    current_id = current_entry.get("release_id")
    state = derive_state(release, current_id, rules_now)
    view = dict(release)
    view.update({
        "state": state,
        "state_label": STATE_LABELS[state],
        "state_tag": STATE_TAGS[state],
        "is_current": state == "published",
        "downloadable": state in DOWNLOADABLE_STATES,
        "short_fingerprint": (release.get("fingerprint") or "")[:12],
        "current_release_id": current_id,
        "rules_digest": release.get("frozen", {}).get("rules", {}).get("digest"),
    })
    return view


# ---------------------------------------------------------------- 发布闸门

def publish_blockers(release: dict, rules_now: str) -> list[dict]:
    """发布前必须全部消除的阻断清单；为空才允许切换到该版本。

    阻断 = 迁移阻断（不产出 ALTER 的项）+ 需人工确认项 + broken_consumers + 级别 none。
    注意不含「方向阻断」：它是 backward / forward 级别的成因（决定升级顺序），
    不是迁移计划的执行障碍——否则任何非 full 的变更都永远发不出去。
    """
    if _rules_stale(release, rules_now):
        return [{
            "code": "rules_stale",
            "field": None,
            "text": "规则（类型矩阵）已更新：本预演按旧规则冻结，结论不可信，请重新预演",
        }]

    result = release["result"]
    blockers: list[dict] = []
    if result["compat"]["level"] == "none":
        blockers.append({
            "code": "compat_level_none",
            "field": None,
            "text": "兼容级别 none：双向均被阻断，不允许直接发布，需改两阶段迁移",
        })
    for item in result["migration"].get("blocking", []):
        blockers.append({"code": item["code"], "field": item.get("field"), "text": item["text"]})
    for item in result["compat"].get("manual_confirm", []):
        blockers.append({
            "code": item["code"],
            "field": item.get("field"),
            "text": f"需人工确认：{item['text']}",
        })
    for item in result["consumers_check"].get("broken", []):
        blockers.append({
            "code": "broken_consumer",
            "field": item["column"],
            "text": f"{item['path']} 引用列 {item['column']}：{item['reason']}",
        })
    return blockers
