# -*- coding: utf-8 -*-
"""lanes — 上游通道层：AutoClaw 凭据解密 + 请求整形 + SSE 解析。

对应 WorkBuddy2api 的 ``lanes/``。本包只关心「怎么跟 AutoClaw 云端说话」，
不关心 OpenAI/Anthropic 协议（那在 adapters/ 与 routes/）。
"""
from lanes import autoclaw  # noqa: F401

__all__ = ["autoclaw"]
