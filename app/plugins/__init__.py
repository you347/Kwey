"""Kwey 插件系统（C3 / P2）：vault 内插件扫描、白名单校验与启用状态持久化。

受信插件模型（非沙箱）：插件经前端 Blob URL 动态 import，运行于主页面 JS 上下文；
能力面由 ``permissions`` 白名单约束（详见 static/js/plugins.js 的能力注入）。
"""
