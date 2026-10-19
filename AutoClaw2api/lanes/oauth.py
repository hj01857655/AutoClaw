# -*- coding: utf-8 -*-
"""lanes/oauth.py —— AutoClaw 账号面的手机验证码登录与 token 续期。

这一层存在的理由：原先凭据只能从桌面端的 DPAPI 密文里解，于是**必须同机、
同 Windows 用户、装桌面端**，桌面端一退出登录本服务就断。账号面本来就开放了
``/userapi/v1/agent-send-code`` + ``agent-login`` + ``refresh``，所以凭据可以
自己拿、自己续、自己存，不再依赖桌面端。

三个动作（端点全部来自 main.cjs 2.1.0 实证，非猜测）：

    send_code(phone)                       POST /userapi/v1/agent-send-code
    login(phone, code)                     POST /userapi/v1/agent-login
    refresh(refresh_token)                 POST /userapi/v1/refresh

**签名与 device_id**：账号面有自己的中间件签名，与模型网关那套
``X-Auth-Sign = md5(appId&ts&appKey)`` 是**同一个**（实测：缺这三个头时
``/userapi/v1/refresh`` 回 ``code:400002 Middleware sign error``，带上就变成
业务码）。但请求体还必须带 ``source_id: "autoclaw"`` 与 ``device_id``——
见 main.cjs ``AuthApiAdapter.post``：::

    async post(path, payload, signal, includeDeviceIdentity = true, accessToken) {
      const json = includeDeviceIdentity
        ? { ...payload, source_id: "autoclaw", device_id: this.deviceId } : payload;

缺 ``device_id`` 服务端回 ``code:400001 请求数据有问题``（实测）。这两个字段由
官方客户端固定写入，调用方无法覆盖，所以本模块同样固定写入。

**device_id 必须稳定**：``agent-login`` 与随后的 ``agent-identity-enter`` 必须
用同一个 device_id，不一致服务端回 ``631016``（main.cjs 注释明写）。所以本模块
把它生成后持久化，不每次随机。

**企业账号的身份选择坑**（main.cjs ``signInWithCode`` 注释）：带
``identity_selection`` 时 token 字段**全是空串**，绝不能当登录成功。本模块
先判它再判 token，与官方客户端判定顺序一致。
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid

import requests

logger = logging.getLogger(__name__)

# 账号面与模型网关同主机（docs/autoclaw-06-网络面.md：autoglm-acceleration-api
# 是"模型 + 账号 + 业务网关"）。
AUTH_BASE = os.environ.get(
    "AUTOCLAW_AUTH_BASE",
    "https://autoglm-acceleration-api.zhipuai.cn").rstrip("/")

SOURCE_ID = "autoclaw"
PATH_SEND_CODE = "/userapi/v1/agent-send-code"
PATH_LOGIN = "/userapi/v1/agent-login"
PATH_REFRESH = "/userapi/v1/refresh"
PATH_REFRESH_FALLBACK = "/userapi/v1/agent-refresh"

_HTTP_TIMEOUT = 20          # main.cjs AUTH_TIMEOUT_MS = 15e3，放宽到 20s
MAX_RESPONSE_BYTES = 256 * 1024   # main.cjs MAX_AUTH_RESPONSE_BYTES

# device_id 落盘位置。与凭据文件同目录，同等级保护。
_DEVICE_FILE = "device-id.txt"


class AuthError(RuntimeError):
    """账号面失败。``code`` 是上游业务码（None 表示网络/协议层失败）。"""

    def __init__(self, message: str, code=None, raw: dict = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


# 上游业务码 → 人话。抄自 main.cjs 的 mapBusinessFailure3：官方客户端自己也是
# 先把码翻成语义再决定提示什么，直接显示 msg 会得到「请求数据有问题」这种
# 对用户毫无信息量的句子（实测错验证码就是 400001，与参数缺失同一个码）。
BUSINESS_CODES = {
    630201: ("验证码已过期", "重新发一个"),
    630202: ("验证码不对", "检查一下，或重新发"),
    400001: ("请求没被接受", "手机号或验证码格式不对"),
    429: ("操作太频繁", "等一会儿再试"),
    630203: ("操作太频繁", "等一会儿再试"),
    630012: ("账号最近注销过", "换个账号"),
    631010: ("不是该企业成员", "用个人账号登"),
    631011: ("企业已停用", "联系管理员"),
    631012: ("企业服务不可用", "稍后再试"),
    631013: ("企业不支持此操作", "用个人账号登"),
    631014: ("所在地区不支持", "换网络环境"),
    631015: ("需要选身份", "企业账号要先选身份"),
    631016: ("身份凭证过期", "重新登录"),
}


def explain(code) -> tuple:
    """业务码 → (一句话原因, 建议)。认不出的码返回 (None, None)。"""
    if not isinstance(code, int):
        return None, None
    return BUSINESS_CODES.get(code, (None, None))


# ── device_id ────────────────────────────────────────────────────────
def load_or_create_device_id(directory: str) -> str:
    """读 device_id，没有就生成一个并落盘。

    必须稳定：``agent-login`` 与 ``agent-identity-enter`` 要用同一个，不一致
    服务端回 631016。官方客户端同样持久化它（main.cjs 的 device identity 文件）。
    """
    path = os.path.join(directory, _DEVICE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except (OSError, ValueError):
        pass
    device_id = secrets.token_hex(16)
    try:
        os.makedirs(directory, exist_ok=True)
        # 先写临时文件再替换：避免进程在半途被杀留下截断的文件
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(device_id)
        os.replace(tmp, path)
    except OSError as error:
        # 落盘失败不阻塞登录——本次进程内仍可用，只是下次会换一个 id
        logger.warning("device_id 落盘失败（下次登录会换 id）: %s", error)
    return device_id


# ── 账号面请求 ────────────────────────────────────────────────────────
def _sign_headers(app_id: str, app_key: str) -> dict:
    """与 lanes.autoclaw.auth_sign_headers() 同一套签名。

    不直接 import 那个函数，是为了让本模块可以独立单测；算法必须保持一致，
    改一处就得改另一处。
    """
    ts = str(int(time.time()))
    sign = hashlib.md5(("%s&%s&%s" % (app_id, ts, app_key)).encode("utf-8")).hexdigest()
    return {"X-Auth-Appid": app_id, "X-Auth-TimeStamp": ts, "X-Auth-Sign": sign}


def _identity_headers(version: str, platform: str, device_id: str) -> dict:
    """账号面的静态身份头 + 每个请求都要带的 source_id / device_id。"""
    return {
        "X-Tm": platform,
        "X-Version": version,
        "X-Product": SOURCE_ID,
        "X-Channel": "official",
        "X-Lang": "zh-CN",
        "X-Client-Type": "pc",
        "x_trace_id": "autoclaw-desktop",
        "source_id": SOURCE_ID,
        "device_id": device_id,
    }


def _post(path: str, payload: dict, device_id: str, version: str, platform: str,
          app_id: str, app_key: str) -> dict:
    """POST 一个账号面端点，返回 ``data`` 字段。

    上游成功时 HTTP 也是 200，业务结果全在 body 的 ``code`` 里——所以这里
    **不看 HTTP 码看 code**，与官方客户端 ``if (envelope.code === 0)`` 一致。
    """
    headers = {"Content-Type": "application/json", "User-Agent": "AutoClaw/%s" % version}
    headers.update(_sign_headers(app_id, app_key))
    headers.update(_identity_headers(version, platform, device_id))

    try:
        resp = requests.post(AUTH_BASE + path, headers=headers,
                             json={**payload, "source_id": SOURCE_ID,
                                   "device_id": device_id},
                             timeout=_HTTP_TIMEOUT, stream=True)
    except requests.RequestException as error:
        raise AuthError("账号面请求失败: %s" % error)

    with resp:
        body = resp.content[:MAX_RESPONSE_BYTES]
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AuthError("账号面返回非 JSON（HTTP %d）: %s"
                        % (resp.status_code, body[:120].decode("utf-8", "replace")))

    code = envelope.get("code")
    if code != 0:
        reason, advice = explain(code)
        # 有映射就用映射的话（上游 msg 对 400001 这类码毫无信息量），
        # 没有就退回上游原文，别把信息吞了。
        if reason:
            message = reason if not advice else "%s —— %s" % (reason, advice)
            if envelope.get("msg"):
                message += "（上游：%s）" % envelope["msg"]
        else:
            message = envelope.get("msg") or "账号面业务失败"
        raise AuthError(message, code=code, raw=envelope)
    return envelope.get("data") or {}


# ── 三个动作 ────────────────────────────────────────────────────────
def send_code(phone: str, device_id: str, version: str, platform: str,
              app_id: str, app_key: str) -> int:
    """发短信验证码，返回冷却秒数（main.cjs 里是 60）。"""
    data = _post(PATH_SEND_CODE, {"phone": phone}, device_id,
                 version, platform, app_id, app_key)
    cooldown = data.get("cooldownSeconds")
    return int(cooldown) if isinstance(cooldown, (int, float)) else 60


def login(phone: str, code: str, device_id: str, version: str, platform: str,
          app_id: str, app_key: str) -> dict:
    """验证码登录，返回 ``{accessToken, refreshToken, expiresAt?, account?}``。

    ``code`` 转成 Number 再发——main.cjs 是 ``Number(code2)``，发字符串服务端
    按 ``400001`` 拒。

    企业账号会先拿到 ``identity_selection``（token 全空串），那种情况抛
    AuthError 并把 selection 挂在 ``raw`` 上，由调用方决定是否进入身份选择。
    """
    try:
        numeric = int(str(code).strip())
    except ValueError:
        raise AuthError("验证码必须是数字")

    data = _post(PATH_LOGIN, {"phone": phone, "code": numeric}, device_id,
                 version, platform, app_id, app_key)

    # 先判 identity_selection：带它时 token 字段全是空串，当登录成功会错
    if data.get("identity_selection"):
        raise AuthError("该账号需要选择身份（企业账号）", code=None, raw=data)

    token = (data.get("accessToken") or data.get("access_token") or "").strip()
    if not token:
        raise AuthError("登录响应里没有 accessToken", raw=data)
    refresh = (data.get("refreshToken") or data.get("refresh_token") or "").strip()
    if not refresh:
        # 没有 refreshToken 也能用，只是到期后要重新收码
        logger.warning("登录响应里没有 refreshToken，到期后需重新验证码登录")
    return {
        "accessToken": token,
        "refreshToken": refresh,
        "expiresAt": data.get("expiresAt") or data.get("expires_at"),
        "account": data.get("account") or data.get("accountId"),
        "phone": phone,
    }


def refresh(refresh_token: str, device_id: str, version: str, platform: str,
            app_id: str, app_key: str) -> dict:
    """用 refreshToken 换新 token。

    main.cjs 的刷新策略是主路径失败后走兜底路径，这里照做：
    ``/userapi/v1/refresh`` → ``/userapi/v1/agent-refresh``。
    """
    last = None
    for path in (PATH_REFRESH, PATH_REFRESH_FALLBACK):
        try:
            data = _post(path, {"refresh_token": refresh_token}, device_id,
                         version, platform, app_id, app_key)
        except AuthError as error:
            last = error
            continue
        token = (data.get("accessToken") or data.get("access_token") or "").strip()
        if not token:
            last = AuthError("刷新响应里没有 accessToken", raw=data)
            continue
        return {
            "accessToken": token,
            "refreshToken": (data.get("refreshToken")
                             or data.get("refresh_token") or refresh_token).strip(),
            "expiresAt": data.get("expiresAt") or data.get("expires_at"),
        }
    raise last or AuthError("刷新失败")


# ── 本地凭据仓库（DPAPI 加密）────────────────────────────────────────
class OAuthCredentialStore:
    """把 OAuth 拿到的 token 用 Windows DPAPI 加密后存文件。

    与桌面端同一套机制：密文绑死当前 Windows 用户与机器，别人拷走文件在别的
    机器上解不开。解密走 ``win32crypt.CryptProtectData`` 的逆操作
    ``CryptUnprotectData``。

    线程安全；``get()`` 到期前 5 分钟就自动刷新，调用方拿到的永远是可用的
    accessToken。
    """

    # 提前多久触发刷新。上游 token 有效期以 JWT exp 为准，这里只是兜底窗口。
    REFRESH_MARGIN_S = 300

    def __init__(self, directory: str, version: str, platform: str,
                 app_id: str, app_key: str, ttl_s: int = 3600):
        self.directory = directory
        self.version = version
        self.platform = platform
        self.app_id = app_id
        self.app_key = app_key
        self.ttl = ttl_s
        self._lock = threading.Lock()
        self._cred = None
        self._at = 0.0
        self._device_id = load_or_create_device_id(directory)
        self.path = os.path.join(directory, "oauth-credential.bin")

    # -- 加解密 --
    @staticmethod
    def _protect(plaintext: bytes) -> bytes:
        """DPAPI 加密。仅 Windows；别的平台直接报错，不静默降级成明文。"""
        try:
            import win32crypt
        except ImportError:
            raise AuthError("DPAPI 需要 pywin32（pip install pywin32）")
        return win32crypt.CryptProtectData(plaintext, "AutoClaw2api oauth", None,
                                           None, None, 1)

    @staticmethod
    def _unprotect(blob: bytes) -> bytes:
        try:
            import win32crypt
        except ImportError:
            raise AuthError("DPAPI 需要 pywin32（pip install pywin32）")
        _desc, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 1)
        return plaintext

    # -- 落盘 --
    def _save(self, cred: dict):
        payload = json.dumps({
            "accessToken": cred["accessToken"],
            "refreshToken": cred.get("refreshToken", ""),
            "expiresAt": cred.get("expiresAt"),
            "savedAt": int(time.time()),
        }, ensure_ascii=False).encode("utf-8")
        blob = self._protect(payload)
        try:
            os.makedirs(self.directory, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except OSError as error:
            # 落盘失败不影响本次使用，只是下次启动要重新登录
            logger.warning("OAuth 凭据落盘失败（下次启动需重新登录）: %s", error)

    def _load(self) -> dict:
        try:
            with open(self.path, "rb") as f:
                blob = f.read()
        except OSError:
            return None
        try:
            data = json.loads(self._unprotect(blob).decode("utf-8"))
        except Exception as error:
            logger.warning("OAuth 凭据解密失败（换用户/换机器？）需重新登录: %s", error)
            return None
        return data if data.get("accessToken") else None

    # -- 对外 --
    def save_login(self, cred: dict):
        """登录成功后入库。"""
        with self._lock:
            self._cred = dict(cred)
            self._at = time.time()
            self._save(cred)

    def has_saved(self) -> bool:
        with self._lock:
            return self._load() is not None

    def get(self) -> str:
        """返回可用的 accessToken，必要时先刷新。"""
        now = time.time()
        with self._lock:
            if self._cred is None:
                self._cred = self._load()
                self._at = now
            cred = self._cred
            if cred is None:
                raise AuthError(
                    "尚未登录。打开 http://127.0.0.1:%s/admin 用手机验证码登录，"
                    "或把 AUTOCLAW_CRED_SOURCE 改回 desktop 复用桌面端凭据"
                    % os.environ.get("PORT", "8378"))

            if self._expiring(cred, now):
                if not cred.get("refreshToken"):
                    raise AuthError("token 已过期且没有 refreshToken，需重新验证码登录")
                logger.info("OAuth token 即将到期，刷新中")
                cred = refresh(cred["refreshToken"], self._device_id,
                               self.version, self.platform,
                               self.app_id, self.app_key)
                self._cred, self._at = cred, now
                self._save(cred)
                logger.info("OAuth token 已刷新")
            return self._cred["accessToken"]

    def _expiring(self, cred: dict, now: float) -> bool:
        exp = cred.get("expiresAt")
        if isinstance(exp, (int, float)) and exp > 0:
            # expiresAt 可能是秒也可能是毫秒，按量级判
            if exp > 1e12:
                exp /= 1000.0
            return exp - now < self.REFRESH_MARGIN_S
        # 没有 exp 信息就按 TTL 兜底
        return now - self._at >= self.ttl

    def invalidate(self):
        with self._lock:
            self._cred = None
            self._at = 0.0
