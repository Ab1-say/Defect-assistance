#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 冒烟测试：覆盖一条完整缺陷生命周期及越权校验。

用法（先启动 python server.py）：
    python scripts/smoke_test.py [base_url]
"""

import json
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def req(method: str, path: str, payload=None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise RuntimeError(f"{method} {path} -> {exc.code}: {body}") from exc


def expect_fail(method: str, path: str, payload, status: int) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(request)
    except urllib.error.HTTPError as exc:
        assert exc.code == status, f"期望 {status}，实际 {exc.code}"
        return
    raise AssertionError("预期失败的操作竟然成功了")


def main() -> None:
    users = req("GET", "/api/users")
    by_username = {u["username"]: u for u in users}
    qa = by_username["qa_lin"]
    pm = by_username["pm_wang"]
    dev = by_username["dev_zhang"]
    other_dev = by_username["dev_chen"]

    # 1. 测试人员提交
    created = req("POST", "/api/bugs", {
        "user_id": qa["id"],
        "title": "冒烟测试缺陷：首页白屏",
        "module": "首页",
        "severity": "S2",
        "description": "Safari 下刷新首页偶发白屏",
    })
    bug_id = created["id"]
    assert created["state"] == "NEW", created
    assert created["reporter"]["id"] == qa["id"]

    # 2. 产品确认并指派
    triaged = req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": pm["id"], "action": "triage",
        "priority": "P1", "assignee_id": dev["id"],
    })
    assert triaged["state"] == "OPEN"
    assert triaged["assignee"]["id"] == dev["id"]

    # 3. 未指派的开发不能开始处理
    expect_fail("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": other_dev["id"], "action": "start",
    }, 403)

    # 4. 处理人开始修复并提交修复
    started = req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": dev["id"], "action": "start",
    })
    assert started["state"] == "IN_PROGRESS"
    fixed = req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": dev["id"], "action": "fix", "comment": "修复路由懒加载竞态",
    })
    assert fixed["state"] == "FIXED"

    # 5. 产品不能替测试做验证
    expect_fail("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": pm["id"], "action": "verify_pass",
    }, 403)

    # 6. 测试验证不通过 -> 回到待处理
    reopened = req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": qa["id"], "action": "verify_fail",
        "comment": "修复后在 iOS 15 上仍能复现",
    })
    assert reopened["state"] == "OPEN"

    # 7. 再次修复并验证通过 -> 关闭
    req("POST", f"/api/bugs/{bug_id}/actions", {"user_id": dev["id"], "action": "start"})
    req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": dev["id"], "action": "fix", "comment": "补充兼容层处理",
    })
    closed = req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": qa["id"], "action": "verify_pass",
    })
    assert closed["state"] == "CLOSED"

    # 8. 回归后测试重开
    reopened2 = req("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": qa["id"], "action": "reopen", "comment": "灰度包中再次出现",
    })
    assert reopened2["state"] == "OPEN"

    # 9. 无说明的驳回必须失败
    expect_fail("POST", f"/api/bugs/{bug_id}/actions", {
        "user_id": pm["id"], "action": "reject",
    }, 400)

    print(f"SMOKE OK: 完成 {len(reopened2['timeline'])} 条流转记录，BUG-{bug_id:04d}", flush=True)


if __name__ == "__main__":
    main()
