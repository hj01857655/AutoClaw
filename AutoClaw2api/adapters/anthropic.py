# -*- coding: utf-8 -*-
"""adapters/anthropic.py — Anthropic Messages ⇄ Chat Completions 翻译。

Claude Code 等原生 Anthropic 客户端只说 Messages 协议；上游只讲 Chat。
本模块做双向翻译：请求体 → Chat 体、Chat SSE 行 → Anthropic SSE 行、
聚合结果 → Anthropic JSON。

**本模块是 406 的高发地**：Anthropic 协议里 ``max_tokens`` 是必填字段，
翻译结果必然带它，而上游一见就 406。所以 ``anthropic_to_chat`` 出口处
必须删掉，不能指望 lanes 层兜底（虽然 lanes 也会删，双保险）。
"""
import json
import time
import uuid

from lanes.autoclaw import BANNER_2X

_STRIP = ("max_tokens", "max_completion_tokens")


def error_body(err_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": err_type, "message": message}}


# ── 请求：Anthropic → Chat ─────────────────────────────────────────
def _content_to_text(content) -> str:
    """content 可能是 str 或 block 数组，取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def _image_block_to_part(block: dict) -> dict:
    """Anthropic image block → OpenAI image_url part（支持 base64 与 url）。"""
    source = block.get("source") or {}
    if source.get("type") == "base64":
        url = "data:%s;base64,%s" % (source.get("media_type") or "image/png",
                                     source.get("data") or "")
    else:
        url = source.get("url") or ""
    return {"type": "image_url", "image_url": {"url": url}}


def _convert_tools(tools) -> list:
    """Anthropic tools → OpenAI tools。"""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        schema = tool.get("input_schema") or {"type": "object", "properties": {}}
        out.append({
            "type": "function",
            "function": {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": schema,
            },
        })
    return out


def _convert_tool_choice(choice):
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function",
                "function": {"name": choice.get("name") or ""}}
    return None


def anthropic_to_chat(body: dict) -> dict:
    """Anthropic /v1/messages 请求体 → Chat Completions 请求体。"""
    messages = []

    # system：Anthropic 允许 str 或 block 数组，且与 messages 分开
    system = body.get("system")
    if system:
        text = _content_to_text(system)
        if text:
            messages.append({"role": "system", "content": text})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        texts, images, tool_calls, tool_results = [], [], [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text") or "")
            elif btype == "image":
                images.append(_image_block_to_part(block))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or ("call_" + uuid.uuid4().hex[:20]),
                    "type": "function",
                    "function": {
                        "name": block.get("name") or "",
                        "arguments": json.dumps(block.get("input") or {},
                                                ensure_ascii=False),
                    },
                })
            elif btype == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": _content_to_text(block.get("content")),
                })

        # tool_result 必须紧跟在自己那条消息里，不能和文本混在一条
        for result in tool_results:
            messages.append(result)
        if tool_calls:
            messages.append({"role": "assistant", "content": "\n".join(texts) or None,
                             "tool_calls": tool_calls})
        elif texts or images:
            if images:
                parts = [{"type": "text", "text": t} for t in texts if t]
                parts.extend(images)
                messages.append({"role": role, "content": parts})
            else:
                messages.append({"role": role, "content": "\n".join(texts)})

    chat = {
        "model": body.get("model") or "",
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            chat[dst] = body[src]
    if body.get("stop_sequences"):
        chat["stop"] = body["stop_sequences"]
    tools = _convert_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _convert_tool_choice(body.get("tool_choice"))
    if choice:
        chat["tool_choice"] = choice

    # 出口处删 max_tokens：Anthropic 必填，上游见即 406
    for field in _STRIP:
        chat.pop(field, None)
    return chat


# ── 响应：Chat → Anthropic ─────────────────────────────────────────
def chat_json_to_anthropic(result: dict, model: str) -> dict:
    """聚合后的 Chat completion JSON → Anthropic message JSON。"""
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []

    if message.get("reasoning_content"):
        # Anthropic 的 thinking block；签名缺失时给空串，客户端据 type 处理
        content.append({"type": "thinking", "thinking": message["reasoning_content"],
                        "signature": ""})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        content.append({"type": "tool_use", "id": call.get("id") or "",
                        "name": fn.get("name") or "", "input": args})
    if not content:
        content.append({"type": "text", "text": ""})

    usage_raw = result.get("usage") or {}
    usage = {
        "input_tokens": usage_raw.get("prompt_tokens") or 0,
        "output_tokens": usage_raw.get("completion_tokens") or 0,
    }
    finish = choice.get("finish_reason")
    stop_reason = {"stop": "end_turn", "length": "max_tokens",
                   "tool_calls": "tool_use", "content_filter": "refusal"}.get(finish, "end_turn")

    return {
        "id": "msg_" + uuid.uuid4().hex[:24],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


# ── 流式：Chat SSE 行 → Anthropic SSE ─────────────────────────────
def chat_sse_to_anthropic(lines, model: str):
    """把标准 Chat SSE 行（带 data: 前缀）转成 Anthropic 事件流。

    输入是 ``_chat_sse_lines`` 的产出；这里只做协议外皮，不改内容。
    事件序列：message_start → content_block_start/delta/stop → message_delta → message_stop
    """
    def frame(event: str, data: dict):
        return ("event: %s\ndata: %s\n\n" % (event, json.dumps(
            data, ensure_ascii=False, separators=(",", ":")))).encode("utf-8")

    msg_id = "msg_" + uuid.uuid4().hex[:24]
    text_idx, think_idx, tool_idx = 0, None, None
    open_blocks = []           # [(index, kind, tool_id)]
    started = False
    usage_out = {}
    text_open = False
    tool_calls_seen = {}

    for raw in lines:
        if not raw.startswith(b"data: "):
            continue
        data = raw[6:].strip()
        if data == b"[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue

        if not started:
            started = True
            yield frame("message_start", {
                "type": "message_start",
                "message": {"id": msg_id, "type": "message", "role": "assistant",
                            "model": model, "content": [], "stop_reason": None,
                            "stop_sequence": None, "usage": {"input_tokens": 0,
                                                             "output_tokens": 0}},
            })

        if chunk.get("usage"):
            usage_out = chunk["usage"]

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}

            if delta.get("reasoning_content"):
                if think_idx is None:
                    think_idx = len(open_blocks)
                    open_blocks.append((think_idx, "thinking", None))
                    yield frame("content_block_start", {
                        "type": "content_block_start", "index": think_idx,
                        "content_block": {"type": "thinking", "thinking": ""},
                    })
                yield frame("content_block_delta", {
                    "type": "content_block_delta", "index": think_idx,
                    "delta": {"type": "thinking_delta",
                              "thinking": delta["reasoning_content"]},
                })

            if delta.get("content"):
                if not text_open:
                    if think_idx is not None:
                        yield frame("content_block_stop", {
                            "type": "content_block_stop", "index": think_idx})
                        think_idx = None
                    text_idx = len(open_blocks)
                    open_blocks.append((text_idx, "text", None))
                    text_open = True
                    yield frame("content_block_start", {
                        "type": "content_block_start", "index": text_idx,
                        "content_block": {"type": "text", "text": ""},
                    })
                yield frame("content_block_delta", {
                    "type": "content_block_delta", "index": text_idx,
                    "delta": {"type": "text_delta", "text": delta["content"]},
                })

            for call in delta.get("tool_calls") or []:
                key = call.get("index", 0)
                fn = call.get("function") or {}
                if key not in tool_calls_seen:
                    if not text_open and think_idx is not None:
                        yield frame("content_block_stop", {
                            "type": "content_block_stop", "index": think_idx})
                        think_idx = None
                    elif text_open:
                        yield frame("content_block_stop", {
                            "type": "content_block_stop", "index": text_idx})
                        text_open = False
                    idx = len(open_blocks)
                    tool_idx = idx
                    open_blocks.append((idx, "tool_use", call.get("id") or ""))
                    tool_calls_seen[key] = idx
                    yield frame("content_block_start", {
                        "type": "content_block_start", "index": idx,
                        "content_block": {"type": "tool_use",
                                          "id": call.get("id") or "",
                                          "name": fn.get("name") or "",
                                          "input": {}},
                    })
                idx = tool_calls_seen[key]
                if fn.get("arguments"):
                    yield frame("content_block_delta", {
                        "type": "content_block_delta", "index": idx,
                        "delta": {"type": "input_json_delta",
                                  "partial_json": fn["arguments"]},
                    })

            if choice.get("finish_reason"):
                for idx, kind, _ in open_blocks:
                    yield frame("content_block_stop",
                                {"type": "content_block_stop", "index": idx})
                open_blocks = []

    if not started:
        started = True
        yield frame("message_start", {
            "type": "message_start",
            "message": {"id": msg_id, "type": "message", "role": "assistant",
                        "model": model, "content": [], "stop_reason": None,
                        "stop_sequence": None, "usage": {"input_tokens": 0,
                                                         "output_tokens": 0}},
        })
        text_idx = 0
        open_blocks.append((0, "text", None))
        yield frame("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""}})

    for idx, kind, _ in open_blocks:
        yield frame("content_block_stop", {"type": "content_block_stop", "index": idx})

    yield frame("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": (usage_out.get("completion_tokens")
                                    if usage_out else 0) or 0},
    })
    yield frame("message_stop", {"type": "message_stop"})
