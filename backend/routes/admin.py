"""
Admin API routes - agents, rooms, threads, workflows, skills, models, messages.
"""
import os
import json
import asyncio
import re
import threading
import time
from datetime import datetime
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from workspaces import get_room_workspace_info
from paths import APP_ROOT, PROFILES_ROOT
from routes.auth import current_tenant_id, get_current_account, is_admin_account, is_authenticated

router = APIRouter()

MENTION_RE = r"@([^\s@,，.。;；:：!！?？]+)"
RESERVED_AGENT_IDS = {"system", "user", "__system__", "__all__"}

# TRAE experience account message limit
TRAE_MSG_LIMIT_PER_USER = 2
TRAE_MSG_TRACKER_KEY = "traetest_msg_tracker"
TRAE_DEMO_GITHUB_URL = "https://github.com/uskyu/Myna"
TRAE_MSG_LIMIT_TIP = (
    "为了控制公共 Demo 的 token 消耗，体验账号每天仅开放 10 个体验名额，"
    "每位体验用户只能体验 1-2 个问题/任务。深度体验欢迎点击 GitHub 链接自行部署："
    f"{TRAE_DEMO_GITHUB_URL} ，有问题可联系作者一起调整。"
)
_msg_lock = threading.Lock()


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "0.0.0.0"


def _check_traetest_message_limit(db, ip: str) -> tuple[bool, str]:
    with _msg_lock:
        raw = db.get_hub_setting(TRAE_MSG_TRACKER_KEY) or "{}"
        try:
            tracker = json.loads(raw)
        except Exception:
            tracker = {}
        today = time.strftime("%Y-%m-%d")
        if tracker.get("date") != today or not isinstance(tracker.get("ip_counts"), dict):
            tracker = {"date": today, "ip_counts": {}}
        cur = tracker["ip_counts"].get(ip, 0)
        if cur >= TRAE_MSG_LIMIT_PER_USER:
            return False, TRAE_MSG_LIMIT_TIP
        tracker["ip_counts"][ip] = cur + 1
        db.set_hub_setting(TRAE_MSG_TRACKER_KEY, json.dumps(tracker))
        return True, ""


SKILL_CREATE_RE = re.compile(
    r"^(?:请|麻烦|帮我)?\s*(?:(?:给|为|帮)\s*@?[^\s@,，。；;:：!！?？]{2,32}\s*)?"
    r"(?:创建|新建|保存|学习|记录|加入|添加)\s*(?:一个|一条|本次|这个|此)?\s*(?:skill|技能)\s*(?=[：:《\"“'])",
    re.I,
)
SKILL_TITLE_RE = re.compile(r"(?:skill|技能)[：:《\"“']+\s*([^\n,，。；;:：》\"”']{2,48})", re.I)
SKILL_TARGET_RE = re.compile(r"(?:给|为|帮)\s*@?([^\s@,，。；;:：!！?？]{2,32})\s*(?:创建|新建|保存|学习|记录|添加|加入)")


def is_real_agent(agent: dict | None) -> bool:
    return bool(agent and agent.get("id") not in RESERVED_AGENT_IDS and agent.get("name"))


def _clean_skill_name(value: str) -> str:
    name = re.sub(r"^(?:一个|一条|本次|这个|此)?\s*(?:skill|技能)\s*", "", value or "", flags=re.I).strip()
    name = re.split(r"(?:\s+|[，,。\n；;])(?:内容|步骤|规则|流程|要求)\s*[：:]?", name, maxsplit=1)[0]
    name = re.split(r"[。\n；;]", name, maxsplit=1)[0].strip(" ：:《》\"'“”`*#")
    return name[:48]


def _strip_message_source_prefix(text: str) -> str:
    """Remove UI/chat sender prefixes before command parsing."""
    return re.sub(r"^\s*\[[^\]]{1,32}\]\s*[：:]\s*", "", text or "").strip()


def _looks_like_skill_meta_discussion(text: str) -> bool:
    """Guard against discussions/corrections that merely mention skill creation."""
    normalized = _strip_message_source_prefix(text)
    lowered = normalized.lower()
    negative_markers = (
        "我再说一遍", "不是让你", "不是叫你", "不要", "别", "禁止", "不能",
        "别自己", "不要自己", "不要创建", "别创建",
    )
    routing_markers = ("调用 open code", "调用 opencode", "open code", "opencode")
    meta_markers = ("优化创建 skill 的策略", "优化创建技能的策略", "创建 skill 的策略", "创建技能的策略")
    if any(marker in normalized for marker in negative_markers):
        return True
    if any(marker in lowered for marker in routing_markers):
        return True
    if any(marker in normalized for marker in meta_markers):
        return True
    return False


def _extract_skill_request(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    normalized = _strip_message_source_prefix(raw)
    if not normalized or _looks_like_skill_meta_discussion(normalized):
        return None

    # Natural-language skill creation is intentionally conservative: it must be
    # a command at the beginning of the message, include an explicit title
    # delimiter (技能：标题 / skill《标题》), and include explicit body content.
    # This prevents ordinary discussion such as “创建 skill 的策略” from causing
    # persistent skill writes.
    if not SKILL_CREATE_RE.search(normalized):
        return None

    explicit_title = SKILL_TITLE_RE.search(normalized)
    if not explicit_title:
        return None
    name = _clean_skill_name(explicit_title.group(1))
    if not name or len(name) < 2:
        return None

    body_match = re.search(r"(?:^|[\s，,。；;\n])(?:内容|步骤|规则|流程|要求)\s*[：:]\s*(.+)", normalized, re.S)
    if not body_match or not body_match.group(1).strip():
        return None
    content = body_match.group(1).strip()
    if len(content) < 10:
        return None

    description = f"由群聊自然语言请求创建：{normalized.splitlines()[0][:80]}"
    target_name = ""
    target_match = SKILL_TARGET_RE.search(normalized)
    if target_match:
        target_name = target_match.group(1).strip(" ：:《》\"'“”`*#")
    return {"name": name, "description": description, "content": content, "target_name": target_name}


def _resolve_skill_targets(db, room_id: str, mentions: list | None, target_name: str = "") -> list:
    members = [m for m in db.get_room_members(room_id) if is_real_agent(m)]
    if target_name:
        matched = [m for m in members if m.get("name") == target_name]
        if matched:
            return matched
    if mentions:
        mentioned = [m for m in members if m.get("id") in mentions]
        if mentioned:
            return mentioned
    return members


def _handle_natural_language_skill_create(db, room_id: str, text: str, mentions: list | None = None, thread_id: str | None = None) -> dict | None:
    request = _extract_skill_request(text)
    if not request:
        return None
    targets = _resolve_skill_targets(db, room_id, mentions, request.get("target_name", ""))
    if not targets:
        return {"ok": False, "error": "当前群聊没有可装载技能的真实智能体"}
    created = []
    for agent in targets:
        existing = db.get_agent_skills(agent["id"])
        same = next((s for s in existing if (s.get("name") or "").strip() == request["name"]), None)
        if same:
            skill = db.update_skill(same["id"], {
                "description": request["description"],
                "content": request["content"],
                "file_type": "text",
            })
        else:
            skill = db.create_skill(agent["id"], request["name"], request["description"], request["content"], "text")
        db.add_room_skill(room_id, skill["id"])
        created.append({"agent_id": agent["id"], "agent_name": agent["name"], "skill_id": skill["id"], "skill_name": skill["name"]})
    names = "、".join([f"@{item['agent_name']}" for item in created])
    status_text = f"✅ 已创建/更新技能「{request['name']}」，并装载到 {names}。"
    system_message = db.create_message(room_id, "system", status_text, "markdown", None, [], thread_id=thread_id)
    return {"ok": True, "message": system_message, "created": created}




def default_single_agent_mentions(db, room_id: str, mentions: list | None) -> list:
    mentions = list(mentions or [])
    if mentions:
        return mentions
    real_members = [m for m in db.get_room_members(room_id) if is_real_agent(m)]
    return [real_members[0]["id"]] if len(real_members) == 1 else mentions

def resolve_real_mentions(db, text: str, mentions: list | None = None) -> list:
    agents = {a["id"]: a for a in db.list_agents() if is_real_agent(a)}
    resolved = []
    for agent_id in mentions or []:
        if agent_id in agents and agent_id not in resolved:
            resolved.append(agent_id)
    if resolved:
        return resolved
    for match in re.finditer(MENTION_RE, text):
        mention_name = match.group(1).strip().strip("*_`~|")
        mentioned = next((a for a in agents.values() if a["name"] == mention_name), None)
        if mentioned and mentioned["id"] not in resolved:
            resolved.append(mentioned["id"])
    return resolved


def is_low_value_collaboration_ack(text: str) -> bool:
    """Detect status-only coordination messages that should not re-trigger agents."""
    normalized = re.sub(r"^\[[^\]]+\]:\s*", "", text or "").strip()
    if not normalized:
        return False
    status_markers = ("用户确认", "用户授权", "授权后", "等待用户", "当前状态保持不变", "状态同步", "基础自测后", "最终验收")
    pause_markers = ("等待", "授权", "确认", "暂停", "不再继续", "不再重复", "后续", "收到", "再 @")
    if any(marker in normalized for marker in status_markers) and any(marker in normalized for marker in pause_markers):
        return True
    return bool(re.search(r"(?:收到|确认)[，。,.\s]*(?:当前|测试侧|状态)|完成.*基础自测后.*再\s*@", normalized))


def get_db(request: Request):
    return request.app.state.db


def get_ws(request: Request):
    return request.app.state.ws_manager



async def _interrupt_matching_streams(ws_manager, room_id: str, mentions: list, thread_id: str | None = None):
    """Interrupt active streams before a new user turn is persisted."""
    for stream_id, stream_info in list(ws_manager.active_streams.items()):
        if stream_info.get("room_id") != room_id:
            continue
        if (stream_info.get("thread_id") or None) != (thread_id or None):
            continue
        agent_id = stream_info.get("agent_id")
        if agent_id in mentions or not mentions:
            await ws_manager.interrupt_stream(stream_id)


def tenant_id_for_request(request: Request) -> str | None:
    return current_tenant_id(request)


def require_admin_account(request: Request):
    if not is_admin_account(request):
        return JSONResponse({"ok": False, "error": "子账号不能修改系统配置"}, status_code=403)
    return None


def ensure_tenant_room_access(db, room_id: str, tenant_id: str | None) -> bool:
    if tenant_id is None:
        return True
    db.ensure_tenant_columns()
    room = db.get_room(room_id)
    return bool(room and room.get("tenant_id") == tenant_id)


def ensure_tenant_agent_access(db, agent_id: str, tenant_id: str | None) -> bool:
    if tenant_id is None:
        return True
    db.ensure_tenant_columns()
    agent = db.get_agent_by_id(agent_id)
    return bool(agent and agent.get("tenant_id") == tenant_id)


def ensure_tenant_thread_access(db, thread_id: str, tenant_id: str | None) -> dict | None:
    thread = db.get_thread(thread_id)
    if not thread:
        return None
    if not ensure_tenant_room_access(db, thread.get("room_id"), tenant_id):
        return None
    return thread


def ensure_tenant_workflow_access(db, workflow_id: str, tenant_id: str | None) -> dict | None:
    workflow = db.get_workflow(workflow_id)
    if not workflow:
        return None
    if not ensure_tenant_room_access(db, workflow.get("room_id"), tenant_id):
        return None
    return workflow


def ensure_tenant_skill_access(db, skill_id: str, tenant_id: str | None) -> dict | None:
    skill = db.get_skill_by_id(skill_id)
    if not skill:
        return None
    if not ensure_tenant_agent_access(db, skill.get("agent_id"), tenant_id):
        return None
    return skill


def filter_tenant_members(db, members: list, tenant_id: str | None) -> list:
    if tenant_id is None:
        return members
    return [m for m in members if m.get("tenant_id") == tenant_id]

# === Auth middleware (via dependency) ===
async def check_auth(request: Request):
    secret = os.environ.get("SECRET_KEY", "")
    if not secret or secret == "change-me-to-a-random-string":
        return True
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {secret}":
        return False
    return True


# === Agents ===

@router.get("/agents")
async def list_agents(request: Request):
    db = get_db(request)
    agents = db.list_agents(tenant_id_for_request(request))
    return {"ok": True, "result": agents}


@router.post("/agents")
async def create_agent(request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    body = await request.json()
    name = body.get("name")
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    model_config_id = body.get("model_config_id") or None
    if model_config_id and not db.get_model_config(model_config_id):
        return JSONResponse({"ok": False, "error": "model_config_id is invalid"}, status_code=400)
    agent = db.create_agent(name, body.get("description", ""), model_config_id, tenant_id)
    return {"ok": True, "result": agent}


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_agent_access(db, agent_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    body = await request.json()
    if tenant_id is not None:
        for forbidden_key in ["status", "model_config_id", "execution_mode", "self_improve", "self_improve_threshold", "tools_config", "container_id", "sort_order"]:
            body.pop(forbidden_key, None)
    name = body.get("name")
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    fields = {"name": name, "description": body.get("description", "")}
    for key in ["status", "model_config_id", "execution_mode", "self_improve",
                "self_improve_threshold", "tools_config"]:
        if key in body:
            val = body[key]
            if key == "self_improve":
                val = 1 if val else 0
            if key == "tools_config" and not isinstance(val, str):
                val = json.dumps(val)
            fields[key] = val
    if "container_id" in body:
        fields["container_id"] = str(body.get("container_id") or "").strip() or None
    elif "sort_order" in body:
        fields["container_id"] = str(body["sort_order"])
    db.update_agent(agent_id, fields)
    return {"ok": True}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request):
    # Protect system agents from deletion
    if agent_id in ('__system__', 'user', 'system'):
        return JSONResponse({"ok": False, "error": "系统智能体不可删除"}, status_code=403)
    db = get_db(request)
    if not ensure_tenant_agent_access(db, agent_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    db.delete_agent(agent_id)
    return {"ok": True}


# === Rooms ===

@router.get("/rooms")
async def list_rooms(request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    room_type = request.query_params.get("type")
    rooms = db.list_rooms(tenant_id, room_type)
    if not rooms:
        return {"ok": True, "result": []}

    room_ids = [r["id"] for r in rooms]
    members_by_room = db.get_room_members_for_rooms(room_ids)
    last_messages_by_room = db.get_last_messages_for_rooms(room_ids)
    result = []
    for r in rooms:
        room_settings = {}
        try:
            room_settings = json.loads(r.get("settings_json") or "{}")
        except Exception:
            room_settings = {}
        workspace_info = {}
        try:
            workspace_info = get_room_workspace_info(r["id"], room_settings)
        except Exception as e:
            workspace_info = {"workspace_error": str(e)}
        result.append({
            **r,
            "members": members_by_room.get(r["id"], []),
            "last_message": last_messages_by_room.get(r["id"]),
            **workspace_info,
        })
    return {"ok": True, "result": result}


@router.post("/rooms")
async def create_room(request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    body = await request.json()
    name = body.get("name")
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    room = db.create_room(name, body.get("description", ""), body.get("type", "group"), tenant_id)
    return {"ok": True, "result": room}


@router.put("/rooms/{room_id}")
async def update_room(room_id: str, request: Request):
    db = get_db(request)
    body = await request.json()
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    room = db.get_room(room_id)
    if not room:
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    if "settings_json" in body:
        settings = body["settings_json"]
        if isinstance(settings, str):
            settings = json.loads(settings)
        workspace_path = str(settings.get("workspace_path") or "").strip()
        if workspace_path:
            expanded = os.path.expanduser(workspace_path)
            if not os.path.isabs(expanded):
                return JSONResponse({"ok": False, "error": "workspace_path must be an absolute path"}, status_code=400)
            settings["workspace_path"] = expanded
        else:
            settings["workspace_path"] = ""
        db.update_room_settings(room_id, settings)
    sets = []
    vals = []
    if "name" in body:
        sets.append(f"name = {db._placeholder()}"); vals.append(body["name"])
    if "description" in body:
        sets.append(f"description = {db._placeholder()}"); vals.append(body["description"])
    if sets:
        vals.append(room_id)
        db.execute(f"UPDATE rooms SET {', '.join(sets)} WHERE id = {db._placeholder()}", vals)
        db.commit()
    return {"ok": True}


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    db.delete_room(room_id)
    return {"ok": True}


# Room members
@router.post("/rooms/{room_id}/members")
async def add_members(room_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    body = await request.json()
    agent_ids = body.get("agent_ids") or ([body["agent_id"]] if body.get("agent_id") else [])
    if not agent_ids:
        return JSONResponse({"ok": False, "error": "agent_id or agent_ids required"}, status_code=400)
    for aid in agent_ids:
        agent = db.get_agent_by_id(aid)
        if tenant_id is not None and (not agent or agent.get("tenant_id") != tenant_id):
            return JSONResponse({"ok": False, "error": f"Invalid agent: {aid}"}, status_code=400)
        if not is_real_agent(agent):
            return JSONResponse({"ok": False, "error": f"Invalid agent: {aid}"}, status_code=400)
        db.add_member(room_id, aid, body.get("role", "member"))
    return {"ok": True, "added": len(agent_ids)}


@router.delete("/rooms/{room_id}/members/{agent_id}")
async def remove_member(room_id: str, agent_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id) or not ensure_tenant_agent_access(db, agent_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room or agent not found"}, status_code=404)
    db.remove_member(room_id, agent_id)
    return {"ok": True}


# Room messages
@router.get("/rooms/{room_id}/messages")
async def get_room_messages(room_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    limit = int(request.query_params.get("limit", "50"))
    before_id = request.query_params.get("before_id")
    q = request.query_params.get("q")
    order = request.query_params.get("order", "desc")
    if q:
        messages = db.get_room_history(
            room_id,
            limit,
            int(before_id) if before_id else None,
            order=order,
            q=q,
        )
    else:
        messages = db.get_room_messages(room_id, limit, int(before_id) if before_id else None)
    return {"ok": True, "result": messages}


@router.delete("/rooms/{room_id}/messages")
async def clear_room_messages(room_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    db.clear_room_messages(room_id)
    return {"ok": True}


# Message edit/delete
@router.patch("/messages/{message_id}")
async def update_message(message_id: int, request: Request):
    db = get_db(request)
    message = db.get_message_by_id(message_id)
    if not message:
        return JSONResponse({"ok": False, "error": "Message not found"}, status_code=404)
    if not ensure_tenant_room_access(db, message.get("room_id"), tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Message not found"}, status_code=404)
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)
    db.update_message(message_id, text)
    return {"ok": True}


@router.delete("/messages/{message_id}")
async def delete_message(message_id: int, request: Request):
    db = get_db(request)
    message = db.get_message_by_id(message_id)
    if not message:
        return JSONResponse({"ok": False, "error": "Message not found"}, status_code=404)
    if not ensure_tenant_room_access(db, message.get("room_id"), tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Message not found"}, status_code=404)
    db.delete_message(message_id)
    return {"ok": True}


# Send message (triggers AI)
@router.post("/rooms/{room_id}/send")
async def send_message(room_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    ws_manager = get_ws(request)
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)

    db.ensure_system_agents()

    # Parse mentions
    mentions = resolve_real_mentions(db, text, body.get("mentions") or [])

    # Handle /retry: delete last AI message and re-trigger
    if text.strip() == '/retry':
        recent = db.get_room_messages(room_id, 10)
        last_ai = None
        for m in reversed(recent):
            if m["sender_id"] not in ("user", "system"):
                last_ai = m
                break
        if last_ai:
            db.delete_message(last_ai["id"])
            await ws_manager.notify_ui({"type": "message_deleted", "room_id": room_id, "message_id": last_ai["id"]})
            # Re-trigger with the last user message
            last_user = None
            for m in reversed(recent):
                if m["sender_id"] == "user" and m["id"] != last_ai["id"]:
                    last_user = m
                    break
            if last_user:
                from ai_engine import process_message
                room = db.get_room(room_id)
                room_type = room["type"] if room else "group"
                target_agents = mentions if mentions else [last_ai["sender_id"]]
                async def _retry():
                    try:
                        await process_message(db, ws_manager, room_id, "user", last_user["text"], target_agents, room_type)
                    except Exception as e:
                        print(f"[AI] retry error: {e}")
                asyncio.create_task(_retry())
        return {"ok": True, "result": {"action": "retry"}}

    room = db.get_room(room_id)
    room_type = room["type"] if room else "group"
    if room_type == "group":
        mentions = default_single_agent_mentions(db, room_id, mentions)

    await _interrupt_matching_streams(ws_manager, room_id, mentions)

    message = db.create_message(room_id, "user", text, "markdown", None, mentions)

    skill_result = _handle_natural_language_skill_create(db, room_id, text, mentions)
    if skill_result:
        if skill_result.get("message"):
            await ws_manager.notify_ui({"type": "new_message", "room_id": room_id, "message": skill_result["message"]})
        if not skill_result.get("ok"):
            error_message = db.create_message(room_id, "system", f"⚠️ 技能创建失败：{skill_result.get('error', '未知错误')}")
            await ws_manager.notify_ui({"type": "new_message", "room_id": room_id, "message": error_message})
        return {"ok": True, "result": message, "skill_result": skill_result}

    if is_low_value_collaboration_ack(text):
        return {"ok": True, "result": message, "skipped_ai": "low_value_collaboration_ack"}

    # TRAE experience account message limit
    if tenant_id == "traetest":
        ip = _get_client_ip(request)
        allowed, limit_msg = _check_traetest_message_limit(db, ip)
        if not allowed:
            limit_sys_msg = db.create_message(room_id, "system", f"⚠️ {limit_msg}")
            await ws_manager.notify_ui({"type": "new_message", "room_id": room_id, "message": limit_sys_msg})
            return {"ok": True, "result": message, "limit_reached": True}

    # Trigger AI asynchronously
    from ai_engine import process_message
    async def _run_ai():
        try:
            await process_message(db, ws_manager, room_id, "user", text, mentions, room_type)
        except Exception as e:
            import traceback
            print(f"[AI] process_message error: {e}")
            traceback.print_exc()
    asyncio.create_task(_run_ai())

    return {"ok": True, "result": message}


# === DMs ===

@router.post("/dm/{agent_id}")
async def create_dm(agent_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_agent_access(db, agent_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    agent = db.get_agent_by_id(agent_id)
    if not agent:
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    db.ensure_system_agents()
    dm_room = db.get_dm_room("user", agent_id)
    if dm_room and tenant_id is not None and dm_room.get("tenant_id") != tenant_id:
        dm_room = None
    if not dm_room:
        dm_room = db.create_room(f"DM: {agent['name']}", "", "dm", tenant_id)
        db.add_member(dm_room["id"], "user", "member")
        db.add_member(dm_room["id"], agent_id, "member")
    return {"ok": True, "result": {"room_id": dm_room["id"], "agent": agent}}


@router.get("/dms")
async def list_dms(request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    return {"ok": True, "result": db.list_dm_rooms_with_details(tenant_id)}


# === Model Configs ===

@router.get("/models")
async def list_models(request: Request):
    db = get_db(request)
    configs = db.list_model_configs()
    safe = [{**c, "api_key": "***" + c["api_key"][-4:] if c.get("api_key") else ""} for c in configs]
    return {"ok": True, "result": safe}


@router.post("/models")
async def create_model(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    db = get_db(request)
    body = await request.json()
    for key in ["name", "provider", "base_url", "api_key", "model"]:
        if not body.get(key):
            return JSONResponse({"error": f"{key} required"}, status_code=400)
    config = db.create_model_config(body)
    return {"ok": True, "result": config}


@router.post("/models/batch-delete")
async def batch_delete_models(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    db = get_db(request)
    body = await request.json()
    model_ids = body.get("model_ids") or body.get("ids") or []
    if not isinstance(model_ids, list) or not model_ids:
        return JSONResponse({"ok": False, "error": "model_ids required"}, status_code=400)
    deleted = db.delete_model_configs_batch(model_ids)
    return {"ok": True, "deleted": deleted}


@router.put("/models/{model_id}")
async def update_model(model_id: str, request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    db = get_db(request)
    body = await request.json()
    db.update_model_config(model_id, body)
    return {"ok": True}


@router.delete("/models/{model_id}")
async def delete_model(model_id: str, request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    db = get_db(request)
    db.delete_model_config(model_id)
    return {"ok": True}


# Model metadata (litellm)
_model_metadata_cache = None
_model_metadata_time = 0


@router.post("/config/models")
async def fetch_provider_models(request: Request):
    """Fetch available models from a provider's /models endpoint."""
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    import httpx
    body = await request.json()
    base_url = body.get("base_url", "").rstrip("/")
    api_key_val = body.get("api_key", "")

    # If no api_key provided but model_config_id given, fetch from DB (admin only).
    if not api_key_val and body.get("model_config_id"):
        blocked = require_admin_account(request)
        if blocked:
            return blocked
        db = get_db(request)
        config = db.get_model_config(body["model_config_id"])
        if config:
            api_key_val = config.get("api_key", "")
            if not base_url:
                base_url = config.get("base_url", "").rstrip("/")

    if not base_url or not api_key_val:
        return JSONResponse({"ok": False, "error": "需要 base_url 和 api_key"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                base_url + "/models",
                headers={"Authorization": f"Bearer {api_key_val}"}
            )
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            data = resp.json()
            # OpenAI format: { data: [{id: "model-name", ...}, ...] }
            models_list = data.get("data", [])
            # Normalize to [{id: "..."}]
            result = [{"id": m["id"]} for m in models_list if m.get("id")]
            # Sort alphabetically
            result.sort(key=lambda x: x["id"])
            return {"ok": True, "result": result}
    except httpx.TimeoutException:
        return {"ok": False, "error": "连接超时（20秒）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/models/test")
async def test_model_connection(request: Request):
    """Test a model connection by sending a simple prompt."""
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    import httpx
    body = await request.json()
    base_url = body.get("base_url", "").rstrip("/")
    api_key_val = body.get("api_key", "")
    model = body.get("model", "gpt-4o-mini")
    api_mode = body.get("api_mode", "chat_completions")

    # If no api_key provided but model_config_id given, fetch from DB (admin only).
    if not api_key_val and body.get("model_config_id"):
        blocked = require_admin_account(request)
        if blocked:
            return blocked
        db = get_db(request)
        config = db.get_model_config(body["model_config_id"])
        if config:
            api_key_val = config["api_key"]
            if not base_url:
                base_url = config["base_url"].rstrip("/")

    if not base_url or not api_key_val:
        return JSONResponse({"ok": False, "error": "需要 base_url 和 api_key"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if api_mode == "anthropic_messages":
                # Anthropic Messages API format
                url = base_url + "/messages"
                resp = await client.post(url, json={
                    "model": model,
                    "max_tokens": 50,
                    "messages": [{"role": "user", "content": "Say hi in 5 words"}]
                }, headers={
                    "x-api-key": api_key_val,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                })
            elif api_mode == "responses":
                # OpenAI Responses API format
                url = base_url + "/responses"
                resp = await client.post(url, json={
                    "model": model,
                    "input": "Say hi in 5 words",
                }, headers={
                    "Authorization": f"Bearer {api_key_val}",
                    "Content-Type": "application/json",
                })
            else:
                # Chat Completions (default)
                url = base_url + "/chat/completions"
                resp = await client.post(url, json={
                    "model": model,
                    "max_tokens": 50,
                    "messages": [{"role": "user", "content": "Say hi in 5 words"}]
                }, headers={
                    "Authorization": f"Bearer {api_key_val}",
                    "Content-Type": "application/json",
                })

            if resp.status_code != 200:
                error_text = resp.text[:200]
                return {"ok": False, "error": f"HTTP {resp.status_code}: {error_text}"}

            data = resp.json()
            # Extract reply based on format
            reply = ""
            if api_mode == "anthropic_messages":
                reply = data.get("content", [{}])[0].get("text", "")
            elif api_mode == "responses":
                reply = data.get("output_text", "") or str(data.get("output", ""))[:100]
            else:
                reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            return {"ok": True, "result": {"reply": reply, "model": model, "api_mode": api_mode}}

    except httpx.TimeoutException:
        return {"ok": False, "error": "连接超时（30秒）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@router.get("/models/metadata")
async def get_model_metadata(request: Request):
    import time
    import httpx
    global _model_metadata_cache, _model_metadata_time

    if _model_metadata_cache and (time.time() - _model_metadata_time < 86400):
        pass
    else:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get("https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json")
                if resp.status_code == 200:
                    data = resp.json()
                    simplified = {}
                    for key, val in data.items():
                        if key == "sample_spec":
                            continue
                        simplified[key] = {
                            "max_input_tokens": val.get("max_input_tokens"),
                            "max_output_tokens": val.get("max_output_tokens"),
                            "provider": val.get("litellm_provider"),
                            "supports_vision": bool(val.get("supports_vision")),
                            "supports_function_calling": bool(val.get("supports_function_calling")),
                        }
                    _model_metadata_cache = simplified
                    _model_metadata_time = time.time()
        except Exception:
            pass

    metadata = _model_metadata_cache or {}
    q = request.query_params.get("q")
    id_param = request.query_params.get("id")

    if id_param:
        exact = metadata.get(id_param)
        if exact:
            return {"ok": True, "result": {id_param: exact}}
        matches = {k: v for k, v in metadata.items() if id_param in k}
        return {"ok": True, "result": dict(list(matches.items())[:20])}
    if q:
        query = q.lower()
        matches = {k: v for k, v in metadata.items() if query in k.lower()}
        return {"ok": True, "result": dict(list(matches.items())[:50])}
    return {"ok": True, "total": len(metadata)}


# === Threads ===

@router.get("/rooms/{room_id}/threads")
async def get_threads(room_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    threads = db.get_threads(room_id)
    return {"ok": True, "result": threads}


@router.post("/rooms/{room_id}/threads")
async def create_thread(room_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    body = await request.json()
    title = body.get("title")
    if not title:
        return JSONResponse({"ok": False, "error": "title is required"}, status_code=400)
    thread = db.create_thread(room_id, title)
    return {"ok": True, "result": thread}


@router.patch("/threads/{thread_id}")
@router.put("/threads/{thread_id}")
async def update_thread(thread_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_thread_access(db, thread_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Thread not found"}, status_code=404)
    body = await request.json()
    db.update_thread(thread_id, body)
    return {"ok": True}


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_thread_access(db, thread_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Thread not found"}, status_code=404)
    db.delete_thread(thread_id)
    return {"ok": True}


# === Approval ===

@router.post("/approvals/{approval_id}")
async def respond_approval(approval_id: str, request: Request):
    """User responds to an approval request (approve/deny)."""
    from ai_engine import resolve_approval
    body = await request.json()
    decision = body.get("decision", "deny")  # command: once/session/always/deny; skill: approve/deny
    if decision not in ("once", "session", "always", "approve", "deny"):
        decision = "deny"
    resolve_approval(approval_id, decision)
    return {"ok": True, "decision": decision}


@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(thread_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_thread_access(db, thread_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Thread not found"}, status_code=404)
    limit = int(request.query_params.get("limit", "30"))
    q = request.query_params.get("q")
    before_id = request.query_params.get("before_id")
    order = request.query_params.get("order", "desc")
    if q:
        thread = db.get_thread(thread_id)
        if not thread:
            return JSONResponse({"ok": False, "error": "Thread not found"}, status_code=404)
        messages = db.get_room_history(
            thread["room_id"],
            limit,
            int(before_id) if before_id else None,
            order=order,
            q=q,
            thread_id=thread_id,
        )
    else:
        messages = db.get_thread_messages(thread_id, limit)
    return {"ok": True, "result": messages}


@router.post("/threads/{thread_id}/send")
async def send_thread_message(thread_id: str, request: Request):
    db = get_db(request)
    ws_manager = get_ws(request)
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"ok": False, "error": "text is required"}, status_code=400)

    thread = ensure_tenant_thread_access(db, thread_id, tenant_id_for_request(request))
    if not thread:
        return JSONResponse({"ok": False, "error": "Thread not found"}, status_code=404)

    db.ensure_system_agents()

    # Parse mentions
    mentions = resolve_real_mentions(db, text, body.get("mentions") or [])

    # Handle /retry in thread
    if text.strip() == '/retry':
        recent = db.get_thread_messages(thread_id, 10)
        last_ai = None
        for m in reversed(recent):
            if m["sender_id"] not in ("user", "system"):
                last_ai = m
                break
        if last_ai:
            db.delete_message(last_ai["id"])
            await ws_manager.notify_ui({"type": "message_deleted", "room_id": thread["room_id"], "message_id": last_ai["id"], "thread_id": thread_id})
            last_user = None
            for m in reversed(recent):
                if m["sender_id"] == "user" and m["id"] != last_ai["id"]:
                    last_user = m
                    break
            if last_user:
                from ai_engine import process_message
                room = db.get_room(thread["room_id"])
                room_type = room["type"] if room else "group"
                target_agents = mentions if mentions else [last_ai["sender_id"]]
                async def _retry_thread():
                    try:
                        await process_message(db, ws_manager, thread["room_id"], "user", last_user["text"], target_agents, room_type, thread_id=thread_id)
                    except Exception as e:
                        print(f"[AI] retry error: {e}")
                asyncio.create_task(_retry_thread())
        return {"ok": True, "result": {"action": "retry"}}

    room = db.get_room(thread["room_id"])
    room_type = room["type"] if room else "group"
    if room_type == "group":
        mentions = default_single_agent_mentions(db, thread["room_id"], mentions)

    await _interrupt_matching_streams(ws_manager, thread["room_id"], mentions, thread_id)

    message = db.create_message(thread["room_id"], "user", text, "markdown", None, mentions, thread_id=thread_id)

    skill_result = _handle_natural_language_skill_create(db, thread["room_id"], text, mentions, thread_id=thread_id)
    if skill_result:
        if skill_result.get("message"):
            await ws_manager.notify_ui({"type": "new_message", "room_id": thread["room_id"], "message": skill_result["message"], "thread_id": thread_id})
        if not skill_result.get("ok"):
            error_message = db.create_message(thread["room_id"], "system", f"⚠️ 技能创建失败：{skill_result.get('error', '未知错误')}", thread_id=thread_id)
            await ws_manager.notify_ui({"type": "new_message", "room_id": thread["room_id"], "message": error_message, "thread_id": thread_id})
        return {"ok": True, "result": message, "skill_result": skill_result}

    # TRAE experience account message limit
    if tenant_id_for_request(request) == "traetest":
        ip = _get_client_ip(request)
        allowed, limit_msg = _check_traetest_message_limit(db, ip)
        if not allowed:
            limit_sys_msg = db.create_message(thread["room_id"], "system", f"⚠️ {limit_msg}", thread_id=thread_id)
            await ws_manager.notify_ui({"type": "new_message", "room_id": thread["room_id"], "message": limit_sys_msg, "thread_id": thread_id})
            return {"ok": True, "result": message, "limit_reached": True}

    # Trigger AI
    from ai_engine import process_message
    asyncio.create_task(process_message(db, ws_manager, thread["room_id"], "user", text, mentions, room_type, thread_id=thread_id))

    return {"ok": True, "result": message}


# === Workflows ===

@router.get("/rooms/{room_id}/workflows")
async def get_workflows(room_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    workflows = db.get_workflows(room_id)
    return {"ok": True, "result": workflows}


@router.post("/rooms/{room_id}/workflows")
async def create_workflow(room_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    body = await request.json()
    name = body.get("name")
    steps_json = body.get("steps_json")
    if not name or not steps_json:
        return JSONResponse({"ok": False, "error": "name and steps_json required"}, status_code=400)
    if not isinstance(steps_json, str):
        steps_json = json.dumps(steps_json)
    trigger_config = body.get("trigger_config", "{}")
    if not isinstance(trigger_config, str):
        trigger_config = json.dumps(trigger_config)
    workflow = db.create_workflow(room_id, name, body.get("description", ""),
                                  steps_json, body.get("trigger_type", "manual"), trigger_config)
    # Reload scheduler
    scheduler = request.app.state.workflow_scheduler
    if scheduler:
        scheduler.reload()
    return {"ok": True, "result": workflow}


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str, request: Request):
    db = get_db(request)
    workflow = ensure_tenant_workflow_access(db, workflow_id, tenant_id_for_request(request))
    if not workflow:
        return JSONResponse({"ok": False, "error": "Workflow not found"}, status_code=404)
    return {"ok": True, "result": workflow}


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_workflow_access(db, workflow_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Workflow not found"}, status_code=404)
    body = await request.json()
    db.update_workflow(workflow_id, body)
    scheduler = request.app.state.workflow_scheduler
    if scheduler:
        scheduler.reload()
    return {"ok": True}


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_workflow_access(db, workflow_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Workflow not found"}, status_code=404)
    db.delete_workflow(workflow_id)
    scheduler = request.app.state.workflow_scheduler
    if scheduler:
        scheduler.reload()
    return {"ok": True}


@router.get("/workflows/{workflow_id}/runs")
async def get_workflow_runs(workflow_id: str, request: Request):
    db = get_db(request)
    runs = db.get_workflow_runs(workflow_id)
    return {"ok": True, "result": runs}


@router.post("/workflows/{workflow_id}/run")
@router.post("/workflows/{workflow_id}/trigger")
async def run_workflow(workflow_id: str, request: Request):
    db = get_db(request)
    workflow = ensure_tenant_workflow_access(db, workflow_id, tenant_id_for_request(request))
    if not workflow:
        return JSONResponse({"ok": False, "error": "Workflow not found"}, status_code=404)
    runner = request.app.state.workflow_runner
    if not runner:
        return JSONResponse({"ok": False, "error": "WorkflowRunner not initialized"}, status_code=500)
    try:
        result = await runner.start(workflow_id, workflow["room_id"])
        return {"ok": True, "result": result}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/workflow-runs/{run_id}/cancel")
async def cancel_workflow_run(run_id: str, request: Request):
    runner = request.app.state.workflow_runner
    if not runner:
        return JSONResponse({"ok": False, "error": "WorkflowRunner not initialized"}, status_code=500)
    runner.cancel(run_id)
    return {"ok": True}


# === Agent Skills ===

@router.get("/agents/{agent_id}/skills")
async def get_agent_skills(agent_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_agent_access(db, agent_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    skills = db.get_agent_skills(agent_id)
    return {"ok": True, "result": skills}


@router.get("/skills")
async def get_all_skills(request: Request):
    db = get_db(request)
    skills = db.get_all_skills(tenant_id_for_request(request))
    return {"ok": True, "result": skills}


@router.get("/skills/{skill_id}")
async def get_skill(skill_id: str, request: Request):
    db = get_db(request)
    skill = ensure_tenant_skill_access(db, skill_id, tenant_id_for_request(request))
    if not skill:
        return JSONResponse({"ok": False, "error": "Skill not found"}, status_code=404)
    return {"ok": True, "result": skill}


@router.post("/agents/{agent_id}/skills")
async def create_skill(agent_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_agent_access(db, agent_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    body = await request.json()
    name = body.get("name")
    if not name:
        return JSONResponse({"ok": False, "error": "name is required"}, status_code=400)
    skill = db.create_skill(agent_id, name, body.get("description", ""),
                            body.get("content", ""), body.get("file_type", "text"))
    return {"ok": True, "result": skill}


@router.put("/skills/{skill_id}")
async def update_skill(skill_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_skill_access(db, skill_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Skill not found"}, status_code=404)
    body = await request.json()
    skill = db.update_skill(skill_id, body)
    return {"ok": True, "result": skill}


@router.delete("/skills/{skill_id}")
async def delete_skill(skill_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_skill_access(db, skill_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Skill not found"}, status_code=404)
    db.delete_skill(skill_id)
    return {"ok": True}


# === Engine Status ===

@router.get("/engine/status")
async def engine_status(request: Request):
    from ai_engine import get_engine_status
    return {"ok": True, "result": get_engine_status()}


@router.post("/skills/{skill_id}/copy")
async def copy_skill(skill_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_skill_access(db, skill_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Source skill not found"}, status_code=404)
    body = await request.json()
    target = body.get("target_agent_id")
    if not target:
        return JSONResponse({"ok": False, "error": "target_agent_id required"}, status_code=400)
    if not ensure_tenant_agent_access(db, target, tenant_id):
        return JSONResponse({"ok": False, "error": "Target agent not found"}, status_code=404)
    skill = db.copy_skill_to_agent(skill_id, target)
    if not skill:
        return JSONResponse({"ok": False, "error": "Source skill not found"}, status_code=404)
    return {"ok": True, "result": skill}


# === Room Skills (isolation per room) ===

@router.get("/rooms/{room_id}/skills")
async def get_room_skills(room_id: str, request: Request):
    db = get_db(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    skills = db.get_room_skills_full(room_id)
    return {"ok": True, "result": skills}


@router.put("/rooms/{room_id}/skills")
async def set_room_skills(room_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    body = await request.json()
    skill_ids = body.get("skill_ids", [])
    for sid in skill_ids:
        if not ensure_tenant_skill_access(db, sid, tenant_id):
            return JSONResponse({"ok": False, "error": f"Skill not found: {sid}"}, status_code=404)
    db.set_room_skills(room_id, skill_ids)
    skills = db.get_room_skills_full(room_id)
    return {"ok": True, "result": skills}


@router.post("/rooms/{room_id}/skills/{skill_id}")
async def add_room_skill(room_id: str, skill_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id) or not ensure_tenant_skill_access(db, skill_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room or skill not found"}, status_code=404)
    db.add_room_skill(room_id, skill_id)
    skills = db.get_room_skills_full(room_id)
    return {"ok": True, "result": skills}


@router.delete("/rooms/{room_id}/skills/{skill_id}")
async def remove_room_skill(room_id: str, skill_id: str, request: Request):
    db = get_db(request)
    tenant_id = tenant_id_for_request(request)
    if not ensure_tenant_room_access(db, room_id, tenant_id) or not ensure_tenant_skill_access(db, skill_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room or skill not found"}, status_code=404)
    db.remove_room_skill(room_id, skill_id)
    skills = db.get_room_skills_full(room_id)
    return {"ok": True, "result": skills}


# === Observability ===

@router.get("/observability/summary")
async def get_observability_summary(request: Request):
    db = get_db(request)
    room_id = request.query_params.get("room_id")
    tenant_id = tenant_id_for_request(request)
    if room_id and not ensure_tenant_room_access(db, room_id, tenant_id):
        return JSONResponse({"ok": False, "error": "Room not found"}, status_code=404)
    if tenant_id is not None and not room_id:
        return JSONResponse({"ok": False, "error": "子账号不能查看全局观测数据"}, status_code=403)
    limit = int(request.query_params.get("limit", "100"))
    return {"ok": True, "result": db.get_observability_summary(room_id, limit)}


# === Hub Settings ===

LOG_FILE = "/tmp/myna.log"
LOG_FILE_ABSPATH = os.path.abspath(LOG_FILE)
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
SECRET_PATTERNS = [
    (re.compile(r"(authorization\s*[:=]\s*bearer\s+)[^\s\"'<>]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(bearer\s+)[A-Za-z0-9._\-+/=]{12,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-[REDACTED]"),
    (re.compile(r"((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\s*[:=]\s*)[^\s,;\"'{}<>]+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(\"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\"\s*:\s*\")[^\"]+", re.I), r"\1[REDACTED]"),
]


def _setting_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _setting_int(value, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(parsed, upper))


def _logging_settings_payload(db) -> dict:
    enabled = _setting_bool(db.get_hub_setting("debug_logging_enabled", "0"))
    level = str(db.get_hub_setting("debug_logging_level", "INFO") or "INFO").upper()
    if level not in LOG_LEVELS:
        level = "INFO"
    retention_days = _setting_int(db.get_hub_setting("debug_logging_retention_days", "7"), 7, 1, 365)
    settings = {
        "enabled": enabled,
        "level": level,
        "retention_days": retention_days,
        "log_path": LOG_FILE,
        "max_lines": 1000,
    }
    return {"ok": True, **settings, "settings": settings, "result": settings}


def _redact_log_line(line: str) -> str:
    redacted = line.rstrip("\n\r")
    for pattern, repl in SECRET_PATTERNS:
        redacted = pattern.sub(repl, redacted)
    return redacted


def _is_allowed_log_file() -> bool:
    return LOG_FILE_ABSPATH == "/tmp/myna.log" and not os.path.islink(LOG_FILE)


@router.get("/logging/settings")
async def get_logging_settings(request: Request):
    db = get_db(request)
    return _logging_settings_payload(db)


async def _update_logging_settings(request: Request):
    db = get_db(request)
    body = await request.json()
    if "enabled" in body:
        db.set_hub_setting("debug_logging_enabled", "1" if bool(body.get("enabled")) else "0")
    if "level" in body:
        level = str(body.get("level") or "").upper()
        if level not in LOG_LEVELS:
            return JSONResponse({"ok": False, "error": "日志级别无效"}, status_code=400)
        db.set_hub_setting("debug_logging_level", level)
    if "retention_days" in body:
        retention_days = _setting_int(body.get("retention_days"), 7, 1, 365)
        db.set_hub_setting("debug_logging_retention_days", str(retention_days))
    return _logging_settings_payload(db)


@router.put("/logging/settings")
async def update_logging_settings(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    return await _update_logging_settings(request)


@router.post("/logging/settings")
async def post_logging_settings(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    return await _update_logging_settings(request)


@router.get("/logging/recent")
async def get_recent_logs(request: Request):
    try:
        lines = int(request.query_params.get("lines", "200"))
    except ValueError:
        lines = 200
    requested_lines = lines
    lines = max(1, min(lines, 1000))
    if not _is_allowed_log_file():
        return JSONResponse({"ok": False, "error": "日志路径不受允许"}, status_code=400)
    if not os.path.exists(LOG_FILE):
        return {"ok": True, "lines": [], "result": [], "path": LOG_FILE, "line_count": 0, "requested_lines": requested_lines}
    if not os.path.isfile(LOG_FILE):
        return JSONResponse({"ok": False, "error": "日志路径不可读取"}, status_code=400)
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            recent = f.readlines()[-lines:]
        redacted = [_redact_log_line(line) for line in recent]
        return {"ok": True, "lines": redacted, "result": redacted, "path": LOG_FILE, "line_count": len(redacted), "requested_lines": requested_lines}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"读取日志失败：{e}"}, status_code=500)


@router.delete("/logging/recent")
async def clear_recent_logs(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    if not _is_allowed_log_file():
        return JSONResponse({"ok": False, "error": "日志路径不受允许"}, status_code=400)
    if not os.path.exists(LOG_FILE):
        return {"ok": True, "cleared": False, "path": LOG_FILE}
    if not os.path.isfile(LOG_FILE):
        return JSONResponse({"ok": False, "error": "日志路径不可清空"}, status_code=400)
    try:
        with open(LOG_FILE, "w", encoding="utf-8"):
            pass
        return {"ok": True, "cleared": True, "path": LOG_FILE}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"清空日志失败：{e}"}, status_code=500)



@router.get("/pinned-conversations")
async def get_pinned_conversations(request: Request):
    db = get_db(request)
    raw = db.get_hub_setting("pinned_conversations", "{}")
    try:
        pins = json.loads(raw or "{}")
    except Exception:
        pins = {}
    if not isinstance(pins, dict):
        pins = {}
    return {"ok": True, "result": pins}


@router.put("/pinned-conversations")
async def update_pinned_conversations(request: Request):
    body = await request.json()
    pins = body.get("pins", {})
    if not isinstance(pins, dict):
        return JSONResponse({"ok": False, "error": "Invalid pins"}, status_code=400)
    clean = {}
    for key, value in pins.items():
        if not isinstance(key, str) or not key:
            continue
        try:
            clean[key] = int(value)
        except Exception:
            clean[key] = 1
    db = get_db(request)
    db.set_hub_setting("pinned_conversations", json.dumps(clean, ensure_ascii=False))
    return {"ok": True, "result": clean}

@router.get("/settings")
async def get_settings(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    db = get_db(request)
    settings = db.get_all_hub_settings()
    # Never expose password hash to frontend
    settings.pop("auth_password", None)
    # Defaults
    defaults = {
        "agent_max_rounds": "50",
        "agent_concurrency": "10",
        "context_messages_limit": "20",
        "self_improve_enabled": "1",
        "self_improve_threshold": "2",
        "self_improve_path": "per_agent",
        # Optional OpenAI-compatible vision fallback used to describe uploaded
        # images for agents whose primary chat model is text-only. Values may
        # also be supplied through MYNA_VISION_* environment variables.
        "vision_model_config_id": "",
        "vision_base_url": "",
        "vision_api_key": "",
        "vision_model": "",
        "vision_params_json": "",
    }
    for k, v in defaults.items():
        if k not in settings:
            settings[k] = v
    return {"ok": True, "result": settings}


@router.put("/settings")
async def update_settings(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    db = get_db(request)
    body = await request.json()
    for key, value in body.items():
        db.set_hub_setting(key, str(value))
    return {"ok": True}


# === Media file serving ===

from fastapi.responses import FileResponse

@router.get("/media/{path:path}")
async def serve_media(path: str, download: str | None = None):
    """Serve media files generated by agents (screenshots, etc.)."""
    import os
    import mimetypes
    # Try multiple locations for the file
    candidates = [
        str(PROFILES_ROOT / path),          # Hermes profiles
        f"/{path}",                         # Absolute path (e.g. /app/backend/project/...)
    ]
    full_path = None
    for candidate in candidates:
        if os.path.isfile(candidate):
            full_path = candidate
            break
    if not full_path:
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)

    filename = os.path.basename(full_path)
    mime, _ = mimetypes.guess_type(full_path)
    mime = mime or "application/octet-stream"

    # Images and videos: inline display; everything else: force download
    is_viewable = mime.startswith("image/") or mime.startswith("video/") or mime == "application/pdf"
    if download or not is_viewable:
        return FileResponse(full_path, filename=filename, media_type=mime,
                           headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    return FileResponse(full_path, media_type=mime)


# === System version & update ===

import subprocess

def _detect_version():
    """Detect version: env var > packaged VERSION file > git tag > fallback."""
    env_ver = os.environ.get("MYNA_VERSION")
    if env_ver:
        return env_ver
    try:
        version_file = APP_ROOT / "VERSION"
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            if version:
                return version
    except Exception:
        pass
    # Try git tag (works when running from source)
    try:
        import subprocess
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().lstrip("v")
    except Exception:
        pass
    return "0.5.0"

MYNA_VERSION = _detect_version()

@router.get("/system/version")
async def get_system_version():
    """Return current version and detect if running in Docker."""
    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    return {"ok": True, "version": MYNA_VERSION, "docker": in_docker}


# Cache for update check (avoid hammering GitHub API)
_update_cache = {"latest": None, "checked_at": 0}


def _version_key(version: str) -> tuple:
    """Return a comparable semantic-ish version key for tags like v0.7.6.

    Avoid external dependencies and keep behavior consistent in Docker images.
    Non-numeric suffixes are ignored for ordering.
    """
    import re
    nums = re.findall(r"\d+", (version or "").lstrip("vV"))
    return tuple(int(n) for n in nums[:4]) or (0,)


@router.get("/system/check-update")
async def check_for_update(request: Request = None):
    if request is not None:
        blocked = require_admin_account(request)
        if blocked:
            return blocked
    """Check GitHub tags for latest version. Server-side with caching (60s)."""
    import time, httpx
    now = time.time()
    # Return cached result if checked within 60s
    if _update_cache["latest"] and (now - _update_cache["checked_at"]) < 60:
        remote = _update_cache["latest"]
    else:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get("https://api.github.com/repos/uskyu/myna/tags?per_page=100")
                if resp.status_code == 200:
                    tags = resp.json()
                    tag_names = [t.get("name", "") for t in tags if t.get("name")]
                    semver_tags = [t for t in tag_names if _version_key(t) != (0,)]
                    remote = max(semver_tags, key=_version_key).lstrip("v") if semver_tags else ""
                    _update_cache["latest"] = remote
                    _update_cache["checked_at"] = now
                else:
                    return JSONResponse({"ok": False, "error": f"GitHub API returned {resp.status_code}"}, status_code=502)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"请求超时或网络错误: {type(e).__name__}"}, status_code=502)

    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    current = MYNA_VERSION.lstrip("v")
    available = bool(remote and _version_key(remote) > _version_key(current))
    return {
        "ok": True,
        "current": current,
        "latest": remote,
        "available": available,
        "docker": in_docker,
    }


@router.post("/system/update")
async def do_system_update(request: Request):
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    """Trigger container update via an external updater.

    A running web app should not pull/recreate its own container. Mature Docker
    apps delegate self-updates to a supervisor/updater process (Watchtower,
    Portainer agent, systemd unit, etc.). Myna's button therefore asks
    Watchtower to perform the update and only streams the requested state.
    """
    import os
    import time
    import httpx

    if not (os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")):
        return JSONResponse({"ok": False, "error": "Not running in Docker mode"}, status_code=400)

    ws_manager = get_ws(request)
    update_url = os.environ.get("WATCHTOWER_UPDATE_URL", "").strip()
    token = os.environ.get("WATCHTOWER_HTTP_API_TOKEN", "").strip()

    if not (update_url and token) and not os.path.exists("/var/run/docker.sock"):
        return JSONResponse({"ok": False, "error": "Watchtower API not configured and Docker socket not mounted"}, status_code=400)

    async def _trigger_external_updater():
        try:
            await ws_manager.notify_ui({
                "type": "update_progress",
                "stage": "requesting_updater",
                "message": "正在通知独立更新器...",
                "percent": 5,
            })

            # Preferred path: Watchtower HTTP API in the same compose network.
            if update_url and token:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(update_url, headers={"Authorization": f"Bearer {token}"})
                if 200 <= resp.status_code < 300:
                    await ws_manager.notify_ui({
                        "type": "update_progress",
                        "stage": "updater_started",
                        "message": "更新器已接管，正在拉取镜像并重启容器...",
                        "percent": 20,
                    })
                    return
                await ws_manager.notify_ui({
                    "type": "update_progress",
                    "stage": "error",
                    "message": f"Watchtower API 调用失败: HTTP {resp.status_code} {resp.text[:120]}",
                    "percent": 0,
                })
                return

            # Compatibility path for older deployments: start a one-shot
            # Watchtower helper container. It is still an external updater; Myna
            # does not attempt to replace its own process.
            updater_name = f"myna-updater-{int(time.time())}"
            cmd = [
                "docker", "run", "-d", "--rm",
                "--name", updater_name,
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "containrrr/watchtower:latest",
                "--run-once",
                "--cleanup",
                "--label-enable",
                "myna-app",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode != 0:
                await ws_manager.notify_ui({
                    "type": "update_progress",
                    "stage": "error",
                    "message": f"启动更新器失败: {(proc.stderr or proc.stdout)[:160]}",
                    "percent": 0,
                })
                return

            await ws_manager.notify_ui({
                "type": "update_progress",
                "stage": "updater_started",
                "message": "一次性 Watchtower 更新器已启动，正在拉取镜像并重启容器...",
                "percent": 20,
            })
        except Exception as e:
            await ws_manager.notify_ui({
                "type": "update_progress",
                "stage": "error",
                "message": f"触发更新失败: {type(e).__name__}: {str(e)[:120]}",
                "percent": 0,
            })

    asyncio.create_task(_trigger_external_updater())
    return {"ok": True, "message": "Update handed off to external updater."}


def _parse_size(s: str) -> int:
    """Parse size string like '12.5MB' to bytes."""
    import re
    s = s.strip().upper()
    m = re.match(r'^([\d.]+)\s*([KMGT]?)B?$', s)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2)
    multipliers = {'': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
    return int(val * multipliers.get(unit, 1))


# === Token Usage Stats (requires auth) ===

@router.get("/token-usage/summary")
async def token_usage_summary(request: Request):
    """Get daily total token usage summary (last N days). Requires auth."""
    if not is_authenticated(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    db = get_db(request)
    days = int(request.query_params.get("days", "30"))
    tenant_id = tenant_id_for_request(request)
    summary = db.get_token_daily_summary(days) if tenant_id is None else db.get_token_daily_by_room(days, tenant_id).get("daily", [])
    totals = db.get_token_daily_totals(tenant_id)
    return {"ok": True, "result": {"daily": summary, "totals": totals}}


@router.get("/token-usage/by-agent")
async def token_usage_by_agent(request: Request):
    """Get per-agent daily token usage breakdown. Requires auth."""
    if not is_authenticated(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    db = get_db(request)
    days = int(request.query_params.get("days", "30"))
    daily = db.get_token_daily_by_agent(days, tenant_id_for_request(request))
    return {"ok": True, "result": daily}


@router.get("/token-usage/by-room")
async def token_usage_by_room(request: Request):
    """Get per-room token usage totals and daily trend. Requires auth."""
    if not is_authenticated(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    db = get_db(request)
    try:
        days = int(request.query_params.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    usage = db.get_token_daily_by_room(days, tenant_id_for_request(request))
    return {"ok": True, "result": usage}


@router.get("/token-usage/agent/{agent_id}")
async def token_usage_single_agent(agent_id: str, request: Request):
    """Get token usage history for a specific agent. Requires auth."""
    if not is_authenticated(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    db = get_db(request)
    days = int(request.query_params.get("days", "30"))
    if not ensure_tenant_agent_access(db, agent_id, tenant_id_for_request(request)):
        return JSONResponse({"ok": False, "error": "Agent not found"}, status_code=404)
    usage = db.get_token_daily_for_agent(agent_id, days)
    agent = db.get_agent_by_id(agent_id)
    return {"ok": True, "result": {"agent": agent, "usage": usage}}


# === Data Directory Management (requires auth) ===

@router.get("/data-dir")
async def get_data_dir(request: Request):
    """Get current data directory information. Requires auth."""
    if not is_authenticated(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    from paths import get_data_dir_info, _migration_pending
    info = get_data_dir_info()
    if _migration_pending:
        info["migration_pending"] = _migration_pending
    return {"ok": True, "result": info}


@router.post("/data-dir/migrate")
async def migrate_data_dir(request: Request):
    """Migrate data to a new directory. Requires auth. App must restart after."""
    if not is_authenticated(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    blocked = require_admin_account(request)
    if blocked:
        return blocked
    body = await request.json()
    new_dir = body.get("new_dir", "").strip()
    if not new_dir:
        return JSONResponse({"ok": False, "error": "请提供新目录路径"}, status_code=400)
    from paths import migrate_data_dir as do_migrate
    result = do_migrate(new_dir)
    return result

