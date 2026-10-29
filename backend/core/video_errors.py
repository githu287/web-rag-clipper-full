"""Public, credential-free errors shared by video adapters, repository and API."""


class VideoError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
