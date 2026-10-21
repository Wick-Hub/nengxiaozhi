"""kb-service —— 能小智校园 AI 助手平台 Python 服务层。

模块分层：
    app.api        REST + SSE 接口
    app.core       解析 / 切片 / 嵌入 / 检索 / 精排 / 生成
    app.db         数据访问与向量适配层
    app.models     请求响应模型
    app.security   鉴权 / 权限 / 加密 / 脱敏 / 限流
"""

__version__ = "0.1.0"
