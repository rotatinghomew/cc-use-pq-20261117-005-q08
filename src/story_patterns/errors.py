class StoryPatternError(Exception):
    """产品可预期错误的基类。

    ``details`` 用于让 CLI 在错误 JSON 中携带可复核的稳定标识，
    例如批次标识与两个内容摘要。
    """

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


class ValidationError(StoryPatternError):
    """输入可解析为 JSON，但不符合故事记录约束。"""


class ConflictError(StoryPatternError):
    """稳定标识被重复用于不同的不可变内容。"""


class NotFoundError(StoryPatternError):
    """请求的本地记录不存在。"""


class AnalysisError(StoryPatternError):
    """分析请求无法按指定规则计算。"""


class BatchSourceConflictError(ConflictError):
    """同一批次标识再次提交了不同的来源内容。

    这不是幂等重试。异常同时携带已确认摘要与本次提交摘要，便于审计。
    """

    def __init__(self, batch_id: str, recorded_source_digest: str, submitted_source_digest: str):
        super().__init__(
            f"batch {batch_id} already records a different source submission",
            batch_id=batch_id,
            recorded_source_digest=recorded_source_digest,
            submitted_source_digest=submitted_source_digest,
            idempotent=False,
        )
