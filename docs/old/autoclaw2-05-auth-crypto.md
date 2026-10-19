# AutoClaw2-05 鉴权、凭据与 client-sign 协议

状态：A（实测自 main.cjs 反编译阅读）。本篇是安全面核心。

## 本地凭据存储（三层）

| 层 | 类 / 文件 | 机制 |
|---|---|---|
| 主进程宿主密钥 | `HostSecretStore`（`src/main/zwork-agent/host-secret-store.ts`） | `electron.safeStorage`（Windows=DPAPI）整包加密 JSON，写 `<tmp>.mode 384 → rename` 原子替换；解密失败把旧文件改名 `.unreadable-<ts>` 保留并拒绝后续写入（防覆盖可恢复密文） |
| 产品账号 | `createElectronProductAccountEncryption` + `product-account/*` | 同 safeStorage；Linux 上 `basic_text` 后端视为不可用（拒绝明文落盘）；账号指针 `userData\device\active-product-account.json` |
| 连接器凭据 | `EncryptedFileCredentialCustody`（`packages/zwork-agent/src/host/`） | 键为 `JSON.stringify([ownerId, connectionId, version])`，同 safeStorage 加密 |

userData 相关文件实测：`device/active-product-account.json`、`accounts/.legacy-runtime-adopted.json`、`device/pending-account-deletion.json`、`account-profile.json`、`autoclaw-import-journal.json`、`user-notifications.json`、`window-state.json`、`pairings.json`（IM 配对记录）。

## 智谱 client-sign 协议（`packages/zwork-agent/src/host/zhipu-client-sign/`）

对 `bigmodel.cn` / `z.ai`（含子域）且凭据形如 `id.secret` 的 API key 生效。握手端点：`{origin}/api/paas/c1f3a7e2/v2/client`。

**密钥派生（HKDF-SHA256，固定 salt）**
```
KDF_SALT        = "WD_CLIENT_SIGN_KDF_SALT" (utf8)
hmacKey         = HKDF(sha256, ikm=secret, salt, info="getSignKey_hmac", 32B)   // 握手签名钥
kek             = HKDF(sha256, ikm=secret, salt, info="ed25519_priv",   32B)   // 私钥解包 KEK
```

**握手**：message = `get_sign_key\n{id}\n{ts}\n{nonce}`，sig = HMAC-SHA256(hmacKey, message) base64；POST `{apiKey, ts, nonce, sig}`，头带 `Authorization: Bearer id.secret`。响应含服务端加密的 ed25519 私钥：
```
blob = base64(privateCipher) = nonce(12B) || ciphertext || tag(16B)
plain = AES-256-GCM-Decrypt(key=kek, iv=nonce, AAD=id, tag) → base64(PKCS8 DER) → ed25519 私钥
```

**请求签名头**（对 bigmodel/z.ai 的 API 请求附加）：
```
X-Session-Id    会话 id
X-Client-Ts     时间戳
X-Client-Version 版本
X-Client-Nonce  nonce
X-Client-Sig    ed25519_sign("{apiKeyId}\n{ts}\n{version}\n{sessionId}\n{nonce}") base64
X-App-Id        = "autoclaw"（默认，ZHIPU_CLIENT_SIGN_DEFAULT_APP_ID）
X-Client-Pow    PoW 计数器（仅当挖到解时附带）
```
无 Authorization 时回退附 `Authorization: Bearer id.secret`。

**PoW**：8 bit 前导零，上限 5,000,000 次迭代。
```
challenge = sha256("{apiKeyId}\n{appId}\n{sessionId}\n{ts}").hex[0:32]
candidate 从 0 递增，找 sha256("{challenge}\n{candidate}") 前导零 ≥ 8 bits 的 candidate
```

**会话管理常量**：私钥 TTL 6h；握手失败冷却 10min；握手超时 30s；verify 失败窗口 10min 内 2 次即重建。
**服务端 verify 失败分类**：`rekey`（VERIFY_SIGNATURE_INVALID / VERIFY_APIKEY_EXPIRED）→ 重新握手；`hard_stop`（VERIFY_POW_REQUIRED / VERIFY_POW_INVALID / VERIFY_TS_OUT_OF_WINDOW / VERIFY_MISSING_FIELD / VERIFY_INVALID_FORMAT / VERIFY_PROTOCOL_ERROR / VERIFY_APIKEY_DISABLED）；其余 `count_only`。
**旁路开关**：env `ZWORK_CLIENT_SIGN_DISABLED` 关闭、`ZWORK_CLIENT_SIGN_DEBUG_HEADERS` 打调试头。缓存键 = `handshakeUrl|id|sha256(secret)[0:16]`。

## 登录与账号 API（/userapi，release 域）

- 国内：`/userapi/v1/agent-send-code`（验证码）→ `/userapi/v1/agent-login` → `/userapi/v1/agent-refresh`；`/userapi/v1/refresh`、`/userapi/v1/user-info`、`/user-profile`、`/user/write-off`
- 海外：`/userapi/overseasv1/zai-oauth-url|zai-oauth-login`、`google-oauth-url|google-oauth-login`、`oauth-captcha-config`（阿里验证码）、`user-write-off`
- OAuth 回调路由：`/auth/callback-zai`、`/auth/callback-google`（内部 URL scheme 处理）
- 第三方连接：`/userapi/v1/connected-apps/{oauth-url,oauth-login,oauth-refresh,status,disconnect,github/mcp,figma/api}`
- 主进程自解析 JWT exp（`parseJwtExpiryMs`）做刷新调度。

## 出网重试分类

`failureClass ∈ {soft, hard_candidate, network}`，各带独立退避表（SOFT_BACKOFF_MS / NETWORK_BACKOFF_MS / HARD_CANDIDATE_RETRY_MS，具体数值未抄录）。

## 未决问题

- 登录响应体结构（token 字段名、refresh 语义）——静态可从 `/userapi/v1/agent-login` 调用处继续读。
- `pairings.json` 的配对协议（accountId\nclientId，见 820762 行注释，属 IM 扫码绑定面）。
- 握手响应中 sign key 的完整字段（version、过期时间）未逐字段确认。
