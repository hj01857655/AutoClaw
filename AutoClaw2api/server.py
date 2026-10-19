# -*- coding: utf-8 -*-
"""AutoClaw2api — AutoClaw 云端模型的本地 OpenAI / Anthropic 兼容网关。

把 AutoClaw 桌面客户端消费的托管模型（``zai_auto`` 等）包装成标准
OpenAI Chat Completions 与 Anthropic Messages 协议，任意支持自定义
Base URL 的客户端指向 ``http://127.0.0.1:8378/v1`` 即可使用。

启动:
    pip install -r requirements.txt
    python server.py

默认监听 ``127.0.0.1:8378``（避开其余六个 2api 占用的端口）。
环境变量：``HOST`` / ``PORT`` / ``API_KEY`` / ``AUTOCLAW_BASE`` /
``AUTOCLAW_APP_VERSION``，可写在项目根目录的 ``.env`` 里。

与其余六个 2api 的差别
────────────────────
其余几家靠 OAuth / 密码换 token；AutoClaw 的凭据是 Chromium os_crypt
密文（DPAPI 护主密钥 + AES-256-GCM 护业务文件），**只读不写**，同 Windows
用户身份下直接解出，桌面端刷新登录态后按 mtime 自动跟随。

目录职责
──────
    server.py          调度层：装配 Flask、鉴权闸门、上游重试、请求日志/落盘，
                       聚合与 SSE 产出、管理面板与启动逻辑，不含具体协议形状
    lanes/autoclaw.py  通道层：凭据解密、请求整形（406 三条硬约束在此）、SSE 解析
    routes/            协议路由：chat / anthropic / responses 三个 register(app, core)
    adapters/          协议翻译：anthropic / responses ⇄ Chat
"""
import json
import logging
import os
import sys
import threading
import time
import uuid
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request

# 必须先于 lanes/adapters 导入：lanes/autoclaw.py 在模块级读 AUTOCLAW_APP_KEY，
# load_dotenv() 排在 import 之后的话，.env 里的覆盖值对它无效。
load_dotenv()

from lanes import autoclaw
from adapters import anthropic as ab

LISTEN_HOST = os.getenv("HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("PORT", "8378"))
API_KEY = os.getenv("API_KEY", "").strip()

# AutoClaw 用户数据根（%APPDATA%/AutoClaw-official）。可用环境变量覆盖以便测试。
AUTOCLAW_BASE = os.getenv("AUTOCLAW_BASE", "") or os.path.join(
    os.environ.get("APPDATA", ""), "AutoClaw-official")
AUTOCLAW_ACCOUNT = os.getenv("AUTOCLAW_ACCOUNT", "") or None
AUTOCLAW_APP_VERSION = os.getenv("AUTOCLAW_APP_VERSION", "2.1.0")
UPSTREAM_BASE = os.getenv("AUTOCLAW_UPSTREAM_BASE", autoclaw.DEFAULT_BASE).rstrip("/")
DEFAULT_MODEL = os.getenv("AUTOCLAW_DEFAULT_MODEL", "zai_auto")
# 平台标识对应 main.cjs 的 managedPlatform()：win32->win, darwin->mac
MANAGED_PLATFORM = os.getenv("AUTOCLAW_PLATFORM", "win" if os.name == "nt" else "mac")

app = Flask(__name__)
PANEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.html")

# ── 请求日志（与其余 2api 同一标准）─────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "request.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").disabled = True

# 模型目录缓存：上游每次拉都算一次出站，缓存 10 分钟，失败用兜底清单
_FALLBACK_MODELS = ["zai_auto", "zai_auto-fast"]
_models_lock = threading.Lock()
_models_cache = {"at": 0.0, "ids": []}
_MODELS_TTL = 600.0

# 逐模型探测结果缓存。探测要出网且花几秒，只在用户点按钮时跑，
# 结果同时进 overview 让面板首屏就能看到上次结论。
_probe_lock = threading.Lock()
_probe_cache = {"done": 0, "total": 0, "unavailable": 0, "at": "", "results": []}


# ── 模型名 ────────────────────────────────────────────────────────────
# 客户端从 /v1/models 拉目录，只会填目录里的 id（zai_auto / zai_auto-fast /
# zaicoding_glm-5.3 / zai_glm-5.3-flash），不会自己造名字——所以反代**原样透传**，
# 不解析、不映射、不兜底。上游是唯一的裁决者：它认得就 200，不认得就
# 400 非法模型，那个错误比本地猜一个"你大概想要哪个档"更有用。
#
# 连「空名补默认」都不在这里做：lanes.build_request() 的
# ``payload.get("model") or "zai_auto"`` 已经兜了，再来一层是重复。


# ── 本地鉴权与 CORS ───────────────────────────────────────────────────
def _authorized() -> bool:
    """未设 API_KEY 时仅监听回环、不校验；设了就要求 Bearer 匹配。"""
    if not API_KEY:
        return True
    return request.headers.get("Authorization", "") == "Bearer " + API_KEY


def _cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.after_request
def after_request(resp: Response) -> Response:
    return _cors(resp)


def _guard():
    """返回 None 表示放行，否则返回一个 401 响应。"""
    if _authorized():
        return None
    return _cors(jsonify({"error": {"message": "invalid api key", "type": "auth_error"}})), 401


# ── 凭据 ─────────────────────────────────────────────────────────────
_store = autoclaw.CredentialStore(AUTOCLAW_BASE, AUTOCLAW_ACCOUNT)


def _token() -> str:
    return _store.get()["accessToken"]


DUMP_DIR = os.environ.get("DUMP_DIR", "") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
DUMP_ON = os.environ.get("DUMP", "1").strip() != "0"
DUMP_MAX_BYTES = 512 * 1024
DUMP_MAX_FILES = int(os.environ.get("DUMP_MAX_FILES", "200"))

_seq_lock = threading.Lock()
_req_seq = 0
_BOOT_TAG = time.strftime("%m%d-%H%M%S", time.localtime()) + "-p%d" % os.getpid()


def _next_req_id():
    global _req_seq
    with _seq_lock:
        _req_seq += 1
        return "r%04d" % _req_seq


def _dump_trim():
    if DUMP_MAX_FILES <= 0:
        return
    try:
        entries = sorted((e for e in os.scandir(DUMP_DIR) if e.is_file()),
                         key=lambda e: e.stat().st_mtime)
    except OSError:
        return
    for stale in entries[:len(entries) - DUMP_MAX_FILES]:
        try:
            os.remove(stale.path)
        except OSError:
            pass


def _dump_save(rid, kind, data):
    """落盘请求/响应原文。**绝不落 header** —— X-Authorization 是明文 JWT。"""
    if not DUMP_ON or not data:
        return
    try:
        os.makedirs(DUMP_DIR, exist_ok=True)
        text = data if isinstance(data, str) else data.decode("utf-8", "replace")
        truncated = len(text) > DUMP_MAX_BYTES
        path = os.path.join(DUMP_DIR, "%s.%s.%s.txt%s" % (_BOOT_TAG, rid, kind, ".truncated" if truncated else ""))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text[:DUMP_MAX_BYTES])
        _dump_trim()
    except OSError as e:
        logger.warning("[%s] 落盘失败: %s", rid, e)


@app.before_request
def _log_request():
    g.rid = _next_req_id()
    g.t0 = time.time()
    if request.path in ("/", "/admin", "/health", "/favicon.ico"):
        return
    body = request.get_data(as_text=True)
    summary = ""
    try:
        j = json.loads(body)
        summary = "model=%s msgs=%d tools=%d stream=%s" % (
            j.get("model"), len(j.get("messages") or []),
            len(j.get("tools") or []), j.get("stream"))
    except Exception:
        pass
    ua = (request.headers.get("User-Agent") or "-").replace("\r", " ").replace("\n", " ")
    logger.info("[%s] in: %s %s | %s | ua=%s | body=%dB",
                g.rid, request.method, request.path, summary or "n/a", ua, len(body))
    _dump_save(g.rid, "req", body)
    return None


@app.after_request
def _log_response(response):
    if request.path in ("/", "/admin", "/health", "/favicon.ico"):
        return response
    if response.is_streamed:
        return response
    try:
        resp_body = response.get_data(as_text=True)
    except Exception:
        return response
    logger.info("[%s] done: status=%d %dB %.1fs",
                getattr(g, "rid", "?"), response.status_code,
                len(resp_body), time.time() - getattr(g, "t0", time.time()))
    _dump_save(getattr(g, "rid", "?"), "resp", resp_body)
    return response


def _stream_log(gen, rid, t0):
    """包裹 SSE 生成器：TTFT、字节数、原文落盘、done/断开/中断日志。"""
    total = 0
    ttft = None
    buf = [] if DUMP_ON else None
    try:
        for piece in gen:
            if ttft is None:
                ttft = time.time() - t0
            total += len(piece)
            if buf is not None:
                buf.append(piece)
            yield piece
        if buf is not None:
            _dump_save(rid, "resp", b"".join(buf))
        logger.info("[%s] done: stream %dB %.1fs ttft=%s",
                    rid, total, time.time() - t0,
                    ("%.1fs" % ttft) if ttft is not None else "-")
    except GeneratorExit:
        logger.warning("[%s] client 断开: stream %dB %.1fs", rid, total, time.time() - t0)
        raise
    except Exception as e:
        logger.error("[%s] stream 中断: %s (%dB %.1fs)", rid, e, total, time.time() - t0)
        raise


# ── 上游错误归一 ──────────────────────────────────────────────────────
def _upstream_error(resp) -> tuple:
    """上游错误 → OpenAI error 结构，并把真实原因落到日志。

    406 空 body 与 403 pay-view 是 AutoClaw 特有的两类，给出可操作提示而不是
    裸状态码——否则排障时只看到 "status=406 0B"，无从判断是请求形状还是账号。
    """
    text = ""
    try:
        text = resp.text[:2000]
    except requests.RequestException:
        text = ""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    import re as _re
    peek = _re.sub(r"[A-Za-z0-9_\-]{24,}", "…", " ".join(text.split()))[:200]
    logger.warning("[%s] 上游非 200: HTTP %d ctype=%s %s",
                   getattr(g, "rid", "?"), resp.status_code, ctype or "?", peek)

    reason = autoclaw.classify_upstream_error(resp.status_code, text)
    err_type = "upstream_error"
    if resp.status_code == 401:
        err_type = "authentication_error"
        _store.invalidate()
    elif resp.status_code == 406:
        err_type = "invalid_request_error"

    if "json" in ctype:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            # pay-view 这类响应带 action/code，原样透出比翻译成英文更有用
            if body.get("action") or body.get("code"):
                return jsonify({"error": {
                    "message": reason,
                    "type": err_type,
                    "code": body.get("code"),
                    "action": body.get("action"),
                }}), resp.status_code
            return jsonify({"error": {
                "message": body.get("message") or body.get("msg") or reason,
                "type": err_type,
                "code": body.get("code"),
            }}), resp.status_code

    return jsonify({"error": {
        "message": reason,
        "type": err_type,
        "code": resp.status_code,
    }}), 502 if not text.strip() else resp.status_code


# ── 模型目录 ────────────────────────────────────────────────────────
# 目录拉取失败时的兜底清单，只影响 /v1/models 的显示内容，**不参与路由裁决**
# （模型名直接透传给上游）。本机 2026-10-19 实测目录为这 4 个。
_FALLBACK_MODELS = ["zai_auto", "zai_auto-fast", "zaicoding_glm-5.3", "zai_glm-5.3-flash"]


def _list_models() -> list:
    now = time.time()
    with _models_lock:
        if _models_cache["ids"] and now - _models_cache["at"] < _MODELS_TTL:
            return _models_cache["ids"]
    ids = []
    try:
        ids = autoclaw.fetch_models(UPSTREAM_BASE, _token(), AUTOCLAW_APP_VERSION,
                                    MANAGED_PLATFORM)
    except Exception as exc:
        logger.warning("模型目录异常: %s", exc)
    if not ids:
        ids = list(_FALLBACK_MODELS)
        logger.info("模型目录用兜底清单: %s", ids)
    with _models_lock:
        _models_cache["ids"] = ids
        _models_cache["at"] = now
    return ids


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    denied = _guard()
    if denied:
        return denied
    try:
        ids = _list_models()
    except Exception as error:
        return _cors(jsonify({"error": {"message": str(error), "type": "upstream_error"}})), 502
    return _cors(jsonify({
        "object": "list",
        "data": [{"id": mid, "object": "model", "created": 0, "owned_by": "autoclaw"}
                 for mid in ids],
    }))


# ── 上游调用 ────────────────────────────────────────────────────────
# 401/406/403 都是请求形状或账号状态问题，重试无意义；只对连接层失败与
# 429/5xx 退避重试。max_tokens 造成的 406 在发出前就已消除，不会到这里。
_UPSTREAM_MAX_ATTEMPTS = 4
_UPSTREAM_RETRY_BACKOFF_S = (2.0, 4.0, 8.0)
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _open_upstream(payload: dict):
    """把请求体发往上游，返回 open 的 Response。

    ``payload`` 是**已翻译成 Chat 的请求体**（OpenAI 客户端原样、Anthropic/
    Responses 经 adapters/ 转过），406 三条硬约束在 lanes.build_request 里落实。

    模型名原样透传；留空时 lanes.build_request() 补默认档。
    """
    token = _token()
    for attempt in range(_UPSTREAM_MAX_ATTEMPTS):
        headers, body, meta = autoclaw.build_request(
            payload, token, AUTOCLAW_APP_VERSION, MANAGED_PLATFORM)
        if meta["hits"]:
            logger.info("[%s] 已中和外来身份句: %s",
                        getattr(g, "rid", "?"), ", ".join(meta["hits"]))
        if meta["injected"]:
            logger.info("[%s] 已注入 2.x banner", getattr(g, "rid", "?"))
        try:
            resp = requests.post(UPSTREAM_BASE + autoclaw.DEFAULT_PATH,
                                 headers=headers, json=body, stream=True,
                                 timeout=autoclaw._HTTP_TIMEOUT)
        except requests.RequestException as error:
            if attempt < _UPSTREAM_MAX_ATTEMPTS - 1:
                logger.warning("[%s] 上游连接失败，%.0fs 后重试 %d/%d: %s",
                               getattr(g, "rid", "?"), _UPSTREAM_RETRY_BACKOFF_S[attempt],
                               attempt + 1, _UPSTREAM_MAX_ATTEMPTS - 1, error)
                time.sleep(_UPSTREAM_RETRY_BACKOFF_S[attempt])
                continue
            raise
        if resp.status_code == 401 and attempt < _UPSTREAM_MAX_ATTEMPTS - 1:
            # token 可能刚被桌面端轮换，重新解一次再试（只多一次本地解密）
            resp.close()
            _store.invalidate()
            token = _token()
            logger.info("[%s] 上游 401，重新解密凭据后重试", getattr(g, "rid", "?"))
            continue
        if resp.status_code == 200 or resp.status_code not in _TRANSIENT_STATUS:
            return resp
        if attempt < _UPSTREAM_MAX_ATTEMPTS - 1:
            resp.close()
            logger.warning("[%s] 上游 %d，%.0fs 后重试 %d/%d",
                           getattr(g, "rid", "?"), resp.status_code,
                           _UPSTREAM_RETRY_BACKOFF_S[attempt],
                           attempt + 1, _UPSTREAM_MAX_ATTEMPTS - 1)
            time.sleep(_UPSTREAM_RETRY_BACKOFF_S[attempt])
            continue
        return resp
    raise RuntimeError("上游重试次数耗尽")


def aggregate(upstream, model: str, chunk_id: str) -> dict:
    """把上游响应聚合成非流式 Chat completion。

    上游在 ``stream: false`` 时回单个 JSON 对象、``stream: true`` 时回 SSE 帧，
    选哪个解析器由 ``lanes.stream_events`` 按 Content-Type 定，这里无感。
    """
    content, reasoning, tool_calls = [], [], []
    finish_reason, usage = None, None
    for kind, value in autoclaw.stream_events(upstream, False):
        if kind == "content":
            content.append(value)
        elif kind == "reasoning":
            reasoning.append(value)
        elif kind == "tool_calls":
            tool_calls.append(value)
        elif kind == "finish":
            finish_reason = value
        elif kind == "usage":
            usage = value
        elif kind == "queue":
            logger.info("[%s] 上游排队事件: %s", getattr(g, "rid", "?"), value)
    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = _merge_tool_calls(tool_calls)
    return {
        "id": chunk_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": autoclaw.normalize_usage(usage),
    }


def _merge_tool_calls(fragments: list) -> list:
    """流式 tool_calls 是按 index 分片的，拼回完整调用。"""
    by_index = {}
    order = []
    for frag in fragments:
        for item in (frag if isinstance(frag, list) else [frag]):
            if not isinstance(item, dict):
                continue
            idx = item.get("index", 0)
            if idx not in by_index:
                by_index[idx] = {"id": item.get("id") or "",
                                 "type": "function",
                                 "function": {"name": "", "arguments": ""}}
                order.append(idx)
            slot = by_index[idx]
            if item.get("id"):
                slot["id"] = item["id"]
            fn = item.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
    return [by_index[i] for i in sorted(order)]


def _chat_sse_lines(upstream, model, chunk_id, want_usage):
    """产出标准 Chat SSE 行（带 data: 前缀，含 [DONE] 收尾帧）。"""
    finish_reason, usage, saw_tool_calls, sent_role = None, None, False, False
    tool_buf = []
    for kind, value in autoclaw.iter_sse(upstream):
        if kind == "done":
            break
        if kind == "queue":
            continue
        if kind == "finish":
            finish_reason = value
            continue
        if kind == "usage":
            usage = value
            continue
        if kind == "content":
            delta = {"content": value}
        elif kind == "reasoning":
            delta = {"reasoning_content": value}
        elif kind == "tool_calls":
            saw_tool_calls = True
            tool_buf.append(value)
            continue          # tool_calls 统一在收尾时一次性下发，避免分片错位
        else:
            continue
        if not sent_role:
            delta = dict({"role": "assistant"}, **delta)
            sent_role = True
        yield b"data: " + json.dumps(
            autoclaw.make_chunk(chunk_id, model, delta),
            ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"

    if saw_tool_calls:
        yield b"data: " + json.dumps(
            autoclaw.make_chunk(chunk_id, model,
                                {"tool_calls": _merge_tool_calls(tool_buf)}),
            ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"

    tail = autoclaw.make_chunk(
        chunk_id, model, {},
        finish_reason or ("tool_calls" if saw_tool_calls else "stop"))
    if usage and not want_usage:
        tail["usage"] = autoclaw.normalize_usage(usage)
    yield b"data: " + json.dumps(
        tail, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
    if usage and want_usage:
        yield b"data: " + json.dumps(
            autoclaw.make_chunk(chunk_id, model, {},
                                usage=autoclaw.normalize_usage(usage)),
            ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"
    yield b"data: [DONE]\n\n"


@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    notice = ""
    valid = False
    remain = ""
    try:
        cred = _store.get()
        valid = bool(cred["accessToken"])
        remain = autoclaw._fmt_exp(cred["accessToken"])
    except Exception as error:
        notice = str(error)
    return _cors(jsonify({
        "ok": valid,
        "service": "AutoClaw2api",
        "base": AUTOCLAW_BASE,
        "upstream": UPSTREAM_BASE,
        "account": (_store._cred or {}).get("account", "")[:12] if _store._cred else "",
        "token_exp": remain,
        "notice": notice,
        "models": _list_models() if valid else [],
    }))


# ── 管理面板 ────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
@app.route("/admin", methods=["GET"])
def serve_panel():
    if not os.path.exists(PANEL_PATH):
        return _cors(Response("AutoClaw2api running. (panel.html not installed)",
                              content_type="text/plain; charset=utf-8"))
    with open(PANEL_PATH, "r", encoding="utf-8") as file:
        return Response(file.read(), content_type="text/html; charset=utf-8")


@app.route("/admin/api/overview", methods=["GET", "OPTIONS"])
def admin_overview():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    denied = _guard()
    if denied:
        return denied
    info = {"valid": False, "account": "", "token_exp": "", "notice": "",
            "upstream": UPSTREAM_BASE, "app_version": AUTOCLAW_APP_VERSION,
            "platform": MANAGED_PLATFORM}
    try:
        cred = _store.get()
        info.update({
            "valid": True,
            "account": cred["account"][:12],
            "token_exp": autoclaw._fmt_exp(cred["accessToken"]),
            "updatedAt": cred.get("updatedAt") or "",
        })
    except Exception as error:
        info["notice"] = str(error)
    probe = dict(_probe_cache)
    return _cors(jsonify({
        "service": "AutoClaw2api",
        "credential": info,
        "models": _list_models(),
        "probe": {"done": probe.get("done", 0), "total": probe.get("total", 0),
                  "unavailable": probe.get("unavailable", 0),
                  "at": probe.get("at", ""),
                  "results": probe.get("results", [])},
        "local_auth": bool(API_KEY),
        "app_version": AUTOCLAW_APP_VERSION,
        "platform": MANAGED_PLATFORM,
        "constraints": [
            "请求体禁 max_tokens / max_completion_tokens / max_output_tokens（否则 406 空响应）",
            "system 必须含 2.x banner（否则 406 空响应）",
            "X-Request-Model 必填，且必须是上游模型 id（否则 400 非法模型）",
            "不发 X-Harness-Type（2.x 上 406）",
        ],
    }))


@app.route("/admin/api/refresh-token", methods=["POST", "OPTIONS"])
def admin_refresh_token():
    """强制重新解密凭据（桌面端刚刷新登录态时用；正常情况下按 mtime 自动跟随）。"""
    if request.method == "OPTIONS":
        return _cors(Response(""))
    denied = _guard()
    if denied:
        return denied
    try:
        _store.invalidate()
        cred = _store.get()
        return _cors(jsonify({"ok": True, "message": "凭据已重新解密",
                              "token_exp": autoclaw._fmt_exp(cred["accessToken"])}))
    except Exception as error:
        return _cors(jsonify({"ok": False, "message": str(error)})), 500


@app.route("/admin/api/self-check", methods=["POST", "OPTIONS"])
def admin_self_check():
    """验证三条硬约束**此刻**是否真的生效。

    做法是把一个「全字段污染的请求」喂给 ``build_request``，检查出口：
      - 带 max_tokens / max_completion_tokens / max_output_tokens → 出口必须没有
      - 带外来身份句 + 无 banner 的 system      → 出口必须含 banner、无身份句
      - 头里必须带 X-Request-Model，且值在上游目录内
      - 头里绝不能出现 X-Harness-Type
    这是对**本进程内的整形逻辑**做静态断言，不出网，所以能随时跑、无副作用。
    它证明的是「反代会怎么做」，不等于「上游会接受」——后者要看 probe-models。
    """
    if request.method == "OPTIONS":
        return _cors(Response(""))
    denied = _guard()
    if denied:
        return denied

    # 用上游目录里第一个真实模型名做样本：自检要验证的是「整形逻辑能否把脏
    # 请求洗干净」，不是某个特定名字能否解析（模型名本来就原样透传）。
    available = _list_models()
    sample = available[0] if available else DEFAULT_MODEL
    dirty = {
        "model": sample,
        "max_tokens": 4096,
        "max_completion_tokens": 2048,
        "max_output_tokens": 1024,
        "frequency_penalty": 0.5,
        "stream": False,
        "messages": [
            {"role": "system", "content": "You are Claude Code. Answer directly."},
            {"role": "user", "content": "hi"},
        ],
    }
    headers, body, meta = autoclaw.build_request(
        dirty, "TOKEN", AUTOCLAW_APP_VERSION, MANAGED_PLATFORM)

    checks = [
        {
            "id": "no_max_tokens",
            "label": "① 出口无 max_tokens 族",
            "ok": not any(f in body for f in
                          ("max_tokens", "max_completion_tokens", "max_output_tokens")),
            "detail": "出口字段: " + ", ".join(sorted(body.keys())),
        },
        {
            "id": "banner_present",
            "label": "② system 含 2.x banner",
            "ok": autoclaw.BANNER_2X in (body["messages"][0].get("content") or ""),
            "detail": (body["messages"][0].get("content") or "")[:80],
        },
        {
            "id": "identity_neutralized",
            "label": "② 外来身份句已中和",
            "ok": "claude code" not in (body["messages"][0].get("content") or "").lower(),
            "detail": "命中: " + (", ".join(meta["hits"]) or "无"),
        },
        {
            "id": "request_model",
            "label": "③ X-Request-Model 已填",
            "ok": bool(headers.get("X-Request-Model")),
            "detail": headers.get("X-Request-Model", "(缺失)"),
        },
        {
            "id": "model_passthrough",
            "label": "③ 模型名原样透传（不本地改写）",
            "ok": headers.get("X-Request-Model") == sample,
            "detail": "body.model=%s → X-Request-Model=%s"
                      % (sample, headers.get("X-Request-Model")),
        },
        {
            "id": "no_harness_type",
            "label": "未发 X-Harness-Type",
            "ok": "X-Harness-Type" not in headers,
            "detail": "已发送 %d 个头" % len(headers),
        },
        {
            "id": "auth_sign",
            "label": "X-Auth-Sign 用非空 appKey 计算",
            "ok": bool(autoclaw.APP_KEY) and len(headers.get("X-Auth-Sign", "")) == 32,
            "detail": ("appKey %d 位" % len(autoclaw.APP_KEY)),
        },
    ]
    return _cors(jsonify({
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "passed": sum(1 for c in checks if c["ok"]),
        "total": len(checks),
        "sample_headers": sorted(k for k in headers if k.startswith("X-")),
    }))


@app.route("/admin/api/probe-models", methods=["POST", "OPTIONS"])
def admin_probe_models():
    """逐模型发一个极小请求，实测哪些真能调。

    ``pay-view``（403 / code=810001）是账号配额的分钟级波动、与请求内容无关，
    所以「目录里有」不等于「现在能调」——只有实调能区分。探测用
    ``max_tokens=1`` 级的最小请求，不落盘、不进 request.log 主流程。
    """
    if request.method == "OPTIONS":
        return _cors(Response(""))
    denied = _guard()
    if denied:
        return denied

    requested = request.get_json(silent=True) and request.get_json(silent=True).get("models")
    models = requested or _list_models()
    token = _token()
    results = []
    for mid in models:
        entry = {"id": mid, "ok": False, "status": 0, "note": ""}
        payload = {"model": mid, "max_tokens": 8,
                   "messages": [{"role": "user", "content": "hi"}]}
        try:
            headers, body, _ = autoclaw.build_request(
                payload, token, AUTOCLAW_APP_VERSION, MANAGED_PLATFORM)
            resp = requests.post(UPSTREAM_BASE + autoclaw.DEFAULT_PATH,
                                 headers=headers, json=body, timeout=autoclaw._HTTP_TIMEOUT)
            entry["status"] = resp.status_code
            if resp.status_code == 200:
                entry["ok"] = True
                entry["note"] = "可调"
            else:
                text = resp.text[:300]
                entry["note"] = autoclaw.classify_upstream_error(resp.status_code, text)
                if "pay-view" in text or "810001" in text:
                    entry["note"] = "账号配额暂不可用（pay-view，分钟级波动）"
                elif resp.status_code == 400 and "非法模型" in text:
                    entry["note"] = "上游拒绝该模型 id"
        except Exception as error:
            entry["note"] = "%s: %s" % (type(error).__name__, error)
        results.append(entry)
        logger.info("[probe] %s -> %d %s", mid, entry["status"], entry["note"])

    ok_count = sum(1 for r in results if r["ok"])
    with _probe_lock:
        _probe_cache.update({
            "done": len(results), "total": len(results),
            "unavailable": len(results) - ok_count,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
        })
    return _cors(jsonify({
        "ok": True,
        "total": len(results),
        "callable": ok_count,
        "unavailable": len(results) - ok_count,
        "results": results,
        "message": "探测 %d 个模型，%d 个可调" % (len(results), ok_count),
    }))


# ── 协议路由层挂载 ──────────────────────────────────────────────────
import sys as _sys

from routes import anthropic as anthropic_route
from routes import chat as chat_route
from routes import responses as responses_route

_core = _sys.modules[__name__]
chat_route.register(app, _core)
anthropic_route.register(app, _core)
responses_route.register(app, _core)


if __name__ == "__main__":
    print("AutoClaw2api 启动: http://%s:%s" % (LISTEN_HOST, LISTEN_PORT))
    print("OpenAI Base URL:    http://%s:%s/v1" % (LISTEN_HOST, LISTEN_PORT))
    print("Anthropic Base URL: http://%s:%s" % (LISTEN_HOST, LISTEN_PORT))
    print("上游: %s" % UPSTREAM_BASE)
    print("本地鉴权: %s" % ("已开启（API_KEY）" if API_KEY else "未开启（仅回环）"))
    try:
        cred = _store.get()
        print("凭据: account=%s %s" % (cred["account"][:12],
                                       autoclaw._fmt_exp(cred["accessToken"])))
    except Exception as error:
        print("凭据不可用: %s" % error)
        print("  → 确认 AutoClaw 桌面端已登录，且以同一 Windows 用户身份运行本服务")
    try:
        print("模型目录: %s" % ", ".join(_list_models()))
    except Exception as error:
        print("模型目录拉取失败（用兜底清单）: %s" % error)
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)
