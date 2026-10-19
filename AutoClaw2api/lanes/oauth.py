# -*- coding: utf-8 -*-
"""lanes/oauth.py —— AutoClaw 账号面的 OAuth 登录与 token 续期。

这一层存在的理由：原先凭据只能从桌面端的 DPAPI 密文里解，于是**必须同机、
同 Windows 用户、装桌面端**，桌面端一退出登录本服务就断。账号面本来就开放了
OAuth 换 token，所以凭据可以自己拿、自己续、自己存，不再依赖桌面端。

流程分三步（端点全部来自 main.cjs 2.1.0 的 ``AuthApiAdapter``，非猜测）：

    1. start()        起 loopback → POST ``/userapi/overseasv1/{p}-oauth-url``
                       传 navigate_uri，拿授权链接
    2. 用户浏览器     在授权页登录，provider 302 回
                       ``http://localhost:{port}/auth/callback-{p}?code=&state=``
    3. _complete()    POST ``/userapi/overseasv1/{p}-oauth-login`` 用
                       code+state+navigate_uri 换 token，DPAPI 加密后落盘

**provider 选哪个**：``zai`` 落到 ``chatglm.cn``（国内智谱账号），``google``
落到 ``accounts.google.com``。国内手机号注册的账号用 ``zai``，与桌面端同一套
账号体系，token 通用。

**签名**：账号面有自己的中间件签名 ``X-Auth-Sign = md5(appId&ts&appKey)``，
与模型网关那套是**同一个**（实测：缺这三个头时 ``/userapi/v1/refresh`` 回
``code:400002 Middleware sign error``，带上就变成业务码）。请求体还必须带
``source_id: "autoclaw"`` 与 ``device_id``——见 ``AuthApiAdapter.post``：::

    const json = includeDeviceIdentity
      ? { ...payload, source_id: "autoclaw", device_id: this.deviceId } : payload;

缺 ``device_id`` 服务端回 ``code:400001 请求数据有问题``（实测）。这两个字段由
官方客户端固定写入，调用方无法覆盖，所以本模块同样固定写入。

**device_id 必须稳定且是 UUID 形**：``oauth-url``、``oauth-login``、``refresh``
要用同一个，官方客户端用 ``crypto.randomUUID()`` 生成后持久化（
``DeviceIdentityStore``）。本模块同样生成后落盘，不每次随机；格式取标准
UUID（带横线），避免服务端按 UUID 形状校验时被拒。

**token 字段是 snake_case**：登录响应里是 ``access_token`` / ``refresh_token``
/ ``user_id``（``loginCandidate`` 明写），不是 camelCase。本模块在边界上归一
成 camelCase，内部与桌面端那份凭据结构保持一致。

**expiresAt 从 JWT 里解**：登录响应本身不返回有效期，``exp`` 在 accessToken
的 payload 里（实测 1 天）。所以落盘时解析 JWT 拿 ``exp``，比按 TTL 猜准。
"""
import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

logger = logging.getLogger(__name__)

# 账号面与模型网关同主机（autoglm-acceleration-api 是"模型 + 账号 + 业务网关"）。
AUTH_BASE = os.environ.get(
    "AUTOCLAW_AUTH_BASE",
    "https://autoglm-acceleration-api.zhipuai.cn").rstrip("/")

SOURCE_ID = "autoclaw"
PATH_OAUTH_URL = "/userapi/overseasv1/%s-oauth-url"
PATH_OAUTH_LOGIN = "/userapi/overseasv1/%s-oauth-login"
PATH_REFRESH = "/userapi/v1/refresh"
PATH_REFRESH_FALLBACK = "/userapi/v1/agent-refresh"

# provider → 授权页必须落在哪个 host。抄自 main.cjs isTrustedOAuthUrl：
# 返回的链接不是这个 host 就判失败，防止把 code 交到第三方。
OAUTH_HOSTS = {
    "google": "accounts.google.com",
    "zai": "chatglm.cn",          # 国内；海外版桌面端是 chat.z.ai
}
# 授权链接 host 白名单的补充项（自建/内网测试用），逗号分隔。
_EXTRA_HOSTS = [h.strip().lower() for h in os.environ.get(
    "AUTOCLAW_OAUTH_EXTRA_HOSTS", "").split(",") if h.strip()]

# loopback 回调。抄自 main.cjs：端口按序试，全占用才失败；路径与 provider 绑定。
CALLBACK_PORTS = (18432, 19654, 19723, 53699)
CALLBACK_PATHS = {"google": "/auth/callback-google", "zai": "/auth/callback-zai"}
OAUTH_ATTEMPT_TIMEOUT_S = 600      # main.cjs OAUTH_ATTEMPT_TIMEOUT_MS = 10 * 6e4

_HTTP_TIMEOUT = 20          # main.cjs AUTH_TIMEOUT_MS = 15e3，放宽到 20s
MAX_RESPONSE_BYTES = 256 * 1024   # main.cjs MAX_AUTH_RESPONSE_BYTES

_DEVICE_FILE = "device-id.txt"


class AuthError(RuntimeError):
    """账号面失败。``code`` 是上游业务码（None 表示网络/协议层失败）。"""

    def __init__(self, message: str, code=None, raw: dict = None):
        super().__init__(message)
        self.code = code
        self.raw = raw or {}


# 上游业务码 → 人话。官方客户端自己也是先把码翻成语义再决定提示什么
# （main.cjs 的 mapBusinessFailure3），直接显示 msg 会得到「请求数据有问题」
# 这种对用户毫无信息量的句子（实测缺 device_id 就是 400001）。
BUSINESS_CODES = {
    400001: ("请求没被接受", "参数缺失或格式不对"),
    400002: ("签名没通过", "检查 appId / appKey"),
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

    必须稳定：``oauth-url`` / ``oauth-login`` / ``refresh`` 要用同一个。官方
    客户端同样持久化它（main.cjs 的 ``DeviceIdentityStore``）。
    """
    path = os.path.join(directory, _DEVICE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except (OSError, ValueError):
        pass
    device_id = str(uuid.uuid4())
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


def jwt_exp(token: str):
    """解 JWT payload 里的 ``exp``（秒）。解不开返回 None。"""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)))
        exp = data.get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


# ── 账号面请求 ────────────────────────────────────────────────────────
def _sign_headers(app_id: str, app_key: str, version: str, platform: str) -> dict:
    """账号面完整身份头。

    ``X-Auth-*`` 三件套只是中间件签名，**不是全部**。main.cjs 里 auth 与模型
    两个 client 都过同一个 managed 层（``createZworkManagedRequestHeaders``），
    那层还会注入 ``X-Version`` / ``X-Tm`` / ``X-Product`` / ``X-Lang`` /
    ``X-Channel``。实测只发三件套时 ``zai-oauth-url`` 回 ``631000 User login
    error``——签名过了（否则是 400002 sign error），业务层因缺少客户端身份
    标识而拒。补齐后同一请求即通过。

    平台值取 main.cjs ``managedPlatform()`` 的映射：win32→win、darwin→mac。
    """
    ts = str(int(time.time()))
    sign = hashlib.md5(("%s&%s&%s" % (app_id, ts, app_key)).encode("utf-8")).hexdigest()
    return {
        "X-Auth-Appid": app_id,
        "X-Auth-TimeStamp": ts,
        "X-Auth-Sign": sign,
        "X-Version": version,
        "X-Tm": platform,
        "X-Product": "autoclaw",
        "X-Lang": "zh-CN",
        "X-Channel": "official",
    }


def _post(path: str, payload: dict, device_id: str, version: str, platform: str,
          app_id: str, app_key: str) -> dict:
    """POST 一个账号面端点，返回 ``data`` 字段。

    上游成功时 HTTP 也是 200，业务结果全在 body 的 ``code`` 里——所以这里
    **不看 HTTP 码看 code**，与官方客户端 ``if (envelope.code === 0)`` 一致。
    """
    headers = {"Content-Type": "application/json",
               "User-Agent": "AutoClaw/%s" % version}
    headers.update(_sign_headers(app_id, app_key, version, platform))
    # source_id 与 device_id 由官方客户端固定写进请求体，服务端认这两个键
    body = dict(payload)
    body["source_id"] = SOURCE_ID
    body["device_id"] = device_id

    try:
        resp = requests.post(AUTH_BASE + path, headers=headers,
                             json=body, timeout=_HTTP_TIMEOUT, stream=True)
    except requests.RequestException as error:
        raise AuthError("账号面请求失败: %s" % error)

    with resp:
        raw = resp.content[:MAX_RESPONSE_BYTES]
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AuthError("账号面返回非 JSON（HTTP %d）: %s"
                        % (resp.status_code, raw[:120].decode("utf-8", "replace")))

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


def _trusted(url: str, provider: str) -> bool:
    """授权链接是否落在该 provider 应有的 host 上。

    抄 main.cjs ``isTrustedOAuthUrl``：必须 https、不带用户凭证、不带端口、
    host 精确匹配。这一步是把 code 交出去前的最后一道校验——授权页跑到别家
    域名上，说明链接被换过，交 code 等于把账号交出去。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username or parsed.password or parsed.port:
        return False
    host = (parsed.hostname or "").lower()
    expected = OAUTH_HOSTS.get(provider, "")
    return host == expected or host in _EXTRA_HOSTS


# ── 三步流程 ──────────────────────────────────────────────────────────
def oauth_url(provider: str, navigate_uri: str, device_id: str, version: str,
              platform: str, app_id: str, app_key: str) -> str:
    """第一步：拿授权页链接。"""
    if provider not in OAUTH_HOSTS:
        raise AuthError("provider 只能是 %s" % " / ".join(OAUTH_HOSTS))
    data = _post(PATH_OAUTH_URL % provider, {"navigate_uri": navigate_uri},
                 device_id, version, platform, app_id, app_key)
    url = str(data.get("oauth_url") or "").strip()
    if not url:
        raise AuthError("响应里没有 oauth_url", raw=data)
    if not _trusted(url, provider):
        raise AuthError("授权链接不在预期 host（%s），已拒绝: %s"
                        % (OAUTH_HOSTS.get(provider), url))
    return url


def exchange_oauth(provider: str, code: str, state: str, navigate_uri: str,
                   device_id: str, version: str, platform: str,
                   app_id: str, app_key: str) -> dict:
    """第三步：code + state 换 token。

    上游字段是 snake_case（``loginCandidate`` 读 ``access_token`` /
    ``refresh_token`` / ``user_id``），这里归一成 camelCase，与桌面端那份
    凭据结构对齐，下游不用分心。
    """
    if not code or not state:
        raise AuthError("回调里没有 code 或 state（用户取消了授权？）")
    data = _post(PATH_OAUTH_LOGIN % provider,
                 {"code": code, "state": state, "navigate_uri": navigate_uri},
                 device_id, version, platform, app_id, app_key)

    token = str(data.get("access_token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    account = str(data.get("user_id") or "").strip()
    if not token or not refresh_token or not account:
        # 官方客户端同样把这三样齐全才算成功（loginPayloadIncomplete）
        raise AuthError("登录响应不完整（需要 access_token / refresh_token / user_id）",
                        raw=data)
    cred = {
        "accessToken": token,
        "refreshToken": refresh_token,
        "account": account,
        "expiresAt": jwt_exp(token),
        "phone": data.get("user_phone") or "",
        "accountType": data.get("account_type") or "",
        "savedAt": int(time.time()),
    }
    if not refresh_token:
        logger.warning("登录响应里没有 refreshToken，到期后需重新授权")
    return cred


def refresh(refresh_token: str, device_id: str, version: str, platform: str,
            app_id: str, app_key: str, account: str = "") -> dict:
    """用 refreshToken 换新 token。

    main.cjs 的刷新策略是主路径失败后走兜底路径，这里照做：
    ``/userapi/v1/refresh`` → ``/userapi/v1/agent-refresh``。

    实测（2026-10-19，桌面端凭据）**这条是通的**，``code:0``。两个反直觉的点：

    - 响应里**没有 ``user_id``**（``account`` 只能由调用方传进来保留，
      否则续期后账号会变成空串）；
    - 响应带 ``refresh_token``（新的）与 ``refresh``（**bool，不是 token**，
      别被字段名骗了）。实测 refreshToken **不是一次性的**——旧值与新值
      都能再刷，所以存错了也不会把账号锁死。

    这也是"不靠 OAuth 也能续期"的那条路：桌面端凭据里本就带 refreshToken。
    """
    last = None
    for path in (PATH_REFRESH, PATH_REFRESH_FALLBACK):
        try:
            data = _post(path, {"refresh_token": refresh_token},
                         device_id, version, platform, app_id, app_key)
        except AuthError as error:
            last = error
            continue
        token = str(data.get("access_token") or "").strip()
        if not token:
            last = AuthError("刷新响应里没有 access_token", raw=data)
            continue
        new_refresh = str(data.get("refresh_token") or "").strip() or refresh_token
        return {
            "accessToken": token,
            "refreshToken": new_refresh,
            # 上游不给 user_id，保留调用方传进来的那个
            "account": account,
            "expiresAt": jwt_exp(token),
            "savedAt": int(time.time()),
        }
    raise last or AuthError("刷新失败")


# ── loopback 回调服务 ─────────────────────────────────────────────────
class _CallbackHandler(BaseHTTPRequestHandler):
    """接 provider 302 回来的 code / state，然后回一页给人看。

    与 main.cjs 的判定一致：路径不对 404，缺 code 或缺 state 算取消
    （服务端可能带 ``error=access_denied`` 跳回来）。
    """

    def log_message(self, fmt, *args):        # 别往 stderr 刷访问日志
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        code = (parse_qs(parsed.query).get("code") or [""])[0].strip()
        state = (parse_qs(parsed.query).get("state") or [""])[0].strip()
        denied = "error" in parse_qs(parsed.query) or not code or not state
        ok = parsed.path == self.server.expected_path and not denied
        page = _CALLBACK_PAGE[1 if ok else 0]
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)
        if ok and not self.server.settled:
            self.server.settled = True
            self.server.result = {"code": code, "state": state}


_CALLBACK_PAGE = (
    # 用 str 存、发送前再 encode：bytes 字面量不允许非 ASCII 字符，
    # 而这两页要给中文用户看。
    ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
     "<title>授权未完成</title></head><body style='font:14px system-ui;"
     "display:grid;place-items:center;min-height:100vh;margin:0'>"
     "<p>授权没有完成，可以关掉这个页面回 AutoClaw2api 重试。</p></body></html>"),
    ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
     "<title>授权成功</title></head><body style='font:14px system-ui;"
     "display:grid;place-items:center;min-height:100vh;margin:0'>"
     "<p>已收到授权，可以关掉这个页面回 AutoClaw2api 了。</p></body></html>"),
)


class OAuthFlow:
    """一次授权流程的编排：起 loopback → 取授权链接 → 等回调 → 换 token。

    用法::

        flow = OAuthFlow(store, provider="zai")
        url = flow.begin()                 # 把人送到这个 url
        cred = flow.wait()                 # 阻塞到授权完成 / 超时 / 取消
        flow.cancel()                      # 提前放弃

    ``begin()`` 与 ``wait()`` 分开，是为了让调用方（Flask 路由）能先把授权
    链接返回给面板，再由浏览器真正发起授权。
    """

    def __init__(self, store, provider: str):
        if provider not in OAUTH_HOSTS:
            raise AuthError("provider 只能是 %s" % " / ".join(OAUTH_HOSTS))
        self.store = store
        self.provider = provider
        self._server = None
        self._port = None
        self._path = CALLBACK_PATHS[provider]
        self._url = None
        self._settled = False

    # -- 第一步 --
    def begin(self) -> str:
        """起回调服务并取授权链接，返回给用户打开的 URL。"""
        self._bind()
        navigate_uri = "http://localhost:%d%s" % (self._port, self._path)
        self._url = oauth_url(
            self.provider, navigate_uri, self.store.device_id,
            self.store.version, self.store.platform,
            self.store.app_id, self.store.app_key)
        return self._url

    @property
    def url(self):
        return self._url

    # -- 第二步 --
    def wait(self, timeout_s: float = OAUTH_ATTEMPT_TIMEOUT_S) -> dict:
        """阻塞等回调，拿到 code/state 后立刻换 token 并落盘。"""
        if self._server is None:
            raise AuthError("先调 begin() 再 wait()")
        deadline = time.time() + timeout_s
        while not self._server.settled:
            if time.time() >= deadline:
                raise AuthError("等授权等超时了（%d 秒），重新走一遍" % timeout_s)
            time.sleep(0.3)
        result = self._server.result
        if not result:
            raise AuthError("授权没有完成")
        cred = exchange_oauth(
            self.provider, result["code"], result["state"],
            "http://localhost:%d%s" % (self._port, self._path),
            self.store.device_id, self.store.version, self.store.platform,
            self.store.app_id, self.store.app_key)
        self.store.save_login(cred)
        return cred

    def cancel(self):
        self._settled = True
        self._close()

    def _bind(self):
        """按 main.cjs 的端口顺序试，全占用才失败。"""
        last = None
        for port in CALLBACK_PORTS:
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
            except OSError as error:
                last = error
                continue
            server.expected_path = self._path
            server.settled = False
            server.result = None
            server.daemon_threads = True
            self._server, self._port = server, port
            return
        raise AuthError("回调端口全被占用了（%s），关掉占用进程再试: %s"
                        % (CALLBACK_PORTS, last))

    def _close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None



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
        self.path = os.path.join(directory, "oauth-credential.bin")
        # device_id 与凭据同目录、同等级保护：它参与签名请求，且必须跨进程稳定
        self.device_id = load_or_create_device_id(directory)

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
            "account": cred.get("account", ""),
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
            # 落盘失败不影响本次使用，只是下次启动要重新授权
            logger.warning("OAuth 凭据落盘失败（下次启动需重新授权）: %s", error)

    def _load(self) -> dict:
        try:
            with open(self.path, "rb") as f:
                blob = f.read()
        except OSError:
            return None
        try:
            data = json.loads(self._unprotect(blob).decode("utf-8"))
        except Exception as error:
            logger.warning("OAuth 凭据解密失败（换用户/换机器？）需重新授权: %s", error)
            return None
        return data if data.get("accessToken") else None

    # -- 对外 --
    def save_login(self, cred: dict):
        """授权成功后入库。"""
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
                    "尚未登录。打开 http://127.0.0.1:%s/admin 用 OAuth 授权登录，"
                    "或把 AUTOCLAW_CRED_SOURCE 改回 desktop 复用桌面端凭据"
                    % os.environ.get("PORT", "8378"))

            if self._expiring(cred, now):
                if not cred.get("refreshToken"):
                    raise AuthError("token 已过期且没有 refreshToken，需重新授权")
                logger.info("OAuth token 即将到期，刷新中")
                cred = refresh(cred["refreshToken"], self.device_id,
                               self.version, self.platform,
                               self.app_id, self.app_key)
                self._cred, self._at = cred, now
                self._save(cred)
                logger.info("OAuth token 已刷新")
            return self._cred["accessToken"]

    def _expiring(self, cred: dict, now: float) -> bool:
        exp = cred.get("expiresAt")
        if isinstance(exp, (int, float)) and exp > 0:
            return exp - now < self.REFRESH_MARGIN_S
        # 没有 exp 信息就按 TTL 兜底
        return now - self._at >= self.ttl

    def invalidate(self):
        with self._lock:
            self._cred = None
            self._at = 0.0

    def flow(self, provider: str) -> OAuthFlow:
        """起一次授权流程。"""
        return OAuthFlow(self, provider)
