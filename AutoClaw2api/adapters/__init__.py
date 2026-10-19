# -*- coding: utf-8 -*-
"""adapters — 协议翻译层：把各家协议的请求/响应翻成上游的母语 Chat Completions。

上游只讲 Chat（母语）。AutoClaw2api 提供三个入口：

    anthropic.py   Anthropic Messages（Claude Code 等原生客户机的必需面）
    responses.py   OpenAI Responses（Codex CLI、agents SDK 的新面）
    chat.py        无需翻译——OpenAI Chat 就是上游母语

**406 高发地集中在翻译层**：各家的 token 上限字段名不同
（Anthropic ``max_tokens``、Responses ``max_output_tokens``），都是调用方常填
字段，而上游一见就 406 空响应。删它这件事在各适配器出口与 lanes/autoclaw.py
各做一次，双保险。
"""

from adapters import anthropic  # noqa: F401  （`import adapters` 后可直接取用）
from adapters import responses  # noqa: F401

__all__ = ["anthropic", "responses"]
