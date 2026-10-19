# -*- coding: utf-8 -*-
"""routes — 协议路由层：每协议一个文件，server.py 只做调度。

    chat.py        OpenAI Chat Completions  /v1/chat/completions
    anthropic.py   Anthropic Messages        /v1/messages
    responses.py   OpenAI Responses          /v1/responses

三者共享同一条上游管线（server._open_upstream → lanes.build_request），差别
只在协议翻译放在 adapters/。新增协议时加一个 routes/<name>.py 加一个
adapters/<name>.py，server.py 不改逻辑。

与 WorkBuddy2api 同构：``register(app, core)`` 里 core 传 server 模块，
路由经模块属性动态查找引擎函数，测试对 server 上这些名字的重绑定因此
对路由层同样生效。
"""
