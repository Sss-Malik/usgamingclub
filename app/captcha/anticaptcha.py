import asyncio
import os
import tempfile

from anticaptchaofficial.imagecaptcha import imagecaptcha  # noqa: F401 (re-imported by tests)

from app.backends.base import TransientBackendError


class AntiCaptchaSolver:
    """Thin async wrapper over the official `anticaptchaofficial` image-captcha client.

    The upstream library is synchronous and file-path-based; we wrap each solve in
    `asyncio.to_thread` so it doesn't block the event loop. Configured for digit-only
    captchas (OrionStars/MilkyWay use a 5-digit numeric JPEG).
    """

    def __init__(self, *, api_key: str) -> None:
        if not api_key:
            raise ValueError("AntiCaptchaSolver requires a non-empty api_key")
        self._api_key = api_key

    async def solve_numeric_image(self, image: bytes) -> str:
        return await asyncio.to_thread(self._solve_sync, image)

    def _solve_sync(self, image: bytes) -> str:
        solver = imagecaptcha()
        solver.set_verbose(0)
        solver.set_key(self._api_key)
        solver.set_numeric(2)            # 2 = "only digits"
        # The library reads from a file path, not a bytes buffer. Write to a temp file
        # in the OS temp dir and unlink after the solve completes.
        fd, path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(image)
            result = solver.solve_and_return_solution(path)
            if result == 0:
                code = getattr(solver, "error_code", "unknown")
                raise TransientBackendError(f"anticaptcha:{code}")
            return str(result).strip()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
