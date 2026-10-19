# -*- coding: utf-8 -*-
"""模型名透传回归：跑之前先启动 server.py。

覆盖三协议 × {真实 id / 空名 / 未知名}，另加空名连发三次——验证服务无状态、
不会因并发或重试把 model 悄悄换掉。未知名用例预期拿到上游 400 非法模型，
那是透传的正确结果，不是缺陷。最后跑两条流式，确认 SSE 帧完整收尾。
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8378"

CASES = [
    ("chat  真实id", "/v1/chat/completions",
     {"model": "zai_auto", "messages": [{"role": "user", "content": "说三个字"}]}),
    ("chat  空model", "/v1/chat/completions",
     {"messages": [{"role": "user", "content": "hi"}]}),
    ("chat  未知名", "/v1/chat/completions",
     {"model": "nope-xyz", "messages": [{"role": "user", "content": "hi"}]}),
    ("anth  真实id", "/v1/messages",
     {"model": "zai_auto", "max_tokens": 4096,
      "messages": [{"role": "user", "content": "说三个字"}]}),
    ("anth  空model", "/v1/messages",
     {"max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]}),
    ("resp  真实id", "/v1/responses",
     {"model": "zai_auto", "max_output_tokens": 2048, "input": "说三个字"}),
    ("resp  空model", "/v1/responses",
     {"max_output_tokens": 2048, "input": "hi"}),
]


def call(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, str(e)


def note_for(path, st, txt):
    if st != 200:
        try:
            err = json.loads(txt).get("error", {})
            return (err.get("message") or txt)[:90]
        except ValueError:
            return txt[:90]
    try:
        d = json.loads(txt)
        if path.endswith("messages"):
            return (d.get("content") or [{}])[0].get("text", "")[:24]
        if path.endswith("responses"):
            out = d.get("output") or []
            return "".join(i.get("text", "") for it in out
                           for i in (it.get("content") or []))[:24]
        return (d["choices"][0]["message"].get("content") or "")[:24]
    except ValueError as e:
        return "parse-err " + str(e)


for name, path, body in CASES:
    st, txt = call(path, body)
    print("%-16s %-4s %s" % (name, st, note_for(path, st, txt)))

print()
for i in range(3):
    st, txt = call("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
    print("空model 第%d次  %-4s %s" % (i + 1, st, note_for("/v1/chat/completions", st, txt)))


def stream_check(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        n, first, done = 0, "", None
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            n += 1
            if not first:
                first = line[:70]
            if "[DONE]" in line:
                done = line
    return n, bool(done), first


print()
for path, body in [("/v1/chat/completions",
                    {"messages": [{"role": "user", "content": "hi"}], "stream": True}),
                   ("/v1/responses", {"input": "hi", "stream": True})]:
    n, done, first = stream_check(path, body)
    print("%-22s frames=%-4d done=%-4s %s" % (path, n, "有" if done else "无", first))
