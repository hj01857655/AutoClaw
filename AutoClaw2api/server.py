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

from lanes import autoclaw
from adapters import anthropic as ab

load_dotenv()

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


# ── 模型名解析 ────────────────────────────────────────────────────────
# 客户端填的模型名（claude-sonnet-5 / gpt-5 / deepseek-v3…）不是上游模型 id，
# 上游一见就 400 非法模型。三类来源按优先级解析，命中即用上游真实 id：
#   1. 直接命中上游目录（zai_auto / zai_auto-fast / zaicoding_glm-5.3 …）
#   2. AUTOCLAW_MODEL_ALIASES 环境变量里的显式映射（JSON）
#   3.  DEFAULT_MODEL（zai_auto）
# 查不到的模型名只警告不拒绝——上游的模型目录可能滞后于服务端，
# 直接放行由上游裁决，比本地黑名单把新模型挡在外面好。
_ALIAS_ENV = os.getenv("AUTOCLAW_MODEL_ALIASES", "")
try:
    MODEL_ALIASES = json.loads(_ALIAS_ENV) if _ALIAS_ENV.strip() else {}
    if not isinstance(MODEL_ALIASES, dict):
        MODEL_ALIASES = {}
except ValueError:
    MODEL_ALIASES = {}
    logger.warning("AUTOCLAW_MODEL_ALIASES 不是合法 JSON，已忽略")


def resolve_model(name: str) -> str:
    """客户端模型名 → 上游模型 id。"""
    name = (name or "").strip()
    if not name:
        return DEFAULT_MODEL
    if name in _list_models():
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    # 家族前缀匹配：claude-* → zai_auto、deepseek-* → zai_glm-5.3-flash
    low = name.lower()
    for prefix, target in (("claude", "zai_auto"), ("deepseek", "zai_glm-5.3-flash"),
                           ("glm", "zai_glm-5.3-flash")):
        if low.startswith(prefix):
            return target
    logger.warning("模型名 %r 不在上游目录，按 %r 路由", name, DEFAULT_MODEL)
    return DEFAULT_MODEL


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

    模型名在这里解析成上游 id：客户端填的 ``claude-sonnet-5`` 之类上游不认，
    直接发会拿到 400 非法模型（路由读的是 X-Request-Model，不是 body.model）。
    """
    token = _token()
    requested = (payload.get("model") or "").strip()
    model = resolve_model(requested)
    if model != requested:
        payload = {**payload, "model": model}
        logger.info("[%s] 模型名 %r 解析为 %r",
                    getattr(g, "rid", "?"), requested, model)
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
    return _cors(jsonify({
        "service": "AutoClaw2api",
        "credential": info,
        "models": _list_models(),
        "local_auth": bool(API_KEY),
        "constraints": [
            "请求体禁 max_tokens / max_completion_tokens（否则 406 空响应）",
            "system 必须含 2.x banner（否则 406 空响应）",
            "X-Request-Model 必填（否则 400 非法模型）",
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
