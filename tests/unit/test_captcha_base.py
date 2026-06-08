from app.captcha.base import CaptchaSolver


class _Solver:
    async def solve_numeric_image(self, image: bytes) -> str:
        return "12345"


def test_protocol_is_satisfied_by_a_class_with_solve_numeric_image():
    # Protocol conformance is structural; a class with the right async method qualifies.
    s: CaptchaSolver = _Solver()
    assert hasattr(s, "solve_numeric_image")
