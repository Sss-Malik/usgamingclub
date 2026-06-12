from typing import Protocol


class CaptchaSolver(Protocol):
    """Abstract solver for image-based captchas.

    Implementations must accept raw image bytes (e.g. JPEG/PNG) and return the decoded text.
    Solver failures should be raised as `TransientBackendError` from `app.backends.base`.
    """

    async def solve_numeric_image(self, image: bytes) -> str: ...
