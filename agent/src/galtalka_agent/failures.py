"""원문 파일이나 비밀값을 포함하지 않는 작업 실패 보고."""
from galtalka_agent.config import AgentError


class JobFailure(AgentError):
    def __init__(self, code, message):
        """로컬 핸들러가 지정한 오류 코드와 안전한 안내만 보관한다."""
        self.code = code
        super().__init__(message)
