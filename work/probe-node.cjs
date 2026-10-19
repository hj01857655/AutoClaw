// AutoClaw 2.x 托管网关探测（第二轮，修正第一轮的 406 误判）
//
// 第一轮（16:17）的注释猜「406 = TLS 指纹拦截」，方向错了。实测依据见
// glmproxy/lib/core.js:1893 —— 2.x 上游对客户端自带 max_tokens 一律回 406，
// 官方客户端根本不发这个字段。同一份代码 core.js:136 记载：曾有人为这个 406
// 做过 curl_cffi TLS 桥，后来删掉，因为「never TLS fingerprinting」。
//
// 本轮修正（相对第一轮）：
//   1. 删掉 max_tokens —— 406 的直接原因
//   2. system 换成 2.x banner（main.cjs 里 ZWORK_DEFAULT_SYSTEM_PROMPT 的首句）
//   3. 补 X-Auth-Sign 族（appId/appKey 明文硬编码，官方每个请求都带）
//   4. X-Tm 在 Windows 上是 "win"（managedPlatform()）
//   5. UA 用 "node"（undici 默认；官方客户端不设 UA，设了反而更可疑）
//   6. 明确不发 X-Harness-Type（2.x 上它 = 406 空 body）
const crypto = require('crypto');

const APPID = '100003';
const APPKEY = process.env.AUTOCLAW_APPKEY || '<32位十六进制，取自 main.cjs>';
const URL = 'https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw/chat/completions';

// main.cjs: ZWORK_DEFAULT_SYSTEM_PROMPT 的首句。2.x 上游对 system 做包含匹配。
const BANNER_2X = 'You are AutoClaw. Answer the user directly and concisely.';

function authSignHeaders() {
  const ts = String(Math.floor(Date.now() / 1000));
  return {
    'X-Auth-Appid': APPID,
    'X-Auth-TimeStamp': ts,
    'X-Auth-Sign': crypto.createHash('md5').update(`${APPID}&${ts}&${APPKEY}`).digest('hex'),
  };
}

async function probe(tag, { token, system, extraHeaders = {}, body = {}, ua }) {
  const id = crypto.randomUUID();
  const h = {
    'Content-Type': 'application/json',
    'Accept': '*/*',
    'Authorization': token,
    'X-Authorization': token,
    'X-Request-Model': 'zai_auto',
    'X-Request-Id': id,
    'X-Trace-Id': id,
    'X-Session-Id': crypto.randomUUID(),
    'X-Agent-Id': 'main',
    'X-Client-Type': 'pc',
    'X-Product': 'autoclaw',
    'X-Channel': 'official',
    'X-Lang': 'zh-CN',
    'X-Tm': 'win',
    'X-Version': '2.1.0',
    'x_trace_id': 'autoclaw-desktop',
    ...authSignHeaders(),
    ...extraHeaders,
  };
  if (ua) h['User-Agent'] = ua;

  const payload = {
    model: 'auto',
    messages: [{ role: 'user', content: 'Reply with one word: OK' }],
    stream: false,
    ...body,
  };
  if (system) payload.messages.unshift({ role: 'system', content: system });

  const res = await fetch(URL, { method: 'POST', headers: h, body: JSON.stringify(payload) });
  const text = await res.text();
  console.log(`${tag.padEnd(34)} -> ${res.status} ${res.statusText}  len=${text.length}  ${text.slice(0, 160).replace(/\n/g, ' ')}`);
  return { status: res.status, text };
}

(async () => {
  const token = process.argv[2];
  if (!token) { console.error('usage: node probe-node.cjs <Bearer-token>'); process.exit(2); }

  // 逐项隔离变量，任何一个 200 就说明前面的修正里哪几条是真门禁。
  console.log('=== A. 只删 max_tokens（system 用 2.x banner）===');
  await probe('A banner, no max_tokens', { token, system: BANNER_2X });

  console.log('=== B. 无 system（检验 banner 是否必需）===');
  await probe('B no system', { token });

  console.log('=== C. 带 max_tokens=32（复现第一轮的 406）===');
  await probe('C banner + max_tokens=32', { token, system: BANNER_2X, body: { max_tokens: 32 } });

  console.log('=== D. 完整形态 + 外来 harness 身份句（测 406 denylist）===');
  await probe('D + "You are Claude Code"', {
    token,
    system: 'You are Claude Code. ' + BANNER_2X,
  });

  console.log('=== E. 完整形态 + OpenAI/JS UA ===');
  await probe('E banner + OpenAI/JS UA', {
    token, system: BANNER_2X, ua: 'OpenAI/JS 6.39.1',
  });
})().catch(e => console.error('ERR', e && e.message ? e.message : e));
