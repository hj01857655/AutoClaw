// 第三轮：结清 autoclaw-10 的两个未决项
//   1. X-Auth-Sign 族是否真门禁（09/10 篇始终照发，未拆解）
//   2. 403 pay-view 是否持续（复跑 A 变体时出现，用户首轮是 200）
// 顺带测 X-Session-Id / X-Agent-Id / X-Request-Model 是否必需。
const crypto = require('crypto');

const APPID = '100003';
const APPKEY = process.env.AUTOCLAW_APPKEY || '<32位十六进制，取自 main.cjs>';
const URL = 'https://autoglm-acceleration-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw/chat/completions';
const BANNER_2X = 'You are AutoClaw. Answer the user directly and concisely.';

function authSignHeaders() {
  const ts = String(Math.floor(Date.now() / 1000));
  return {
    'X-Auth-Appid': APPID,
    'X-Auth-TimeStamp': ts,
    'X-Auth-Sign': crypto.createHash('md5').update(`${APPID}&${ts}&${APPKEY}`).digest('hex'),
  };
}

async function probe(tag, token, { drop = [], body = {}, system = BANNER_2X }) {
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
  };
  for (const k of drop) delete h[k];
  if (system) h['__sys__'] = system; // 占位，下面拼 body

  const payload = {
    model: 'auto',
    messages: system
      ? [{ role: 'system', content: system }, { role: 'user', content: 'Reply with one word: OK' }]
      : [{ role: 'user', content: 'Reply with one word: OK' }],
    stream: false,
    ...body,
  };

  const res = await fetch(URL, { method: 'POST', headers: h, body: JSON.stringify(payload) });
  const text = await res.text();
  console.log(`${tag.padEnd(38)} -> ${res.status}  len=${String(text.length).padEnd(5)} ${text.slice(0, 130).replace(/\n/g, ' ')}`);
  return res.status;
}

(async () => {
  const token = process.argv[2];
  if (!token) { console.error('usage: node probe7.cjs <Bearer-token>'); process.exit(2); }

  console.log('=== A1/A2 重复完整形态（看 pay-view 是否持续）===');
  await probe('A1 full', token, {});
  await new Promise(r => setTimeout(r, 3000));
  await probe('A2 full (3s later)', token, {});

  console.log('=== F. 去掉 X-Auth-Sign 族（测是否门禁）===');
  await probe('F no X-Auth-Sign', token, { drop: ['X-Auth-Appid', 'X-Auth-TimeStamp', 'X-Auth-Sign'] });

  console.log('=== H. 去掉 X-Session-Id / X-Agent-Id ===');
  await probe('H no session/agent id', token, { drop: ['X-Session-Id', 'X-Agent-Id'] });

  console.log('=== I. 去掉 X-Request-Model ===');
  await probe('I no X-Request-Model', token, { drop: ['X-Request-Model'] });
})().catch(e => console.error('ERR', e && e.message ? e.message : e));
