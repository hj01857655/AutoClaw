# -*- coding: utf-8 -*-
"""routes/anthropic.py — Anthropic Messages 协议路由（/v1/messages）。

Claude Code 等原生 Anthropic 客户端只说 Messages 协议，上游只出 Chat。
adapters.anthropic 把 Anthropic ⇄ Chat 翻掉，复用同一条上游管线。

**注意 max_tokens**：Anthropic 协议必填该字段，而上游一见就 406。翻译层
（adapters/anthropic.py 出口）会删掉它，这里不额外处理——但若上游行为
变更，排查 406 时第一个该看的就是它还在不在请求体里。

本层只管协议形状；上游发送与重试、上游错误归一、鉴权闸门、请求日志/落盘
都在 server.py。
"""
import json
import uuid

from flask import Response, g, jsonify, request

from adapters import anthropic as ab


def register(app, core):
    """挂载 anthropic 路由。core = server 模块：_cors / _guard /
    _open_upstream / _upstream_error / _chat_sse_lines / _stream_log 与
    autoclaw 通道模块一律在请求期经 core 动态查找。"""
    logger = core.logger

    @app.route("/v1/messages", methods=["POST", "OPTIONS"])
    def messages_api():
        if request.method == "OPTIONS":
            return core._cors(Response(""))
        denied = core._guard()
        if denied:
            return denied

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return core._cors(jsonify(ab.error_body(
                "invalid_request_error", "invalid request: JSON object body required"))), 400

        chat_body = ab.anthropic_to_chat(body)
        if not chat_body.get("messages"):
            return core._cors(jsonify(ab.error_body(
                "invalid_request_error", "invalid request: 'messages' is required"))), 400

        want_stream = bool(chat_body.get("stream"))
        chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]

        try:
            upstream = core._open_upstream(chat_body)
        except Exception as error:
            return core._cors(jsonify(ab.error_body("api_error", str(error)))), 502

        if upstream.status_code != 200:
            with upstream:
                status_body, status_code = core._upstream_error(upstream)
            return core._cors(status_body), status_code

        model = chat_body.get("model") or core.DEFAULT_MODEL

        if not want_stream:
            with upstream:
                result = core.aggregate(upstream, model, chunk_id)
            return core._cors(Response(
                json.dumps(ab.chat_json_to_anthropic(result, model), ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8"),
                status=200,
                content_type="application/json; charset=utf-8",
            ))

        def generate():
            try:
                for frame in ab.chat_sse_to_anthropic(
                        core._chat_sse_lines(upstream, model, chunk_id, False), model):
                    yield frame
            finally:
                upstream.close()

        resp = Response(core._stream_log(generate(), g.rid, g.t0), status=200,
                        mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return core._cors(resp)
