"""API + 端到端：受控发布全流程（预演 → 发布 → 失效），含 A/B/C 三组真实样例。"""
import pytest
from fastapi.testclient import TestClient

from app import settings, store
from app.main import app

client = TestClient(app)


def preview(payload):
    return client.post("/api/releases/preview", json=payload)


def publish(release_id):
    return client.post(f"/api/releases/{release_id}/publish")


def get_release(release_id):
    return client.get(f"/api/releases/{release_id}")


def custom_payload(table, old_fields, new_fields, files):
    return {
        "old": {"table": table, "fields": old_fields},
        "new": {"table": table, "fields": new_fields},
        "consumers": {"table": table, "files": files},
    }


# ---------------------------------------------------------------- 基础页面

def test_health_ok():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_index_and_workbench_render():
    home = client.get("/")
    assert home.status_code == 200
    assert "Parquet 字段 ID 与类型演进映射" in home.text
    bench = client.get("/workbench?sample=B")
    assert bench.status_code == 200
    assert "dwd_payment" in bench.text


# ---------------------------------------------------------------- 样例 A/B/C 主流程

def test_sample_a_preview_publish_and_download():
    body = preview({"sample": "A"}).json()
    assert body["summary"]["level"] == "full"
    assert body["summary"]["sql_steps"] == 0
    assert body["state"] == "preview"

    res = publish(body["release_id"])
    assert res.status_code == 200
    assert res.json()["state"] == "published"

    page = client.get(f"/result/{body['release_id']}")
    assert page.status_code == 200
    assert "当前生效版" in page.text

    sql = client.get(f"/api/releases/{body['release_id']}/migration.sql")
    assert sql.status_code == 200
    assert "attachment" in sql.headers.get("content-disposition", "")


def test_sample_b_rename_added_backfill_and_alter_order():
    body = preview({"sample": "B"}).json()
    summary = body["summary"]
    assert summary["level"] == "backward"
    assert summary["renamed"] == 1
    assert summary["added"] == 2
    assert summary["backfill_count"] == 1
    assert summary["sql_steps"] == 4

    res = publish(body["release_id"])
    assert res.status_code == 200, res.json()

    run = get_release(body["release_id"]).json()
    assert run["state"] == "published"
    sql = run["result"]["migration"]["sql_text"]
    assert "RENAME COLUMN channel TO pay_channel" in sql
    assert "ADD COLUMN fee" in sql
    assert "ALTER COLUMN amount TYPE decimal(18,2)" in sql
    assert sql.index("RENAME COLUMN") < sql.index("ADD COLUMN") < sql.index("ALTER COLUMN")
    assert run["result"]["consumers_check"]["needs_update_count"] == 2

    dl = client.get(f"/api/releases/{body['release_id']}/migration.sql")
    assert dl.status_code == 200
    assert "RENAME COLUMN" in dl.text


def test_sample_c_blocked_cannot_publish():
    body = preview({"sample": "C"}).json()
    summary = body["summary"]
    assert summary["level"] == "none"
    assert summary["blocking_count"] == 3
    assert summary["can_execute"] is False
    assert summary["broken_consumers"] > 0

    res = publish(body["release_id"])
    assert res.status_code == 409
    detail = res.json()
    assert "阻断" in detail["detail"]
    assert len(detail["blockers"]) >= 3

    run = get_release(body["release_id"]).json()
    assert run["state"] == "preview"
    codes = {b["code"] for b in run["result"]["block_reasons"]}
    assert codes == {"type_blocked", "partition_type_changed", "field_removed"}

    # 预演版不是可执行方案：下载一律 409
    assert client.get(f"/api/releases/{body['release_id']}/migration.sql").status_code == 409


# ---------------------------------------------------------------- 幂等：相同冻结输入不新增版本

def test_preview_is_idempotent_for_same_frozen_input():
    payload = custom_payload(
        "t_idem",
        [{"id": 1, "name": "a", "type": "int32"}],
        [{"id": 1, "name": "a", "type": "int64"}],
        [{"path": "s3://x/p0.parquet", "columns": ["a"]}],
    )
    first = preview(payload).json()
    assert first["created"] is True
    second = preview(payload).json()
    assert second["created"] is False
    assert second["release_id"] == first["release_id"]
    assert second["version"] == first["version"]
    # 键序不同、语义相同也算同一输入
    shuffled = {
        "consumers": {"files": [{"columns": ["a"], "path": "s3://x/p0.parquet"}], "table": "t_idem"},
        "new": {"fields": [{"type": "int64", "name": "a", "id": 1}], "table": "t_idem"},
        "old": {"fields": [{"type": "int32", "name": "a", "id": 1}], "table": "t_idem"},
    }
    assert preview(shuffled).json()["release_id"] == first["release_id"]


def test_new_input_gets_new_version():
    base = custom_payload("t_ver", [{"id": 1, "name": "a", "type": "int32"}],
                          [{"id": 1, "name": "a", "type": "int64"}],
                          [{"path": "s3://x/p0.parquet", "columns": ["a"]}])
    v1 = preview(base).json()
    changed = custom_payload("t_ver", [{"id": 1, "name": "a", "type": "int32"}],
                             [{"id": 1, "name": "a", "type": "int64"}, {"id": 2, "name": "b", "type": "string"}],
                             [{"path": "s3://x/p0.parquet", "columns": ["a"]}])
    v2 = preview(changed).json()
    assert v2["release_id"] != v1["release_id"]
    assert v2["version"] == v1["version"] + 1


def test_publish_is_idempotent_noop():
    body = preview(custom_payload("t_pubidem", [{"id": 1, "name": "a", "type": "int32"}],
                                  [{"id": 1, "name": "a", "type": "int64"}],
                                  [{"path": "s3://x/p0.parquet", "columns": ["a"]}])).json()
    assert publish(body["release_id"]).status_code == 200
    again = publish(body["release_id"])
    assert again.status_code == 200
    assert again.json()["state"] == "published"
    assert get_release(body["release_id"]).json()["version"] == body["version"]


# ---------------------------------------------------------------- 失败保留上一版，重试不新增版本

def test_failed_publish_keeps_previous_and_retry_reuses_same_release(monkeypatch):
    v1 = preview(custom_payload("t_fail", [{"id": 1, "name": "a", "type": "int32"}],
                                [{"id": 1, "name": "a", "type": "int64"}],
                                [{"path": "s3://x/p0.parquet", "columns": ["a"]}])).json()
    assert publish(v1["release_id"]).status_code == 200

    v2 = preview(custom_payload("t_fail", [{"id": 1, "name": "a", "type": "int32"}],
                                [{"id": 1, "name": "a", "type": "int64"}, {"id": 2, "name": "b", "type": "string"}],
                                [{"path": "s3://x/p0.parquet", "columns": ["a"]}])).json()

    def boom(table, release_id):
        raise OSError("disk full")

    monkeypatch.setattr(store, "set_current", boom)
    res = publish(v2["release_id"])
    assert res.status_code == 500
    assert "上一版" in res.json()["detail"]

    # 上一版仍是生效版；失败版不可下载，结果页展示失败横幅与重试入口
    assert get_release(v2["release_id"]).json()["state"] == "failed"
    assert client.get(f"/api/releases/{v1['release_id']}/migration.sql").status_code == 200
    assert client.get(f"/api/releases/{v2['release_id']}/migration.sql").status_code == 409
    failed_page = client.get(f"/result/{v2['release_id']}")
    assert "发布失败" in failed_page.text
    assert "重试发布" in failed_page.text

    monkeypatch.undo()
    # 相同冻结输入重试：回到同一个 release，不新增版本
    retry = preview(custom_payload("t_fail", [{"id": 1, "name": "a", "type": "int32"}],
                                   [{"id": 1, "name": "a", "type": "int64"}, {"id": 2, "name": "b", "type": "string"}],
                                   [{"path": "s3://x/p0.parquet", "columns": ["a"]}])).json()
    assert retry["created"] is False
    assert retry["release_id"] == v2["release_id"]
    assert retry["version"] == v2["version"]

    assert publish(v2["release_id"]).status_code == 200
    current = store.load_current()
    assert current["t_fail"]["release_id"] == v2["release_id"]
    assert get_release(v1["release_id"]).json()["state"] == "superseded"


# ---------------------------------------------------------------- 规则 / 清单变化后旧方案失效

def test_rules_change_invalidates_old_release(monkeypatch):
    payload = custom_payload("t_stale", [{"id": 1, "name": "a", "type": "int32"}],
                             [{"id": 1, "name": "a", "type": "int64"}],
                             [{"path": "s3://x/p0.parquet", "columns": ["a"]}])
    v1 = preview(payload).json()
    assert publish(v1["release_id"]).status_code == 200
    assert client.get(f"/api/releases/{v1['release_id']}/migration.sql").status_code == 200

    monkeypatch.setattr(settings, "MATRIX_VERSION", "type-matrix/2099.01")

    # 旧方案失效：状态 stale、下载 409、不可再发布，结果页展示失效横幅
    assert get_release(v1["release_id"]).json()["state"] == "stale"
    assert client.get(f"/api/releases/{v1['release_id']}/migration.sql").status_code == 409
    assert publish(v1["release_id"]).status_code == 409
    assert "已失效" in client.get(f"/result/{v1['release_id']}").text

    # 相同输入按新规则重新预演 → 新版本（指纹含规则摘要）
    v2 = preview(payload).json()
    assert v2["created"] is True
    assert v2["release_id"] != v1["release_id"]
    assert publish(v2["release_id"]).status_code == 200
    assert store.load_current()["t_stale"]["release_id"] == v2["release_id"]


def test_consumer_list_change_supersedes_old_release():
    v1 = preview(custom_payload("t_super", [{"id": 1, "name": "a", "type": "int32"}],
                                [{"id": 1, "name": "a", "type": "int64"}],
                                [{"path": "s3://x/p0.parquet", "columns": ["a"]}])).json()
    assert publish(v1["release_id"]).status_code == 200

    # 读取方清单变化 → 新指纹 → 新版本；发布后旧版被取代、脚本失效
    v2 = preview(custom_payload("t_super", [{"id": 1, "name": "a", "type": "int32"}],
                                [{"id": 1, "name": "a", "type": "int64"}],
                                [{"path": "s3://x/p0.parquet", "columns": ["a"]},
                                 {"path": "s3://x/p1.parquet", "columns": ["a"]}])).json()
    assert v2["release_id"] != v1["release_id"]
    assert publish(v2["release_id"]).status_code == 200

    old = get_release(v1["release_id"]).json()
    assert old["state"] == "superseded"
    assert client.get(f"/api/releases/{v1['release_id']}/migration.sql").status_code == 409
    page = client.get(f"/result/{v1['release_id']}")
    assert "已失效" in page.text
    assert client.get(f"/api/releases/{v2['release_id']}/migration.sql").status_code == 200


def test_broken_consumers_block_publish():
    # 删列本身不阻断迁移（can_execute=True），但 broken_consumers 必须拦住发布
    body = preview(custom_payload(
        "t_broken",
        [{"id": 1, "name": "a", "type": "int32"}, {"id": 2, "name": "b", "type": "string"}],
        [{"id": 1, "name": "a", "type": "int64"}],
        [{"path": "s3://x/p0.parquet", "columns": ["a", "b"]}],
    )).json()
    assert body["summary"]["can_execute"] is True
    assert body["summary"]["broken_consumers"] == 1
    res = publish(body["release_id"])
    assert res.status_code == 409
    codes = {b["code"] for b in res.json()["blockers"]}
    assert "broken_consumer" in codes


# ---------------------------------------------------------------- 结果页 / 历史 / 下载指向同一版

def test_result_history_and_download_point_to_same_version():
    body = preview(custom_payload("t_same", [{"id": 1, "name": "a", "type": "int32"}],
                                  [{"id": 1, "name": "a", "type": "int64"}],
                                  [{"path": "s3://x/p0.parquet", "columns": ["a"]}])).json()
    assert publish(body["release_id"]).status_code == 200

    page = client.get(f"/result/{body['release_id']}")
    assert page.status_code == 200
    assert "当前生效版" in page.text
    assert f"/api/releases/{body['release_id']}/migration.sql" in page.text

    rows = client.get("/api/releases").json()["releases"]
    row = next(r for r in rows if r["release_id"] == body["release_id"])
    assert row["is_current"] is True
    assert row["state"] == "published"

    review = client.get("/review")
    assert review.status_code == 200
    assert body["release_id"] in review.text
    assert "当前生效版" in review.text

    sql = client.get(f"/api/releases/{body['release_id']}/migration.sql")
    assert sql.status_code == 200
    assert "ALTER TABLE t_same" in sql.text


# ---------------------------------------------------------------- 输入校验与 404

@pytest.mark.parametrize("payload,needle", [
    ({"old": "{bad json", "new": {}, "consumers": {}}, "不是合法 JSON"),
    ({"old": {"table": "t", "fields": []}, "new": {"table": "t", "fields": []},
      "consumers": {"files": []}}, "fields 必须是非空数组"),
    ({"old": {"table": "t", "fields": [{"name": "a", "type": "money"}]},
      "new": {"table": "t", "fields": [{"name": "a", "type": "string"}]},
      "consumers": {"files": [{"path": "p", "columns": ["a"]}]}}, "类型不合法"),
    ({"old": {"table": "t", "fields": [{"id": 1, "name": "a", "type": "string"},
                                       {"id": 1, "name": "b", "type": "string"}]},
      "new": {"table": "t", "fields": [{"id": 1, "name": "a", "type": "string"}]},
      "consumers": {"files": [{"path": "p", "columns": ["a"]}]}}, "field_id 重复"),
    ({"old": {"table": "t1", "fields": [{"id": 1, "name": "a", "type": "string"}]},
      "new": {"table": "t2", "fields": [{"id": 1, "name": "a", "type": "string"}]},
      "consumers": {"files": [{"path": "p", "columns": ["a"]}]}}, "表名不一致"),
])
def test_bad_inputs_return_400(payload, needle):
    res = preview(payload)
    assert res.status_code == 400
    assert needle in res.json()["detail"]


def test_non_json_body_returns_400():
    res = client.post("/api/releases/preview", content="not-json", headers={"Content-Type": "application/json"})
    assert res.status_code == 400


def test_unknown_sample_returns_404():
    assert client.get("/api/samples/Z").status_code == 404


def test_missing_release_returns_404():
    assert client.get("/api/releases/rel-20990101T000000-abcdef").status_code == 404
    assert client.get("/result/rel-20990101T000000-abcdef").status_code == 404
    assert publish("rel-20990101T000000-abcdef").status_code == 404
    assert client.get("/api/releases/rel-20990101T000000-abcdef/migration.sql").status_code == 404


def test_review_page_and_history():
    preview({"sample": "A"})
    res = client.get("/review")
    assert res.status_code == 200
    assert "类型矩阵说明" in res.text
    rows = client.get("/api/releases").json()
    assert rows["count"] >= 1


def test_matrix_endpoint():
    body = client.get("/api/matrix").json()
    assert body["version"]
    assert any(row["verdict"] == "compatible" for row in body["promotions"])
    assert len(body["decimal_rules"]) == 4
