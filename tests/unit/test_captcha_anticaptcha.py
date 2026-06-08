from unittest.mock import MagicMock, patch

import pytest

from app.backends.base import TransientBackendError
from app.captcha.anticaptcha import AntiCaptchaSolver


@pytest.fixture
def fake_solver_class():
    """Patch anticaptchaofficial.imagecaptcha.imagecaptcha with a MagicMock factory.

    The library's `imagecaptcha()` returns a stateful solver instance; we replace it
    so unit tests never touch the live AntiCaptcha service.
    """
    with patch("app.captcha.anticaptcha.imagecaptcha") as factory:
        instance = MagicMock()
        factory.return_value = instance
        yield factory, instance


async def test_solve_numeric_image_returns_solution_text(fake_solver_class):
    factory, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = "34596"
    solver = AntiCaptchaSolver(api_key="testkey")
    out = await solver.solve_numeric_image(b"\xff\xd8FAKE_JPEG")
    assert out == "34596"
    # The library was configured with our key + numeric-only mode + zero verbose
    instance.set_key.assert_called_once_with("testkey")
    instance.set_numeric.assert_called_once_with(2)
    instance.set_verbose.assert_called_once_with(0)


async def test_solve_numeric_image_raises_transient_on_error_code(fake_solver_class):
    _, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = 0
    instance.error_code = "ERROR_KEY_DOES_NOT_EXIST"
    solver = AntiCaptchaSolver(api_key="bad")
    with pytest.raises(TransientBackendError) as ei:
        await solver.solve_numeric_image(b"\xff\xd8FAKE")
    assert "anticaptcha:ERROR_KEY_DOES_NOT_EXIST" in str(ei.value)


async def test_solve_numeric_image_strips_whitespace(fake_solver_class):
    _, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = "  12345  \n"
    solver = AntiCaptchaSolver(api_key="k")
    out = await solver.solve_numeric_image(b"x")
    assert out == "12345"


async def test_solve_writes_then_removes_temp_file(fake_solver_class, tmp_path, monkeypatch):
    """The library accepts a file path; we write the bytes to a temp file and unlink in finally."""
    _, instance = fake_solver_class
    instance.solve_and_return_solution.return_value = "11111"

    written_paths: list[str] = []

    def fake_solve(path: str) -> str:
        written_paths.append(path)
        # File must exist while solver is reading it
        with open(path, "rb") as f:
            assert f.read() == b"PAYLOAD"
        return "11111"

    instance.solve_and_return_solution.side_effect = fake_solve
    solver = AntiCaptchaSolver(api_key="k")
    await solver.solve_numeric_image(b"PAYLOAD")
    # Tempfile was removed after solve
    assert written_paths and not any(__import__("os").path.exists(p) for p in written_paths)


async def test_solve_unlinks_tempfile_when_solver_raises(fake_solver_class):
    """If the upstream library raises mid-solve, the finally clause still cleans up the tempfile."""
    import os as _os
    _, instance = fake_solver_class
    captured: list[str] = []

    def boom(path: str):
        captured.append(path)
        raise RuntimeError("upstream boom")

    instance.solve_and_return_solution.side_effect = boom
    solver = AntiCaptchaSolver(api_key="k")
    with pytest.raises(RuntimeError, match="upstream boom"):
        await solver.solve_numeric_image(b"x")
    assert captured and not _os.path.exists(captured[0])
