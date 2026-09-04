#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DefectFlow - a tiny defect collaboration system.

Runs with only the Python standard library:
    python server.py
Then open http://127.0.0.1:8000
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = BASE_DIR / "defect.db"
HOST = "127.0.0.1"
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000
RESET_DB = "--reset" in sys.argv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# --------------------------------------------------------------------------- #
# 领域常量：状态、动作、角色、优先级
# --------------------------------------------------------------------------- #

ROLE_LABELS = {
    "tester": "测试",
    "product": "产品",
    "developer": "开发",
}

STATE_LABELS = {
    "NEW": "待确认",
    "OPEN": "待处理",
    "IN_PROGRESS": "处理中",
    "FIXED": "待验证",
    "CLOSED": "已关闭",
    "DEFERRED": "已延期",
    "REJECTED": "已拒绝",
}

SEVERITY_LABELS = {
    "S1": "致命",
    "S2": "严重",
    "S3": "一般",
    "S4": "轻微",
}

PRIORITY_LABELS = {
    "P0": "紧急",
    "P1": "高",
    "P2": "中",
    "P3": "低",
}

ACTION_DEFS = {
    "create": {"label": "提交缺陷", "to": "NEW"},
    "triage": {"label": "确认并指派", "to": "OPEN"},
    "start": {"label": "开始处理", "to": "IN_PROGRESS"},
    "fix": {"label": "提交修复", "to": "FIXED"},
    "verify_pass": {"label": "验证通过并关闭", "to": "CLOSED"},
    "verify_fail": {"label": "验证不通过，重新打开", "to": "OPEN"},
    "defer": {"label": "延期处理", "to": "DEFERRED"},
    "activate": {"label": "恢复处理", "to": "OPEN"},
    "reject": {"label": "驳回", "to": "REJECTED"},
    "reopen": {"label": "重开缺陷", "to": "OPEN"},
}


def action_rule(action, roles, *, to_state=None, comment_required=False,
                fields=(), assigned_only=False):
    """声明一条状态流转规则。"""
    return {
        "action": action,
        "to": to_state or ACTION_DEFS[action]["to"],
        "roles": set(roles),
        "commentRequired": comment_required,
        "fields": list(fields),
        "assignedOnly": assigned_only,
    }


TRANSITIONS = {
    "NEW": [
        action_rule("triage", ["product"], fields=["priority", "assignee_id"]),
        action_rule("defer", ["product"]),
        action_rule("reject", ["product"], comment_required=True),
    ],
    "OPEN": [
        action_rule("start", ["developer"], assigned_only=True),
        action_rule("defer", ["product"]),
        action_rule("reject", ["product"], comment_required=True),
    ],
    "IN_PROGRESS": [
        action_rule("fix", ["developer"], assigned_only=True),
        action_rule("defer", ["product"]),
        action_rule("reject", ["product"], comment_required=True),
    ],
    "FIXED": [
        action_rule("verify_pass", ["tester"]),
        action_rule("verify_fail", ["tester"], comment_required=True),
    ],
    "DEFERRED": [
        action_rule("activate", ["product"]),
        action_rule("reject", ["product"], comment_required=True),
    ],
    "CLOSED": [
        action_rule("reopen", ["tester", "product"], comment_required=True),
    ],
    "REJECTED": [],
}

MAIN_FLOW_ORDER = ["NEW", "OPEN", "IN_PROGRESS", "FIXED", "CLOSED"]


# --------------------------------------------------------------------------- #
# 演示数据（无登录系统，前端通过选择身份模拟不同角色）
# --------------------------------------------------------------------------- #

SEED_USERS = [
    {"username": "qa_lin", "name": "林小测", "role": "tester", "title": "测试工程师"},
    {"username": "qa_zhou", "name": "周小测", "role": "tester", "title": "测试工程师"},
    {"username": "pm_wang", "name": "王产品", "role": "product", "title": "产品经理"},
    {"username": "dev_zhang", "name": "张开发", "role": "developer", "title": "前端开发"},
    {"username": "dev_chen", "name": "陈开发", "role": "developer", "title": "后端开发"},
]


def now(offset_minutes: int = 0) -> str:
    return (datetime.now() + timedelta(minutes=offset_minutes)).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    module TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL,
    priority TEXT,
    reporter_id INTEGER NOT NULL,
    assignee_id INTEGER,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (reporter_id) REFERENCES users(id),
    FOREIGN KEY (assignee_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id INTEGER NOT NULL,
    actor_id INTEGER,
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (bug_id) REFERENCES bugs(id),
    FOREIGN KEY (actor_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_bugs_state ON bugs(state);
CREATE INDEX IF NOT EXISTS idx_events_bug ON events(bug_id);
"""


# --------------------------------------------------------------------------- #
# 数据库访问
# --------------------------------------------------------------------------- #

DB_LOCK = threading.RLock()


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


CONN = get_connection()


def init_db(force: bool = False) -> None:
    global CONN
    with DB_LOCK:
        if force and DB_PATH.exists():
            CONN.close()
            DB_PATH.unlink()
            CONN = get_connection()
        CONN.executescript(SCHEMA)
        if CONN.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            seed_demo()
        CONN.commit()


def seed_demo() -> None:
    """写入演示账号与覆盖各状态的缺陷，便于第一次打开就能体验完整流程。"""
    cur = CONN.execute("PRAGMA user_version")  # noqa: F841
    user_ids = {}
    for u in SEED_USERS:
        c = CONN.execute(
            "INSERT INTO users (username, name, role, title) VALUES (?,?,?,?)",
            (u["username"], u["name"], u["role"], u["title"]),
        )
        user_ids[u["username"]] = c.lastrowid

    def insert_bug(title, description, module, severity, reporter, assignee,
                   state, priority=None):
        c = CONN.execute(
            """INSERT INTO bugs (code, title, description, module, severity,
                                 priority, reporter_id, assignee_id, state,
                                 created_at, updated_at)
               VALUES ('', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, description, module, severity, priority,
             user_ids[reporter], user_ids.get(assignee), state,
             now(-60), now(-60)),
        )
        bug_id = c.lastrowid
        code = f"BUG-{bug_id:04d}"
        CONN.execute("UPDATE bugs SET code=? WHERE id=?", (code, bug_id))
        return bug_id

    def add_event(bug_id, actor, action, from_state, to_state, comment,
                  offset=-60):
        CONN.execute(
            """INSERT INTO events (bug_id, actor_id, action, from_state,
                                   to_state, comment, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (bug_id, user_ids[actor], action, from_state, to_state, comment,
             now(offset)),
        )

    # 1. 待验证：一个已经走完开发、等测试确认的缺陷
    b1 = insert_bug(
        "登录页在 Safari 下输入框发生重叠",
        "使用 Safari 16+ 打开登录页，用户名输入框与密码框在缩放到 125% 时重叠。",
        "Web 登录", "S2", "qa_lin", "dev_zhang", "FIXED", "P1",
    )
    add_event(b1, "qa_lin", "create", "", "NEW", "Safari 125% 缩放下复现", -300)
    add_event(b1, "pm_wang", "triage", "NEW", "OPEN", "优先处理，指派前端", -280)
    add_event(b1, "dev_zhang", "start", "OPEN", "IN_PROGRESS", "", -200)
    add_event(b1, "dev_zhang", "fix", "IN_PROGRESS", "FIXED", "改用 flex 布局并补充媒体查询", -15)

    # 2. 处理中
    b2 = insert_bug(
        "订单导出超过 5 万行时接口超时",
        "导出 5 万行以上订单时请求超过 60s 被网关断开，前端提示系统繁忙。",
        "订单中心", "S1", "qa_lin", "dev_chen", "IN_PROGRESS", "P1",
    )
    add_event(b2, "qa_lin", "create", "", "NEW", "用 6 万行生产数据可复现", -220)
    add_event(b2, "pm_wang", "triage", "NEW", "OPEN", "影响大客户，P1", -200)
    add_event(b2, "dev_chen", "start", "OPEN", "IN_PROGRESS", "先改为分页流式导出", -90)

    # 3. 待确认（刚提交）
    b3 = insert_bug(
        "个人中心头像上传后页面未即时刷新",
        "上传新头像成功后列表仍显示旧头像，刷新页面后才更新。",
        "个人中心", "S3", "qa_zhou", None, "NEW",
    )
    add_event(b3, "qa_zhou", "create", "", "NEW", "", -40)

    # 4. 待处理（已确认待开发）
    b4 = insert_bug(
        "消息通知偶发重复推送",
        "高峰期部分用户收到同一条通知 2-3 次，时间集中在 10:00-10:30。",
        "消息中心", "S2", "qa_lin", "dev_zhang", "OPEN", "P2",
    )
    add_event(b4, "qa_lin", "create", "", "NEW", "", -180)
    add_event(b4, "pm_wang", "triage", "NEW", "OPEN", "先排查推送去重逻辑", -150)

    # 5. 已延期
    b5 = insert_bug(
        "支付回调失败后缺少补偿重试",
        "支付回调处理抛异常后没有补偿任务，订单状态可能永久停留为“支付中”。",
        "支付", "S1", "qa_lin", "dev_chen", "DEFERRED", "P2",
    )
    add_event(b5, "qa_lin", "create", "", "NEW", "", -600)
    add_event(b5, "pm_wang", "triage", "NEW", "OPEN", "需要评估改造范围", -570)
    add_event(b5, "pm_wang", "defer", "OPEN", "DEFERRED", "与下个结算季一起排期", -400)

    # 6. 已拒绝
    b6 = insert_bug(
        "IE11 下按钮点击无反馈",
        "IE11 中提交按钮点击后没有 loading 反馈，可能造成重复提交。",
        "兼容性", "S4", "qa_zhou", None, "REJECTED",
    )
    add_event(b6, "qa_zhou", "create", "", "NEW", "", -1000)
    add_event(b6, "pm_wang", "reject", "NEW", "REJECTED", "已确认不再支持 IE11，非当前版本范围", -950)

    # 7. 已关闭（走完整闭环）
    b7 = insert_bug(
        "注册页验证码在深色模式下看不清",
        "深色主题下验证码背景与文字对比度过低，识别困难。",
        "注册登录", "S3", "qa_lin", "dev_zhang", "CLOSED", "P2",
    )
    add_event(b7, "qa_lin", "create", "", "NEW", "", -700)
    add_event(b7, "pm_wang", "triage", "NEW", "OPEN", "视觉问题，P2", -670)
    add_event(b7, "dev_zhang", "start", "OPEN", "IN_PROGRESS", "", -600)
    add_event(b7, "dev_zhang", "fix", "IN_PROGRESS", "FIXED", "提高对比度并增加描边", -500)
    add_event(b7, "qa_lin", "verify_pass", "FIXED", "CLOSED", "验证通过", -460)


def query_user(user_id: int) -> dict | None:
    with DB_LOCK:
        row = CONN.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def query_bug_row(bug_id: int) -> dict | None:
    with DB_LOCK:
        row = CONN.execute("SELECT * FROM bugs WHERE id=?", (bug_id,)).fetchone()
        return dict(row) if row else None


def allowed_actions(user: dict, bug: dict) -> list[dict]:
    """返回某个身份在当前状态下可执行的动作（服务端为唯一事实来源）。"""
    result = []
    for rule in TRANSITIONS[bug["state"]]:
        if user["role"] not in rule["roles"]:
            continue
        if rule["assignedOnly"] and bug["assignee_id"] != user["id"]:
            continue
        item = {
            "action": rule["action"],
            "label": ACTION_DEFS[rule["action"]]["label"],
            "to": rule["to"],
            "commentRequired": rule["commentRequired"],
            "fields": rule["fields"],
        }
        result.append(item)
    return result


def user_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "role": row["role"],
        "roleLabel": ROLE_LABELS[row["role"]],
        "title": row["title"],
    }


def bug_public(row: dict) -> dict:
    reporter = query_user(row["reporter_id"])
    assignee = query_user(row["assignee_id"]) if row["assignee_id"] else None
    data = dict(row)
    data.update(
        stateLabel=STATE_LABELS[row["state"]],
        severityLabel=SEVERITY_LABELS[row["severity"]],
        priorityLabel=PRIORITY_LABELS.get(row["priority"]) if row["priority"] else None,
        reporter=user_public(reporter) if reporter else None,
        assignee=user_public(assignee) if assignee else None,
    )
    return data


def event_public(row: dict) -> dict:
    actor = query_user(row["actor_id"]) if row["actor_id"] else None
    data = dict(row)
    data.update(
        actionLabel=ACTION_DEFS.get(row["action"], {}).get("label", row["action"]),
        fromStateLabel=STATE_LABELS.get(row["from_state"] or ""),
        toStateLabel=STATE_LABELS.get(row["to_state"]),
        actor=user_public(actor) if actor else None,
    )
    return data


def list_bugs() -> list[dict]:
    with DB_LOCK:
        rows = CONN.execute(
            "SELECT * FROM bugs ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        result = []
        for row in rows:
            item = bug_public(dict(row))
            item["eventCount"] = CONN.execute(
                "SELECT COUNT(*) FROM events WHERE bug_id=?", (row["id"],)
            ).fetchone()[0]
            result.append(item)
        return result


def detail(bug_id: int, as_user: int | None = None) -> dict | None:
    with DB_LOCK:
        row = query_bug_row(bug_id)
        if not row:
            return None
        events = CONN.execute(
            "SELECT * FROM events WHERE bug_id=? ORDER BY id", (bug_id,)
        ).fetchall()
        payload = bug_public(row)
        payload["timeline"] = [event_public(dict(e)) for e in events]
        user = query_user(as_user) if as_user else None
        payload["allowedActions"] = allowed_actions(user, row) if user else []
        return payload


def create_bug(payload: dict) -> dict:
    user = query_user(payload.get("user_id"))
    if not user:
        raise ValueError("无效的用户")
    if user["role"] != "tester":
        raise PermissionError("只有测试人员可以提交缺陷")
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("标题不能为空")
    if len(title) > 120:
        raise ValueError("标题不能超过 120 个字符")
    severity = str(payload.get("severity", ""))
    if severity not in SEVERITY_LABELS:
        raise ValueError("无效的严重程度")
    assignee_id = payload.get("assignee_id")
    if assignee_id:
        assignee = query_user(int(assignee_id))
        if not assignee or assignee["role"] != "developer":
            raise ValueError("指派人必须是开发人员")

    with DB_LOCK:
        ts = now()
        cur = CONN.execute(
            """INSERT INTO bugs (code, title, description, module, severity,
                                 reporter_id, assignee_id, state,
                                 created_at, updated_at)
               VALUES ('', ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)""",
            (
                title,
                str(payload.get("description", "")).strip(),
                str(payload.get("module", "")).strip(),
                severity,
                user["id"],
                int(assignee_id) if assignee_id else None,
                ts,
                ts,
            ),
        )
        bug_id = cur.lastrowid
        code = f"BUG-{bug_id:04d}"
        CONN.execute("UPDATE bugs SET code=? WHERE id=?", (code, bug_id))
        CONN.execute(
            """INSERT INTO events (bug_id, actor_id, action, from_state,
                                   to_state, comment, created_at)
               VALUES (?,?,?, '', 'NEW', ?, ?)""",
            (bug_id, user["id"], "create", str(payload.get("description", "")).strip(), ts),
        )
        CONN.commit()
        return detail(bug_id, user["id"])


def apply_action(bug_id: int, payload: dict) -> dict:
    """执行一次状态流转，服务端校验角色、状态和必要字段。"""
    user = query_user(payload.get("user_id"))
    if not user:
        raise ValueError("无效的用户")
    action = str(payload.get("action", ""))
    comment = str(payload.get("comment", "")).strip()

    with DB_LOCK:
        bug = query_bug_row(bug_id)
        if not bug:
            raise KeyError("缺陷不存在")

        rules = TRANSITIONS[bug["state"]]
        rule = next((r for r in rules if r["action"] == action), None)
        if not rule:
            raise PermissionError("当前状态不允许执行该操作")
        if user["role"] not in rule["roles"]:
            raise PermissionError("当前身份没有权限执行该操作")
        if rule["assignedOnly"] and bug["assignee_id"] != user["id"]:
            raise PermissionError("该缺陷已指派给其他开发人员")
        if rule["commentRequired"] and not comment:
            raise ValueError("该操作需要填写处理说明")

        if action == "triage":
            priority = str(payload.get("priority", ""))
            if priority not in PRIORITY_LABELS:
                raise ValueError("请选择优先级")
            assignee_id = payload.get("assignee_id")
            try:
                assignee_id = int(assignee_id) if assignee_id else 0
            except (TypeError, ValueError):
                assignee_id = 0
            assignee = query_user(assignee_id) if assignee_id else None
            if not assignee or assignee["role"] != "developer":
                raise ValueError("请指派一位开发人员")
            CONN.execute(
                "UPDATE bugs SET priority=?, assignee_id=? WHERE id=?",
                (priority, assignee_id, bug_id),
            )

        to_state = rule["to"]
        ts = now()
        CONN.execute(
            """INSERT INTO events (bug_id, actor_id, action, from_state,
                                   to_state, comment, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (bug_id, user["id"], action, bug["state"], to_state, comment, ts),
        )
        CONN.execute(
            "UPDATE bugs SET state=?, updated_at=? WHERE id=?",
            (to_state, ts, bug_id),
        )
        CONN.commit()
        return detail(bug_id, user["id"])


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DefectFlow/1.0"

    def log_message(self, fmt, *args):  # 控制台保持简洁
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- helpers ----------
    def send_json(self, status: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法的 JSON")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def serve_static(self, rel_path: str) -> None:
        if rel_path in ("", "/"):
            rel_path = "index.html"
        target = (STATIC_DIR / rel_path).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- routing ----------
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path
        query = parse_qs(parts.query)
        try:
            if path == "/api/users":
                with DB_LOCK:
                    users = [user_public(dict(r)) for r in CONN.execute("SELECT * FROM users ORDER BY id")]
                self.send_json(200, users)
            elif path == "/api/bugs":
                bugs = list_bugs()
                self.send_json(200, {"items": bugs})
            elif path.startswith("/api/bugs/") and "/actions" not in path:
                bug_id = int(path.rsplit("/", 1)[1])
                as_user = int(query.get("as", ["0"])[0]) if query.get("as") else None
                data = detail(bug_id, as_user)
                if data is None:
                    self.send_error_json(404, "缺陷不存在")
                else:
                    self.send_json(200, data)
            elif path.startswith("/api/"):
                self.send_error_json(404, "接口不存在")
            else:
                self.serve_static(path.lstrip("/"))
        except (ValueError, TypeError):
            self.send_error_json(400, "请求参数不合法")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/bugs":
                data = create_bug(payload)
                self.send_json(201, data)
            elif path.startswith("/api/bugs/") and path.endswith("/actions"):
                bug_id = int(path.split("/")[3])
                data = apply_action(bug_id, payload)
                self.send_json(200, data)
            else:
                self.send_error_json(404, "接口不存在")
        except KeyError:
            self.send_error_json(404, "缺陷不存在")
        except PermissionError as exc:
            self.send_error_json(403, str(exc))
        except ValueError as exc:
            self.send_error_json(400, str(exc))


def main() -> None:
    if RESET_DB:
        init_db(force=True)
    else:
        init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"DefectFlow running at http://{HOST}:{PORT}  (数据库: {DB_PATH})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
