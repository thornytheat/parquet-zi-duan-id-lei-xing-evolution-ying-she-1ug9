"""受控发布编排：预演（幂等）与发布（闸门 + 原子切换 + 失败保留上一版）。

纯计算在 release.py / report.py，持久化在 store.py；本模块只负责把三者接起来。
"""
from __future__ import annotations

from . import release as rel
from . import store


def preview_release(old: dict, new: dict, consumers: dict, source: str) -> tuple[dict, bool]:
    """生成预演：相同冻结输入（同指纹）直接返回已有 release，不新增版本。

    返回 (release, created)；created=False 表示这是同指纹重试。
    """
    frozen = rel.freeze_inputs(old, new, consumers)
    fp = rel.fingerprint(frozen)
    existing = store.find_by_fingerprint(fp)
    if existing is not None:
        return existing, False
    draft = rel.build_release(
        old, new, consumers, source, frozen, fp,
        version=store.next_version(new["table"]),
    )
    return store.save_release(draft), True


def publish_release(release_id: str) -> dict:
    """把指定 release 一次性切换为当前生效版。

    - 已是当前版：幂等返回（重复发布不新增版本、不动指针）；
    - 已失效（规则更新 / 已被取代）：拒绝，需重新预演；
    - 阻断未全部消除：拒绝并返回阻断清单；
    - 切换动作 = 原子替换 current.json；任何 IO 失败都把 release 标为 failed，
      current.json 不被触碰，上一版继续生效。
    """
    release = store.load_release(release_id)  # KeyError → 路由层 404
    rules_now = rel.current_rules_digest()
    current = store.load_current()
    current_id = (current.get(release["table"]) or {}).get("release_id")
    state = rel.derive_state(release, current_id, rules_now)

    if state == "published":
        return release  # 幂等：重复发布同一版 = 无操作
    if state == "stale":
        raise rel.ReleaseConflict(
            "该版本冻结的规则已更新，旧方案已失效；请按当前规则重新预演",
            state="stale",
        )
    if state == "superseded":
        raise rel.ReleaseConflict(
            f"该版本已被 {current_id} 取代，不能再发布；如需回滚请重新预演相同输入",
            state="superseded",
        )

    blockers = rel.publish_blockers(release, rules_now)
    if blockers:
        raise rel.ReleaseBlocked(
            f"存在 {len(blockers)} 项未消除的阻断，全部消除后才允许发布",
            blockers=blockers,
        )

    release["attempts"] = int(release.get("attempts") or 0) + 1
    release["error"] = None
    try:
        store.save_release(release)
        # 唯一的切换点：原子替换当前版指针。失败则上一版原样保留。
        store.set_current(release["table"], release["release_id"])
    except OSError as exc:
        release["status"] = "failed"
        release["error"] = f"{type(exc).__name__}: {exc}"
        try:
            store.save_release(release)
        except OSError:
            pass
        raise rel.PublishFailed(
            f"发布失败（{exc}），上一版仍生效；可重试，重试不新增版本",
            state="failed",
        ) from exc

    # 切换已生效；补记发布时间（仅展示用，写失败不影响一致性）
    release["published_at"] = rel.now_iso()
    try:
        store.save_release(release)
    except OSError:
        pass
    return release
