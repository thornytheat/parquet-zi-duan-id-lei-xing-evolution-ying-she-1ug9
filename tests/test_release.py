"""受控发布纯函数：冻结指纹、派生状态、发布闸门。"""
import pytest

from app import release as rel
from app import settings


def schema(table, fields):
    return {
        "table": table,
        "fields": [
            {"id": f[0], "name": f[1], "type": f[2], "nullable": True, "default": None, "partition": False}
            for f in fields
        ],
        "partitions": [],
    }


def consumers(table, *path_columns):
    return {
        "table": table,
        "files": [{"path": p, "columns": c} for p, c in path_columns],
    }


def make_release(old, new, cons, source="测试"):
    frozen = rel.freeze_inputs(old, new, cons)
    return rel.build_release(old, new, cons, source, frozen, rel.fingerprint(frozen), version=1)


# ---------------------------------------------------------------- 指纹

def test_fingerprint_stable_for_same_semantic_input():
    old = schema("t_fp", [(1, "a", "int32")])
    new = schema("t_fp", [(1, "a", "int64")])
    cons = consumers("t_fp", ("s3://x/p0.parquet", ["a"]))
    fp1 = rel.fingerprint(rel.freeze_inputs(old, new, cons))
    # 键序不同、语义相同 → 同一指纹（与粘贴文本的格式无关）
    old2 = {"fields": [{"partition": False, "default": None, "nullable": True, "type": "int32", "name": "a", "id": 1}],
            "table": "t_fp", "partitions": []}
    fp2 = rel.fingerprint(rel.freeze_inputs(old2, new, cons))
    assert fp1 == fp2


def test_fingerprint_changes_with_any_frozen_input():
    old = schema("t_fp", [(1, "a", "int32")])
    new = schema("t_fp", [(1, "a", "int64")])
    cons = consumers("t_fp", ("s3://x/p0.parquet", ["a"]))
    base = rel.fingerprint(rel.freeze_inputs(old, new, cons))
    assert rel.fingerprint(rel.freeze_inputs(old, new, consumers("t_fp", ("s3://x/p1.parquet", ["a"])))) != base
    assert rel.fingerprint(rel.freeze_inputs(old, schema("t_fp", [(1, "a", "int32"), (2, "b", "string")]), cons)) != base


def test_fingerprint_changes_when_rules_change(monkeypatch):
    old = schema("t_fp", [(1, "a", "int32")])
    new = schema("t_fp", [(1, "a", "int64")])
    cons = consumers("t_fp", ("s3://x/p0.parquet", ["a"]))
    frozen_before = rel.freeze_inputs(old, new, cons)
    digest_before = frozen_before["rules"]["digest"]
    fp_before = rel.fingerprint(frozen_before)
    monkeypatch.setattr(settings, "MATRIX_VERSION", "type-matrix/2099.01")
    frozen_after = rel.freeze_inputs(old, new, cons)
    assert frozen_after["rules"]["digest"] != digest_before
    assert rel.fingerprint(frozen_after) != fp_before
    assert rel.current_rules_digest() != digest_before


# ---------------------------------------------------------------- 派生状态

def _release_with(status="preview", published_at=None, digest="rules-1"):
    return {
        "release_id": "rel-20260101T000000-aaaaaa",
        "table": "t_state",
        "status": status,
        "published_at": published_at,
        "frozen": {"rules": {"digest": digest}},
    }


def test_derive_state_published_and_stale_current():
    r = _release_with()
    assert rel.derive_state(r, r["release_id"], "rules-1") == "published"
    # 当前版但规则已更新 → 失效
    assert rel.derive_state(r, r["release_id"], "rules-2") == "stale"


def test_derive_state_superseded_and_stale_draft():
    r = _release_with(published_at="2026-01-01T00:00:00+08:00")
    assert rel.derive_state(r, "rel-other", "rules-1") == "superseded"
    draft = _release_with()
    assert rel.derive_state(draft, "rel-other", "rules-2") == "stale"


def test_derive_state_preview_and_failed():
    assert rel.derive_state(_release_with(), None, "rules-1") == "preview"
    assert rel.derive_state(_release_with(status="failed"), None, "rules-1") == "failed"


def test_only_published_is_downloadable():
    view = rel.release_view(_release_with(), {"t_state": {"release_id": "rel-20260101T000000-aaaaaa"}}, "rules-1")
    assert view["state"] == "published" and view["downloadable"] is True
    view = rel.release_view(_release_with(), {}, "rules-1")
    assert view["state"] == "preview" and view["downloadable"] is False


# ---------------------------------------------------------------- 发布闸门

def test_publish_blockers_empty_for_clean_change():
    r = make_release(schema("t_gate", [(1, "a", "int32")]), schema("t_gate", [(1, "a", "int64")]),
                     consumers("t_gate", ("s3://x/p0.parquet", ["a"])))
    assert rel.publish_blockers(r, rel.current_rules_digest()) == []


def test_publish_blockers_include_type_block_and_manual_confirm():
    # int64→int32 被矩阵阻断
    r = make_release(schema("t_gate", [(1, "a", "int64")]), schema("t_gate", [(1, "a", "int32")]),
                     consumers("t_gate", ("s3://x/p0.parquet", ["a"])))
    codes = {b["code"] for b in rel.publish_blockers(r, rel.current_rules_digest())}
    assert "type_blocked" in codes
    # id_changed 需人工确认
    r2 = make_release(schema("t_gate", [(1, "a", "int32")]), schema("t_gate", [(9, "a", "int32")]),
                      consumers("t_gate", ("s3://x/p0.parquet", ["a"])))
    codes2 = {b["code"] for b in rel.publish_blockers(r2, rel.current_rules_digest())}
    assert "id_changed" in codes2


def test_publish_blockers_include_broken_consumers():
    # 删列本身不阻断迁移（can_execute=True），但引用被删列的读取方必须拦住发布
    r = make_release(
        schema("t_gate", [(1, "a", "int32"), (2, "b", "string")]),
        schema("t_gate", [(1, "a", "int64")]),
        consumers("t_gate", ("s3://x/p0.parquet", ["a", "b"])),
    )
    assert r["result"]["migration"]["can_execute"] is True
    blockers = rel.publish_blockers(r, rel.current_rules_digest())
    assert any(b["code"] == "broken_consumer" and b["field"] == "b" for b in blockers)


def test_publish_blockers_rules_stale_short_circuits():
    r = make_release(schema("t_gate", [(1, "a", "int32")]), schema("t_gate", [(1, "a", "int64")]),
                     consumers("t_gate", ("s3://x/p0.parquet", ["a"])))
    blockers = rel.publish_blockers(r, "some-other-digest")
    assert [b["code"] for b in blockers] == ["rules_stale"]
