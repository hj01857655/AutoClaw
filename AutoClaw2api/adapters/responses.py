# -*- coding: utf-8 -*-
"""adapters/responses.py — OpenAI Responses ⇄ Chat Completions 翻译。

Codex CLI、openai-agents SDK 等新客户端只说 Responses 协议；上游只讲 Chat。
本模块做双向翻译：``responses_to_chat``（请求）、``chat_json_to_responses``
（非流式）、``chat_sse_to_responses``（流式事件序列）。

**本模块是 406 的第二高发地**：Responses 的 ``max_output_tokens`` 与
Anthropic 的 ``max_tokens`` 一样是调用方常填字段，上游一见就 406 空响应。
出口处必须删掉；lanes 层也会删，双保险。

与 adapters/anthropic.py 的分工：那份把 Messages 的 block 数组摊平成 Chat，
这份把 Responses 的 item 数组摊平。两边最终都汇入同一条 Chat 管线。
"""
import json
import time
import uuid

# 出口处删除：Responses 的 token 上限字段，上游见即 406
_STRIP_TOKEN_LIMITS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def error_body(err_type: str, message: str) -> dict:
    """Responses 的错误结构（与 Chat 的 error 对象形状不同：外层有 type）。"""
    return {"error": {"type": err_type, "code": None, "message": message}}


# ── 请求：Responses → Chat ─────────────────────────────────────────
def _content_to_text(content) -> str:
    """content 可能是 str 或 block/item 数组，取纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("input_text", "output_text", "text",
                                         "summary_text", "refusal"):
                    parts.append(block.get("text") or "")
        return "\n".join(p for p in parts if p)
    return ""


def _convert_tools(tools) -> list:
    """Responses tools（扁平 function）→ Chat tools（嵌套 function）。

    Responses 的形态是 ``{"type":"function","name":..,"parameters":..}``，
    Chat 要 ``{"type":"function","function":{"name":..,"parameters":..}}``。
    """
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") not in (None, "function"):
            continue                      # 内置工具（web_search 等）上游不认
        out.append({
            "type": "function",
            "function": {
                "name": tool.get("name") or "",
                "description": tool.get("description") or "",
                "parameters": tool.get("parameters") or tool.get("input_schema")
                              or {"type": "object", "properties": {}},
            },
        })
    return out


def _convert_tool_choice(choice):
    if not choice:
        return None
    if isinstance(choice, str):
        return {"auto": "auto", "required": "required", "none": "none"}.get(choice)
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "function":
            return {"type": "function",
                    "function": {"name": choice.get("name") or ""}}
        return {"auto": "auto", "required": "required"}.get(kind)
    return None


def _input_to_messages(inp, out: list):
    """``input``（str 或 item 数组）→ Chat messages，追加到 ``out``。

    item 的 type 决定了角色：message / function_call / function_call_output /
    reasoning。前两者映射直白，后两者要跟 tool_calls 配对。
    """
    if isinstance(inp, str):
        out.append({"role": "user", "content": inp})
        return

    pending_calls = []      # 攒 assistant 的 function_call，遇 output 时落地
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")

        if itype in (None, "message"):
            role = item.get("role") or "user"
            if role not in ("user", "assistant", "system", "developer"):
                role = "user"
            if role == "developer":
                role = "system"
            content = item.get("content")
            if isinstance(content, list):
                # 多段 content：文本与图片混排
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype in ("input_text", "output_text", "text"):
                        parts.append({"type": "text", "text": block.get("text") or ""})
                    elif btype == "input_image":
                        url = block.get("image_url") or block.get("url") or ""
                        parts.append({"type": "image_url", "image_url": {"url": url}})
                out.append({"role": role, "content": parts or _content_to_text(content)})
            else:
                out.append({"role": role, "content": _content_to_text(content)})

        elif itype == "function_call":
            pending_calls.append({
                "id": item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex[:20]),
                "type": "function",
                "function": {"name": item.get("name") or "",
                             "arguments": item.get("arguments") or "{}"},
            })

        elif itype == "function_call_output":
            # function_call_output 必须作为独立的 tool 消息，不能和文本混
            out.append({"role": "tool",
                        "tool_call_id": item.get("call_id") or "",
                        "content": _content_to_text(item.get("output"))})

        elif itype == "reasoning":
            continue                      # 推理链不回灌，上游自己会重新想

        # 其它 item 类型（local_shell_call 等）上游不认，丢弃

    if pending_calls:
        # 悬空的 function_call：补一条 assistant 消息承载，否则 tool 配对断裂
        out.append({"role": "assistant", "content": None, "tool_calls": pending_calls})


def responses_to_chat(body: dict) -> dict:
    """Responses ``/v1/responses`` 请求体 → Chat Completions 请求体。"""
    messages = []

    instructions = body.get("instructions")
    if instructions:
        text = _content_to_text(instructions)
        if text:
            messages.append({"role": "system", "content": text})

    _input_to_messages(body.get("input"), messages)

    chat = {
        "model": body.get("model") or "",
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(src) is not None:
            chat[dst] = body[src]
    tools = _convert_tools(body.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _convert_tool_choice(body.get("tool_choice"))
    if choice:
        chat["tool_choice"] = choice

    # 出口处删 token 上限：Responses 的 max_output_tokens，上游见即 406
    for field in _STRIP_TOKEN_LIMITS:
        chat.pop(field, None)
    return chat


# ── 响应：Chat → Responses ─────────────────────────────────────────
def _resp_id(chat_id: str) -> str:
    return "resp_" + (chat_id or uuid.uuid4().hex)


def chat_json_to_responses(result: dict, model: str) -> dict:
    """聚合后的 Chat completion JSON → Responses JSON。"""
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = []

    if message.get("reasoning_content"):
        output.append({"type": "reasoning",
                       "summary": [{"type": "summary_text",
                                    "text": message["reasoning_content"]}],
                       "content": []})
    if message.get("content"):
        output.append({"type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text",
                                    "text": message["content"],
                                    "annotations": []}]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append({"type": "function_call", "status": "completed",
                       "call_id": call.get("id") or "",
                       "name": fn.get("name") or "",
                       "arguments": fn.get("arguments") or "{}"})
    if not output:
        output.append({"type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "",
                                    "annotations": []}]})

    usage_raw = result.get("usage") or {}
    finish = choice.get("finish_reason")
    return {
        "id": _resp_id(result.get("id")),
        "object": "response",
        "created_at": result.get("created") or int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "output_text": message.get("content") or "",
        "usage": {
            "input_tokens": usage_raw.get("prompt_tokens") or 0,
            "output_tokens": usage_raw.get("completion_tokens") or 0,
            "total_tokens": usage_raw.get("total_tokens") or 0,
            "output_tokens_details": {
                "reasoning_tokens": _reasoning_tokens(usage_raw)},
        },
        "finish_reason": finish or "stop",
        "error": None,
    }


def _reasoning_tokens(usage_raw: dict) -> int:
    details = usage_raw.get("completion_tokens_details")
    if isinstance(details, dict):
        return details.get("reasoning_tokens") or 0
    return 0


# ── 流式：Chat SSE 行 → Responses SSE ─────────────────────────────
def chat_sse_to_responses(lines, model: str):
    """把标准 Chat SSE 行（带 data: 前缀）转成 Responses 事件序列。

    输入是 ``_chat_sse_lines`` 的产出。事件顺序：
    response.created → response.output_item.added → content_part.added →
    output_text.delta* → output_text.done → content_part.done →
    output_item.done → response.completed
    """
    def frame(event: str, data: dict, seq):
        return ("event: %s\ndata: %s\n\n" % (event, json.dumps(
            data, ensure_ascii=False, separators=(",", ":")))).encode("utf-8"), seq + 1

    resp_id = "resp_" + uuid.uuid4().hex[:24]
    text_idx = 0
    seq = 0
    started = False
    text = []
    reasoning = []
    tool_calls = {}
    tool_order = []
    usage = {}
    finish_reason = "stop"

    def emit(event, data):
        nonlocal seq
        piece, seq = frame(event, data, seq)
        return piece

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

        if chunk.get("usage"):
            usage = chunk["usage"]

        if not started:
            started = True
            yield emit("response.created", {
                "type": "response.created",
                "response": {"id": resp_id, "object": "response",
                             "status": "in_progress", "model": model, "output": []},
            })
            yield emit("response.output_item.added", {
                "type": "response.output_item.added", "output_index": 0,
                "item": {"type": "message", "role": "assistant", "status": "in_progress",
                         "content": []},
            })
            yield emit("response.content_part.added", {
                "type": "response.content_part.added", "item_id": resp_id,
                "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}

            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
                yield emit("response.reasoning_summary_text.delta", {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": resp_id, "output_index": 0, "summary_index": 0,
                    "delta": delta["reasoning_content"],
                })

            if delta.get("content"):
                text.append(delta["content"])
                yield emit("response.output_text.delta", {
                    "type": "response.output_text.delta", "item_id": resp_id,
                    "output_index": 0, "content_index": 0,
                    "delta": delta["content"],
                })

            for call in delta.get("tool_calls") or []:
                key = call.get("index", 0)
                fn = call.get("function") or {}
                if key not in tool_calls:
                    tool_idx = len(tool_order) + 1
                    tool_calls[key] = {"id": call.get("id") or "",
                                       "name": fn.get("name") or "",
                                       "args": []}
                    tool_order.append(key)
                    yield emit("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": tool_idx,
                        "item": {"type": "function_call", "status": "in_progress",
                                 "call_id": tool_calls[key]["id"],
                                 "name": tool_calls[key]["name"], "arguments": ""},
                    })
                if fn.get("arguments"):
                    tool_calls[key]["args"].append(fn["arguments"])
                    yield emit("response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": resp_id, "output_index": len(tool_order),
                        "delta": fn["arguments"],
                    })

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    if not started:
        started = True
        yield emit("response.created", {
            "type": "response.created",
            "response": {"id": resp_id, "object": "response",
                         "status": "in_progress", "model": model, "output": []},
        })
        yield emit("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0,
            "item": {"type": "message", "role": "assistant", "status": "in_progress",
                     "content": []},
        })
        yield emit("response.content_part.added", {
            "type": "response.content_part.added", "item_id": resp_id,
            "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

    if reasoning:
        joined = "".join(reasoning)
        yield emit("response.reasoning_summary_text.done", {
            "type": "response.reasoning_summary_text.done", "item_id": resp_id,
            "output_index": 0, "summary_index": 0, "text": joined,
        })
        yield emit("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0,
            "item": {"type": "reasoning", "status": "completed",
                     "summary": [{"type": "summary_text", "text": joined}],
                     "content": []},
        })

    joined_text = "".join(text)
    yield emit("response.output_text.done", {
        "type": "response.output_text.done", "item_id": resp_id,
        "output_index": 0, "content_index": 0, "text": joined_text,
    })
    yield emit("response.content_part.done", {
        "type": "response.content_part.done", "item_id": resp_id,
        "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": joined_text, "annotations": []},
    })
    yield emit("response.output_item.done", {
        "type": "response.output_item.done", "output_index": 0,
        "item": {"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": joined_text,
                              "annotations": []}]},
    })

    for offset, key in enumerate(tool_order):
        call = tool_calls[key]
        args = "".join(call["args"])
        yield emit("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done", "item_id": resp_id,
            "output_index": offset + 1, "arguments": args,
        })
        yield emit("response.output_item.done", {
            "type": "response.output_item.done", "output_index": offset + 1,
            "item": {"type": "function_call", "status": "completed",
                     "call_id": call["id"], "name": call["name"], "arguments": args},
        })

    yield emit("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id, "object": "response", "created_at": int(time.time()),
            "status": "completed", "model": model,
            "output": _final_output(joined_text, reasoning, tool_calls, tool_order),
            "output_text": joined_text,
            "usage": {
                "input_tokens": usage.get("prompt_tokens") or 0,
                "output_tokens": usage.get("completion_tokens") or 0,
                "total_tokens": usage.get("total_tokens") or 0,
                "output_tokens_details": {"reasoning_tokens": _reasoning_tokens(usage)},
            },
            "finish_reason": finish_reason,
            "error": None,
        },
    })


def _final_output(text: str, reasoning: list, tool_calls: dict, tool_order: list) -> list:
    out = []
    if reasoning:
        out.append({"type": "reasoning", "status": "completed",
                    "summary": [{"type": "summary_text", "text": "".join(reasoning)}],
                    "content": []})
    out.append({"type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for key in tool_order:
        call = tool_calls[key]
        out.append({"type": "function_call", "status": "completed",
                    "call_id": call["id"], "name": call["name"],
                    "arguments": "".join(call["args"])})
    return out
