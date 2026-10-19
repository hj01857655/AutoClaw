# -*- coding: utf-8 -*-
"""routes/chat.py — OpenAI Chat Completions 协议路由（/v1/chat/completions）。

上游支持流式与非流式（``stream`` 由请求体决定）。流式侧复用 server 的
``_chat_sse_lines`` 产出标准 Chat SSE（含 [DONE] 收尾帧）。

本层只管协议形状；上游发送与重试（_open_upstream）、上游错误归一
（_upstream_error）、鉴权闸门（_guard）、请求日志/落盘（_stream_log）
都在 server.py。
"""
import json
import uuid

from flask import Response, g, jsonify, request


def register(app, core):
    """挂载 chat 路由。core = server 模块：_cors / _guard / _open_upstream /
    _upstream_error / _chat_sse_lines / _stream_log 与 autoclaw 通道模块
    一律在请求期经 core 动态查找——测试对 server 上这些名字的重绑定因此
    对路由层同样生效。"""
    logger = core.logger

    @app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
    def chat_completions():
        if request.method == "OPTIONS":
            return core._cors(Response(""))
        denied = core._guard()
        if denied:
            return denied

        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body.get("messages"):
            return core._cors(jsonify({"error": {
                "message": "invalid request: 'messages' is required",
                "type": "invalid_request_error",
            }})), 400

        want_stream = bool(body.get("stream"))
        want_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        chunk_id = "chatcmpl-" + uuid.uuid4().hex[:24]

        try:
            upstream = core._open_upstream(body)
        except Exception as error:
            return core._cors(jsonify({"error": {
                "message": str(error), "type": "upstream_error"}})), 502

        if upstream.status_code != 200:
            with upstream:
                status_body, status_code = core._upstream_error(upstream)
            return core._cors(status_body), status_code

        model = body.get("model") or core.DEFAULT_MODEL

        if not want_stream:
            with upstream:
                result = core.aggregate(upstream, model, chunk_id)
            return core._cors(Response(
                json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                status=200,
                content_type="application/json; charset=utf-8",
            ))

        def generate():
            try:
                yield from core._chat_sse_lines(upstream, model, chunk_id, want_usage)
            finally:
                upstream.close()

        resp = Response(core._stream_log(generate(), g.rid, g.t0), status=200,
                        mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["X-Accel-Buffering"] = "no"
        return core._cors(resp)
