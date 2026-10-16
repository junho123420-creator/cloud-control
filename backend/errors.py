"""앱 공통 오류. main.py가 {detail:{code,message}} 형식으로 응답한다."""


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
