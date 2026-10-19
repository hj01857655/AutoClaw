# -*- coding: utf-8 -*-
"""lanes/account.py —— AutoClaw 账号面：积分任务领取、余额与流水查询。

这些端点都是 2026-10-19 在桌面端凭据下实测打通的（国内域
autoglm-acceleration-api.zhipuai.cn，token 来自桌面端 DPAPI 解密）：

    claim_task()          POST /autoclaw-proxy/proxy/autoclaw-task-complete
    list_tasks()          GET  /autoclaw-proxy/proxy/autoclaw-task-list
    wallet_balance()      GET  /agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw
    credit_ledgers()      GET  /agent-assetmgr/api/v1/ledgers_std
    points_expiring()     GET  /agent-assetmgr/api/v1/points/expiring

**签到路径反直觉**：文档里写的 ``POST /userapi/v1/user-info`` 是桌面端
main.cjs 的 ``DAILY_CREDITS_CLAIM_PATH``，看着名正言顺，实测在当前账号上
**一分没给**（code:0 但 completed:false）。真正给分的是任务面的
``autoclaw-task-complete``，传 ``task_id=daily_signin`` → 200 分，
余额 22931→23131。所以本模块走任务面，``user-info`` 那条不再实现。

**哪些任务能领，服务端早就告诉你了**：任务列表每项带 ``client_triggered``。
为 ``false`` 的（当前是 ``invite_friends``、``upgrade_pc_app``）点了也是白点，
服务端回「任务不支持客户端触发」——所以这个字段必须带到上层，让 UI 直接
不渲染领取按钮，而不是让用户点了再看报错。

**余额**：``/agent-assetmgr/api/v2/wallets`` 是账号资产面的端点，Web 端与
桌面端同一套后端，桌面端 token 直接可用（实测 code:0）。返回五个分类钱包，
通常只有 ``reward``（奖励积分）非零，其余为 0。

**拿不到不等于零**：查余额失败时本模块返回 None，不返回 0。「余额查不到」和
「余额是 0」对用户是两件事，混成 0 会让人以为号废了。
"""
import json
import logging
import os

import requests

from . import autoclaw

logger = logging.getLogger(__name__)

SOURCE_ID = "autoclaw"
PATH_WALLETS = "/agent-assetmgr/api/v2/wallets"
PATH_POINTS_EXPIRING = "/agent-assetmgr/api/v1/points/expiring"
PATH_LEDGERS = "/agent-assetmgr/api/v1/ledgers_std"
PATH_WALLET_INSTANCES = "/agent-assetmgr/api/v1/wallet-instances"

# 桌面端用户数据根。与 work/decrypt-cred.py 的 AUTOCLAW_USER_DATA_ROOT 同源，
# 仅用于读 device-id.json。
USER_DATA_ROOT = os.environ.get(
    "AUTOCLAW_USER_DATA_ROOT",
    os.path.expandvars(r"%APPDATA%/AutoClaw-official"))

_HTTP_TIMEOUT = 20          # main.cjs DAILY_CREDITS_CLAIM_TIMEOUT_MS = 15e3，放宽到 20s
MAX_RESPONSE_BYTES = 256 * 1024

# 钱包分类 → 中文名。上游已给 display_name，这里只作兜底（万一哪天不给）。
WALLET_LABELS = {
    "reward": "奖励积分",
    "daily": "每日积分",
    "subscription": "订阅积分",
    "fuel_pack": "加油包积分",
    "other": "其他积分",
}

# 只把有余额的钱包返回给上层。五个里通常四个是 0，全塞给面板只是噪音。
NONZERO_ONLY = True


class AccountError(RuntimeError):
    """账号面失败。``code`` 是上游业务码（None 表示网络/协议层失败）。"""

    def __init__(self, message: str, code=None, raw: dict = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


def _headers(token: str, version: str, platform: str) -> dict:
    """签名头 + 身份头 + Bearer。

    ``token`` 直接透传：桌面端解出来的 accessToken 自带 ``Bearer `` 前缀
    （实测），再套一层会变成 "Bearer Bearer ..."。
    """
    headers = {"Content-Type": "application/json",
               "User-Agent": "AutoClaw/%s" % version,
               "Authorization": token}
    headers.update(autoclaw.auth_sign_headers())
    headers.update(autoclaw.client_identity_headers(version, platform))
    return headers


def _post(base: str, path: str, token: str, version: str, platform: str,
          payload: dict) -> dict:
    """POST 并解信封，返回 ``data``。

    上游成功时 HTTP 也是 200，业务结果全在 body 的 ``code`` 里——**不看
    HTTP 码看 code**，与官方客户端 ``if (envelope.code === 0)`` 一致。
    """
    try:
        resp = requests.post(base + path, headers=_headers(token, version, platform),
                             json=payload, timeout=_HTTP_TIMEOUT, stream=True)
    except requests.RequestException as error:
        raise AccountError("账号面请求失败: %s" % error)

    with resp:
        body = resp.content[:MAX_RESPONSE_BYTES]
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AccountError("账号面返回非 JSON（HTTP %d）: %s"
                           % (resp.status_code, body[:120].decode("utf-8", "replace")))

    code = envelope.get("code")
    if code != 0:
        raise AccountError(envelope.get("msg") or "账号面业务失败",
                           code=code, raw=envelope)
    return envelope.get("data") or {}


def _get(base: str, path: str, token: str, version: str, platform: str) -> dict:
    """GET 版本，信封解析与 _post 相同。"""
    try:
        resp = requests.get(base + path, headers=_headers(token, version, platform),
                            timeout=_HTTP_TIMEOUT, stream=True)
    except requests.RequestException as error:
        raise AccountError("账号面请求失败: %s" % error)

    with resp:
        body = resp.content[:MAX_RESPONSE_BYTES]
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AccountError("账号面返回非 JSON（HTTP %d）: %s"
                           % (resp.status_code, body[:120].decode("utf-8", "replace")))

    code = envelope.get("code")
    if code != 0:
        raise AccountError(envelope.get("msg") or "账号面业务失败",
                           code=code, raw=envelope)
    return envelope.get("data") or {}


# ── 每日积分 ────────────────────────────────────────────────────────
# 两条签到路径，实测结论（2026-10-19，桌面端凭据 + 国内域）：
#
#   POST /userapi/v1/user-info            → code:0 但 completed:false，**一分没给**
#   POST .../autoclaw-task-complete        → reward_points:200，余额 22931→23131
#
# 所以这里走 task-complete。user-info 那条是桌面端 main.cjs 的
# DAILY_CREDITS_CLAIM_PATH，看着名正言顺，实际在当前账号上不生效——服务端
# 按天去重，且它自己的响应也承认这次没完成。
# 任务端点的完整路径。**注意形状**：是 /autoclaw-proxy/proxy/autoclaw-task-list，
# 不是 /autoclaw-proxy/proxy/autoclaw/autoclaw-task-list（那个 404）。也就是说
# 它挂在 /autoclaw-proxy/proxy/ 之下，与 UPSTREAM_BASE 的
# /autoclaw-proxy/proxy/autoclaw 是**兄弟**关系，不是父子。
# 所以 base 要传裸域名，不传 UPSTREAM_BASE。
PATH_TASK_COMPLETE = "/autoclaw-proxy/proxy/autoclaw-task-complete"
PATH_TASK_LIST = "/autoclaw-proxy/proxy/autoclaw-task-list"
TASK_DAILY_SIGNIN = "daily_signin"


def claim_task(base: str, token: str, version: str, platform: str,
               task_id: str) -> dict:
    """领指定任务的积分，返回 ``{completed, points, already, rejected}``。

    注意 base 要传 **ACCOUNT_BASE**（裸域名）。任务端点不在 UPSTREAM_BASE 底下，
    两者是 /autoclaw-proxy/proxy/ 下的兄弟路径——详见 PATH_TASK_COMPLETE 注释。

    四种结果必须分开，提示语完全不同：

      - ``completed=True``            真领到了
      - ``already=True``              今天已经领过了（幂等，正常）
      - ``rejected=True``             服务端不认这个客户端触发（``invite_friends``
                                      / ``upgrade_pc_app`` 就是这种），**不是失败，
                                      是"这个任务不该在这里领"**
      - 三者皆 False                  服务端没认这次请求（真失败）

    **受限任务的响应形状和成功时不一样**（实测）：成功/已签过是
    ``{"data":{...}}``，而受限任务是 **HTTP 400 + 顶层 ``{"message": "..."}``，
    压根没有 ``data``**。所以不能看到没有 data 就报"结构不对"——那会把一个
    已知的正常情况当成故障。这里按形状分别处理。

    上游字段：``{already_completed, reward_points, success, task_id}``。
    """
    try:
        resp = requests.post(base + PATH_TASK_COMPLETE,
                             headers=_headers(token, version, platform),
                             json={"task_id": task_id},
                             timeout=_HTTP_TIMEOUT, stream=True)
    except requests.RequestException as error:
        raise AccountError("领取请求失败: %s" % error)
    with resp:
        body = resp.content[:MAX_RESPONSE_BYTES]
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AccountError("领取返回非 JSON（HTTP %d）: %s"
                           % (resp.status_code, body[:120].decode("utf-8", "replace")))
    if not isinstance(envelope, dict):
        raise AccountError("领取响应结构不对: %s" % type(envelope).__name__)

    # 受限任务：HTTP 400，顶层 message，没有 data。已知的正常情况，不是故障。
    if "data" not in envelope:
        message = str(envelope.get("message") or "")
        if resp.status_code >= 400 and message:
            return {"completed": False, "already": False, "rejected": True,
                    "points": 0, "message": message}
        raise AccountError(message or "领取响应里没有 data",
                           code=envelope.get("code"), raw=envelope)

    data = envelope.get("data")
    if not isinstance(data, dict):
        raise AccountError("领取响应结构不对: %s" % type(data).__name__, raw=envelope)

    already = bool(data.get("already_completed"))
    points = data.get("reward_points")
    try:
        points = int(points or 0)
    except (TypeError, ValueError):
        points = 0
    success = bool(data.get("success"))
    # 兜底：万一上游哪天把"不支持"放回 200 的 data 里，也照样认出来
    message = str(data.get("message") or "")
    rejected = (not success) and (not already) and ("不支持" in message)
    return {
        "completed": success and not already,
        "already": already,
        "rejected": rejected,
        "points": points,
        "message": message,
    }


def list_tasks(base: str, token: str, version: str, platform: str) -> list:
    """列任务，返回每项的可领取性与分数。

    响应带信封 ``{"data":[...]}``，**没有 ``code`` 字段**——所以这里不能走
    ``_post`` 的 code 判定，自己解。任务字段：``{task_id, title,
    reward_points, status, task_type, client_triggered, display}``。

    ``client_triggered=False`` 的任务点了也领不到（服务端回「任务不支持客户端
    触发」），这个字段必须带出去，让 UI 别渲染领取按钮。
    """
    try:
        resp = requests.get(base + PATH_TASK_LIST + "?lang=zh-CN",
                            headers=_headers(token, version, platform),
                            timeout=_HTTP_TIMEOUT, stream=True)
    except requests.RequestException as error:
        raise AccountError("任务列表请求失败: %s" % error)
    with resp:
        body = resp.content[:MAX_RESPONSE_BYTES]
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AccountError("任务列表返回非 JSON（HTTP %d）: %s"
                           % (resp.status_code, body[:120].decode("utf-8", "replace")))

    items = envelope.get("data") if isinstance(envelope, dict) else envelope
    if not isinstance(items, list):
        raise AccountError("任务列表结构不对（期望 data 为数组）: %s"
                           % type(items).__name__)

    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        points = item.get("reward_points")
        try:
            points = int(points or 0)
        except (TypeError, ValueError):
            points = 0
        out.append({
            "task_id": str(item.get("task_id") or ""),
            "title": str(item.get("title") or ""),
            "points": points,
            "status": str(item.get("status") or ""),
            "type": str(item.get("task_type") or ""),
            # 默认 True：老字段万一哪天上游不给了，宁可让用户点一下看报错，
            # 也别把本来能领的任务藏起来。
            "claimable": bool(item.get("client_triggered", True)),
        })
    return out


# ── 余额 ────────────────────────────────────────────────────────────
def wallet_balance(base: str, token: str, version: str, platform: str) -> dict:
    """查积分余额。返回 ``{total, wallets}``。

    ``total`` 是 ``total_balance``；``wallets`` 是分类列表，每项
    ``{type, label, balance, view}``。查不到时抛 AccountError——调用方应区分
    "查不到"与"余额为 0"，不要把异常吞成 0。
    """
    data = _get(base, PATH_WALLETS + "?biz_app_id=autoclaw",
                token, version, platform)

    wallets = []
    for item in data.get("wallets") or []:
        balance = item.get("balance")
        try:
            balance = int(balance)
        except (TypeError, ValueError):
            balance = 0
        wallet_type = str(item.get("public_wallet_type") or "").strip()
        entry = {
            "type": wallet_type,
            "label": item.get("display_name") or WALLET_LABELS.get(wallet_type, wallet_type),
            "balance": balance,
            "view": item.get("balance_view") or str(balance),
        }
        if NONZERO_ONLY and balance <= 0:
            continue
        wallets.append(entry)

    try:
        total = int(data.get("total_balance") or 0)
    except (TypeError, ValueError):
        total = 0
    return {"total": total, "wallets": wallets}


# ── 过期积分 ────────────────────────────────────────────────────────
def points_expiring(base: str, token: str, version: str, platform: str) -> dict:
    """查即将过期的积分。

    实测返回 ``{expiring_points, total_points, expire_time_value,
    expire_time_type, expiring_points_text}``。``expire_time_type`` 是
    ``day`` / ``month`` 之类，``expire_time_value`` 是数量（3 = 3 天后）。

    ``expiring_points`` 为 0 时返回空 dict——调用方据此判断要不要提示。
    """
    data = _get(base, PATH_POINTS_EXPIRING, token, version, platform)
    try:
        expiring = int(data.get("expiring_points") or 0)
    except (TypeError, ValueError):
        expiring = 0
    if expiring <= 0:
        return {}
    value = data.get("expire_time_value")
    unit = str(data.get("expire_time_type") or "").strip()
    if isinstance(value, (int, float)) and unit:
        when = "%d %s后" % (int(value), {"day": "天", "month": "个月",
                                        "year": "年"}.get(unit, unit))
    else:
        when = ""
    return {"expiring": expiring, "when": when,
            "text": str(data.get("expiring_points_text") or "")}


# ── 积分流水 ────────────────────────────────────────────────────────
def credit_ledgers(base: str, token: str, version: str, platform: str,
                   page: int = 1, page_size: int = 20) -> list:
    """查积分流水。

    每条 ``mutation_type`` 是 ``debit``（花）或 ``credit``（得），
    ``amount`` 带符号。``metadata`` 里有 ``actual_model`` 与
    ``autoclaw_model_mode``——所以这一条不只是账本，还能回答"刚才那次对话
    花了多少分、走的哪个模型"。

    **时间字段是 ``occurred_at``，不是 ``created_at``**（实测响应里根本没有
    后者的键，取错了会拿到空串，面板上的时间列就全空）。另外还有
    ``wallet_expires_at``——钱包本身的到期时间，不是这笔流水的发生时间。
    """
    path = PATH_LEDGERS + "?page=%d&page_size=%d" % (page, page_size)
    data = _get(base, path, token, version, platform)
    out = []
    for item in data.get("entries") or []:
        if not isinstance(item, dict):
            continue
        amount = item.get("amount")
        try:
            amount = int(amount or 0)
        except (TypeError, ValueError):
            amount = 0
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        out.append({
            "id": str(item.get("id") or ""),
            "amount": amount,
            "kind": str(item.get("mutation_type") or ""),
            "wallet": str(item.get("wallet_type") or ""),
            "scope": str(item.get("wallet_scope") or ""),
            "description": str(item.get("description") or ""),
            "model": str(meta.get("actual_model") or ""),
            "mode": str(meta.get("autoclaw_model_mode") or ""),
            "created_at": str(item.get("occurred_at")
                              or item.get("created_at")
                              or item.get("createdAt") or ""),
        })
    return out
