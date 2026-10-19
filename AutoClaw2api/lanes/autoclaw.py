# -*- coding: utf-8 -*-
"""AutoClaw 上游通道层：凭据解密 + 请求整形 + SSE 解析。

对应 WorkBuddy2api 的 ``lanes/workbuddy.py``。本文件只关心「怎么跟 AutoClaw
云端说话」，不关心 OpenAI/Anthropic 协议长什么样（那在 adapters/ 与 routes/）。

三条硬约束来自本机实测（docs/autoclaw-10-2x门禁与反代落地.md）：
  1. 请求体绝不能带 ``max_tokens`` / ``max_completion_tokens`` —— 否则 406 空 body
  2. system 必须含 2.x banner —— 否则 406 空 body
  3. 绝不发 ``X-Harness-Type`` —— 2.x 宿主上它 = 406 空 body

凭据走 Chromium os_crypt：密钥在 ``Local State`` 的 ``os_crypt.encrypted_key``
（DPAPI 只护那 32 字节主密钥），业务文件 ``account-credentials.enc`` 是
``"v10" || nonce(12) || ciphertext || tag(16)`` 的 AES-256-GCM。
"""
import base64
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
import uuid

import requests

logger = logging.getLogger(__name__)

# ── 上游契约常量（全部来自 main.cjs 实证，非猜测）─────────────────────
APP_ID = "100003"
# appKey：main.cjs AUTOCLAW_COMPATIBLE_IDENTITY 里明文硬编码的 32 位十六进制
# 客户端标识，只用于算 X-Auth-Sign = md5(appId & timestamp & appKey)。
# 本机 2.1.0（mtime 2026-10-11）main.cjs:466500 实证该值未漂移。
#
# 默认值兜底、环境变量可覆盖：实测（probe7 F）证明 X-Auth-Sign 族不是门禁，
# 三个头全删仍 200，所以缺了它不影响可用性。但既然要发就发对——空 key 会算出
# 一个格式合法但值错误的 sign，比不发更容易误导排障，所以留默认值。
_APP_KEY_DEFAULT = "38d2391985e2369a5fb8227d8e6cd5e5"
APP_KEY = os.environ.get("AUTOCLAW_APP_KEY", "").strip() or _APP_KEY_DEFAULT
DEFAULT_BASE = "https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw"
DEFAULT_PATH = "/chat/completions"
DEFAULT_MODEL_CONFIG_PATH = "/autoclaw-proxy/proxy/autoclaw-model-config"

# main.cjs: ZWORK_DEFAULT_SYSTEM_PROMPT 的首句。上游对 system 做包含匹配，
# 后面四段（FILE_CITATION / OUTPUT_LANGUAGE / USER_COMMUNICATION /
# DECISION_AUTONOMY）是应用自己的输出规范，不过网关。
BANNER_2X = "You are AutoClaw. Answer the user directly and concisely."

# 2.x 上游的 system-prompt denylist（引用级证据，来自 glmproxy shape.js 的实测矩阵）。
# 大小写不敏感的字面子串匹配；命中即 406 空 body。反代默认开启中和。
FOREIGN_IDENTITY_PHRASES = (
    "you are claude code",
    "you are cursor",
    "you are cline",
    "you are codex",
    "you are qwen code",
    "you are opencode",
    "you are an interactive cli tool that helps users with software engineering tasks",
    "you are an interactive agent in traecode",
    "claude agent sdk",
)
FOREIGN_IDENTITY_REPLACEMENT = "You are AutoClaw"
#  complements 而非 identity subject 的条目要单独给替换值，
# 否则 "built on Anthropic's Claude Agent SDK" 会变成语法不通的句子。
FOREIGN_IDENTITY_REPLACEMENTS = {"claude agent sdk": "AutoClaw"}

# 上游拒绝客户端自带 max_tokens 时回的 406 是空 body，与其它 406 无法从
# 响应体区分——所以这三个字段在发出前就必须消失，不能靠响应码事后补救。
# max_output_tokens 是 Responses 协议的同名字段，一并禁发。
FORBIDDEN_BODY_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")

# 这些字段上游不接受，剥掉（OpenAI 客户端普遍会带）
_STRIP_BODY_FIELDS = ("frequency_penalty", "presence_penalty", "logit_bias",
                      "logprobs", "top_logprobs", "n", "seed", "user",
                      "parallel_tool_calls", "store", "metadata", "service_tier")

_TOKEN_TTL_MS = 5 * 60 * 1000     # 凭据文件最多 5 分钟重读一次
_HTTP_TIMEOUT = (10, 300)          # connect, read


# ── 凭据解密 ────────────────────────────────────────────────────────
def _dpapi_unprotect(blob: bytes) -> bytes:
    """Windows DPAPI 解密。非 Windows 平台直接抛，让调用方给出可操作的报错。"""
    try:
        import win32crypt
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            "需要 pywin32（pip install pywin32）才能解密 AutoClaw 凭据；"
            "该机制仅适用于 Windows 同用户身份运行") from exc
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]


def _load_master_key(base: str) -> bytes:
    """Local State -> os_crypt.encrypted_key -> 32B AES-256 主密钥。"""
    path = os.path.join(base, "Local State")
    with io.open(path, encoding="utf-8") as f:
        state = json.load(f)
    raw = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if raw[:5] != b"DPAPI":
        raise RuntimeError("Local State 的 encrypted_key 不是 DPAPI 前缀，格式不符")
    key = _dpapi_unprotect(raw[5:])
    if len(key) != 32:
        raise RuntimeError("主密钥长度 %d，预期 32" % len(key))
    return key


def _decrypt_v10(key: bytes, blob: bytes) -> bytes:
    """"v10" || nonce(12) || ciphertext(N-16) || tag(16) -> plaintext"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if blob[:3] != b"v10":
        raise RuntimeError("凭据文件不是 v10 格式（magic=%r）" % blob[:3])
    return AESGCM(key).decrypt(blob[3:15], blob[15:], None)


def discover_account(base: str) -> str:
    """返回 accounts/ 下的账号目录名（sha256(accountId)）。

    多个账号时取最近修改的那个——桌面端同时登录多号的场景极少，
    而按 mtime 选至少是确定性的。
    """
    root = os.path.join(base, "accounts")
    if not os.path.isdir(root):
        raise RuntimeError("找不到 AutoClaw 账号目录: %s" % root)
    entries = [e for e in os.scandir(root) if e.is_dir()]
    if not entries:
        raise RuntimeError("AutoClaw 账号目录为空: %s" % root)
    entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    return entries[0].name


def decrypt_credential(base: str, account: str = None) -> dict:
    """解出 {accessToken, refreshToken, updatedAt, path}。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    account = account or discover_account(base)
    path = os.path.join(base, "accounts", account, "account-credentials.enc")
    key = _load_master_key(base)
    with io.open(path, "rb") as f:
        blob = f.read()
    cred = json.loads(_decrypt_v10(key, blob).decode("utf-8"))
    token = (cred.get("accessToken") or "").strip()
    if not token:
        raise RuntimeError("凭据文件里没有 accessToken（可能已退出登录）")
    return {
        "accessToken": token,
        "refreshToken": (cred.get("refreshToken") or "").strip(),
        "updatedAt": cred.get("updatedAt"),
        "account": account,
        "path": path,
    }


def token_exp_unix(token: str):
    """从 JWT 读 exp；解不出返回 None。只用于日志/探活，不参与决策。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
        exp = claims.get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


class CredentialStore:
    """线程安全的凭据缓存：按 mtime 热重载，桌面端刷新登录态后自动跟随。"""

    def __init__(self, base: str, account: str = None, ttl_ms: int = _TOKEN_TTL_MS):
        self.base = base
        self.account = account
        self.ttl = ttl_ms / 1000.0
        self._lock = threading.Lock()
        self._cred = None
        self._at = 0.0
        self._mtime = None

    def _stamp(self, path: str):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def get(self) -> dict:
        now = time.time()
        with self._lock:
            if self._cred and now - self._at < self.ttl:
                # TTL 内也查一次 mtime：桌面端换 token 时文件会变，成本一次 stat
                if self._mtime is None or self._stamp(self._cred["path"]) == self._mtime:
                    return self._cred
            cred = decrypt_credential(self.base, self.account)
            self._cred, self._at = cred, now
            self._mtime = self._stamp(cred["path"])
            logger.info("凭据已加载: account=%s updatedAt=%s exp=%s",
                        cred["account"][:12], cred.get("updatedAt"),
                        _fmt_exp(cred["accessToken"]))
            return cred

    def invalidate(self):
        with self._lock:
            self._cred = None
            self._at = 0.0


def _fmt_exp(token: str) -> str:
    exp = token_exp_unix(token)
    if not exp:
        return "?"
    left = exp - time.time()
    return "%s (%.1fh)" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)), left / 3600.0)


# ── 身份头（每次现算，X-Auth-Sign 带时间戳）────────────────────────
def auth_sign_headers() -> dict:
    ts = str(int(time.time()))
    sign = hashlib.md5(("%s&%s&%s" % (APP_ID, ts, APP_KEY)).encode("utf-8")).hexdigest()
    return {"X-Auth-Appid": APP_ID, "X-Auth-TimeStamp": ts, "X-Auth-Sign": sign}


def client_identity_headers(version: str, platform: str = "win") -> dict:
    """官方客户端的静态身份头。

    ``platform`` 对应 main.cjs 的 ``managedPlatform()``：win32->win, darwin->mac。
    """
    return {
        "X-Tm": platform,
        "X-Version": version,
        "X-Product": "autoclaw",
        "X-Channel": "official",
        "X-Lang": "zh-CN",
        "X-Client-Type": "pc",
        "x_trace_id": "autoclaw-desktop",
        # 注意：绝不为「更像官方」而补 X-Harness-Type —— 2.x 上它是 406
    }


# ── 请求整形 ────────────────────────────────────────────────────────
def neutralize_foreign_identity(text: str) -> tuple:
    """把外来 harness 身份句中和掉。返回 (新文本, 命中的短语列表)。"""
    if not text:
        return text, []
    hits = []
    low = text.lower()
    for phrase in FOREIGN_IDENTITY_PHRASES:
        if phrase in low:
            hits.append(phrase)
            repl = FOREIGN_IDENTITY_REPLACEMENTS.get(phrase, FOREIGN_IDENTITY_REPLACEMENT)
            # 大小写不敏感替换，保留原文其余部分
            text = re.sub(re.escape(phrase), repl, text, flags=re.IGNORECASE)
            low = text.lower()
    return text, hits


def sanitize_body(payload: dict, model: str) -> dict:
    """OpenAI 请求体 -> AutoClaw 上游请求体。

    三件事：① 删 max_tokens 族（406 根因）② 剥上游不认识的字段
    ③ model 去 provider 前缀（上游按 X-Request-Model 路由，body 不校验）
    """
    body = {k: v for k, v in payload.items() if k not in _STRIP_BODY_FIELDS}
    for field in FORBIDDEN_BODY_FIELDS:
        body.pop(field, None)
    body["model"] = strip_provider_prefix(model)
    return body


def strip_provider_prefix(model: str) -> str:
    """"zai_auto" -> "auto"；无前缀原样返回。"""
    for sep in ("_", "-"):
        if sep in model:
            head, _, tail = model.partition(sep)
            if head in ("zai", "zaicoding", "tdpsk", "zhipu"):
                return tail
    return model


def ensure_banner(messages: list) -> tuple:
    """保证 system 里含 2.x banner，并中和外来身份句。

    返回 (messages, injected, hits)。策略：
      - 已有 system：banner 缺失则前置，身份句就地中和
      - 无 system：新建一条只带 banner 的 system，放在最前
    就地中和而不是把整段 system 降级成 user —— 后者（glmproxy 的
    quarantine）会改变客户端的语义，这里只做最小必要修改。
    """
    out = [dict(m) for m in (messages or [])]
    injected = False
    hits = []

    for msg in out:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            new, found = neutralize_foreign_identity(content)
            if found:
                msg["content"] = new
                hits.extend(found)
            if BANNER_2X not in new:
                msg["content"] = BANNER_2X + "\n\n" + new
                injected = True
        elif isinstance(content, list):
            # 多段 content：只处理 text 段
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                text = part.get("text") or ""
                new, found = neutralize_foreign_identity(text)
                if found:
                    part["text"] = new
                    hits.extend(found)
                    text = new
                if BANNER_2X not in text:
                    part["text"] = BANNER_2X + "\n\n" + text
                    injected = True

    if not any(m.get("role") == "system" for m in out):
        out.insert(0, {"role": "system", "content": BANNER_2X})
        injected = True

    return out, injected, hits


def build_request(payload: dict, token: str, version: str, platform: str,
                  session_id: str = None, run_id: str = None) -> tuple:
    """组装 (headers, body, meta)。

    ``meta = {"injected": bool, "hits": [...], "model": str}``，供调用方打日志。
    """
    model = payload.get("model") or "zai_auto"
    messages, injected, hits = ensure_banner(payload.get("messages") or [])
    body = sanitize_body({**payload, "messages": messages}, model)

    req_id = uuid.uuid4().hex
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Authorization": token,
        "X-Authorization": token,
        "X-Request-Model": model,          # 路由由它决定，必须是完整 id
        "X-Request-Id": req_id,
        "X-Trace-Id": req_id,
        "X-Client-Type": "pc",
        "X-Agent-Id": "main",
    }
    if session_id:
        headers["X-Session-Id"] = session_id
    if run_id:
        headers["X-Query-Id"] = run_id
    headers.update(auth_sign_headers())
    headers.update(client_identity_headers(version, platform))
    return headers, body, {"injected": injected, "hits": hits, "model": model}


# ── 上游调用 ────────────────────────────────────────────────────────
def fetch_models(base: str, token: str, version: str, platform: str,
                 timeout: int = 20) -> list:
    """拉服务端模型目录；失败返回 []（调用方用兜底清单）。

    该 host 有间歇性 TLS EOF（实测约三成概率），所以重试三次再放弃——
    模型目录只在启动和首次请求时拉，重试成本远低于落进兜底清单的代价。
    """
    url = base.replace("/autoclaw-proxy/proxy/autoclaw", "") + DEFAULT_MODEL_CONFIG_PATH
    headers = {"Accept": "*/*", "Authorization": token, "X-Authorization": token}
    headers.update(auth_sign_headers())
    headers.update(client_identity_headers(version, platform))
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                logger.warning("模型目录 HTTP %d", resp.status_code)
                return []
            return extract_model_ids(resp.json())
        except Exception as exc:
            last_error = exc
            logger.warning("模型目录拉取失败 %d/3: %s", attempt + 1, exc)
            time.sleep(1.0)
    logger.warning("模型目录三次均失败: %s", last_error)
    return []


def extract_model_ids(data) -> list:
    """从形形色色的响应形状里抠出模型 id 列表。抠不出返回 []。"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            mid = node.get("model") or node.get("id") or node.get("model_id")
            if isinstance(mid, str) and mid and len(mid) < 64:
                found.append(mid)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    seen, out = set(), []
    for mid in found:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


# ── SSE 解析 ────────────────────────────────────────────────────────
def iter_sse(resp):
    """产出 (kind, value)：content / reasoning / tool_calls / finish / usage / done。

    与 lanes/workbuddy.py 的同名函数保持同一契约，routes/ 层可原样复用。
    """
    finish_reason, usage = None, None
    saw_tool_calls = False
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            frame = json.loads(data)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue

        # 排队事件（MANAGED_MODEL_QUEUE_SSE_EVENT = "zwork_queue"）
        if frame.get("event") == "zwork_queue" or "zwork_queue" in frame:
            yield "queue", frame.get("zwork_queue") or frame
            continue

        if frame.get("usage"):
            usage = frame["usage"]
        for choice in frame.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield "content", delta["content"]
            if delta.get("reasoning_content"):
                yield "reasoning", delta["reasoning_content"]
            if delta.get("tool_calls"):
                saw_tool_calls = True
                yield "tool_calls", delta["tool_calls"]

    yield "finish", finish_reason or ("tool_calls" if saw_tool_calls else "stop")
    if usage:
        yield "usage", usage
    yield "done", None


def stream_events(resp, want_stream: bool):
    """按上游实际形态选解析器，产出统一的 (kind, value) 序列。

    ``want_stream`` 只作提示：实测请求 ``stream: false`` 时上游回单个 JSON 对象，
    ``stream: true`` 才回 SSE 帧，但这条对应关系上游不承诺——所以以
    Content-Type 为准，JSON 解析失败时再回退当 SSE 文本试一次。

    非 SSE 路径会把整个 body 读进内存：上游单次补全的响应体在百 KB 量级，可接受。
    """
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "event-stream" in ctype:
        return iter_sse(resp)
    return iter_body(resp)


def iter_body(resp):
    """上游非流式响应 → 与 ``iter_sse`` 同构的 (kind, value) 序列。

    实测：请求体 ``stream: false`` 时上游回的是**单个 chat.completion JSON 对象**
    （``Content-Type: application/json``），而 ``stream: true`` 才是 SSE 帧。
    只认 ``data:`` 行的解析器会把非流式响应整个漏掉、吐出空 content —— 所以
    两个入口都要有，且产出契约一致，routes/ 层无需分支。

    仅消费一次 body；调用方负责 close。
    """
    raw = resp.content or b""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        yield "finish", "stop"
        yield "done", None
        return
    try:
        payload = json.loads(text)
    except ValueError:
        # 既不是 JSON 也不是 SSE：可能是上游把错误写进了 body
        yield "error", text[:500]
        return
    if not isinstance(payload, dict):
        yield "finish", "stop"
        yield "done", None
        return

    if payload.get("usage"):
        yield "usage", payload["usage"]

    for choice in payload.get("choices") or []:
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content") or ""
        if reasoning:
            yield "reasoning", reasoning
        content = message.get("content") or ""
        if content:
            yield "content", content
        for call in message.get("tool_calls") or []:
            yield "tool_calls", [call]
        if choice.get("finish_reason"):
            yield "finish", choice["finish_reason"]
            return

    yield "finish", "stop"
    yield "done", None


def make_chunk(chunk_id: str, model: str, delta: dict,
               finish_reason=None, usage=None) -> dict:
    """标准 OpenAI chat.completion.chunk。"""
    frame = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }
    if usage:
        frame["usage"] = usage
    return frame


def normalize_usage(usage: dict) -> dict:
    """AutoClaw 的 usage -> OpenAI 形态。

    实测字段名不保证（prompt/completion/total 是最小集），缺的补 0，
    因为客户端普遍直接读 usage["prompt_tokens"]。
    """
    usage = usage or {}
    def pick(*names):
        for name in names:
            if isinstance(usage.get(name), int):
                return usage[name]
        return 0
    return {
        "prompt_tokens": pick("prompt_tokens", "input_tokens", "promptTokens"),
        "completion_tokens": pick("completion_tokens", "output_tokens", "completionTokens"),
        "total_tokens": pick("total_tokens", "totalTokens"),
    }


# 406 空 body 是请求形状问题，重试无意义；这两类要识别出来给可操作提示。
def classify_upstream_error(status: int, body: str) -> str:
    if status == 406 and not (body or "").strip():
        return ("上游 406 空响应：请求形状不被接受。"
                "检查①请求体是否带了 max_tokens ②system 是否含 2.x banner "
                "③是否误发了 X-Harness-Type")
    if status == 403:
        return "上游 403：可能是积分不足（pay-view）或权限问题"
    if status in (401,):
        return "上游 401：token 失效或过期，重新登录 AutoClaw 桌面端"
    return "上游 HTTP %d" % status
