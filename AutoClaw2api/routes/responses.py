# -*- coding: utf-8 -*-
"""routes/responses.py — OpenAI Responses 协议路由（/v1/responses）。

Codex CLI、openai-agents SDK 等新客户端只说 Responses 协议。
adapters.responses 把 Responses ⇄ Chat 翻掉，复用同一条上游管线。

**注意 max_output_tokens**：它是 Responses 里限制输出长度的字段，上游一见就
406。翻译层（adapters/responses.py 出口）会删掉它，这里不额外处理——但若上游
行为变更，排查 406 时第一个该看的就是它还在不在请求体里。

本层只管协议形状；上游发送与重试、上游错误归一、鉴权闸门、请求日志/落盘
都在 server.py。
"""
import json
import uuid

from flask import Response, g, jsonify, request

from adapters import responses as rb


def register(app, core):
    """挂载 responses 路由。core = server 模块：_cors / _guard /
    _open_upstream / _upstream_error / _chat_sse_lines / _stream_log。"""
    logger = core.logger

    @app.route("/v1/responses", methods=["POST", "OPTIONS"])
    def responses_api():
        if request.method == "OPTIONS":
            return core._cors(Response(""))
        denied = core._guard()
        if denied:
            return denied

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return core._cors(jsonify(rb.error_body(
                "invalid_request_error",
                "invalid request: JSON object body required"))), 400

        chat_body = rb.responses_to_chat(body)
        if not chat_body.get("messages"):
            return core._cors(jsonify(rb.error_body(
                "invalid_request_error", "invalid request: 'input' is required"))), 400

        want_stream = bool(chat_body.get("stream"))
        chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]

        try:
            upstream = core._open_upstream(chat_body)
        except Exception as error:
            return core._cors(jsonify(rb.error_body("api_error", str(error)))), 502

        if upstream.status_code != 200:
            with upstream:
                status_body, status_code = core._upstream_error(upstream)
            return core._cors(status_body), status_code

        model = chat_body.get("model") or core.DEFAULT_MODEL

        if not want_stream:
            with upstream:
                result = core.aggregate(upstream, model, chunk_id)
            return core._cors(Response(
                json.dumps(rb.chat_json_to_responses(result, model),
                           ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                status=200,
                content_type="application/json; charset=utf-8",
            ))

        def generate():
            try:
                for frame in rb.chat_sse_to_responses(
                        core._chat_sse_lines(upstream, model, chunk_id, False), model):
                    yield frame
            finally:
                upstream.close()

        resp = Response(core._stream_log(generate(), g.rid, g.t0), status=200,
                        mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return core._cors(resp)
