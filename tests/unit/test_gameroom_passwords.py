import re

from app.backends.gameroom.passwords import (
    generate_memorable_complex_password,
    generate_memorable_password,
)


def test_complex_password_has_all_required_classes():
    for _ in range(50):
        pw = generate_memorable_complex_password()
        assert 6 <= len(pw) <= 12, pw
        assert any(c.isupper() for c in pw), pw
        assert any(c.islower() for c in pw), pw
        assert any(c.isdigit() for c in pw), pw
        assert any(c in "!@#$%&*" for c in pw), pw
        assert " " not in pw


def test_complex_password_format_word_symbol_digits():
    pw = generate_memorable_complex_password()
    assert re.fullmatch(r"[A-Z][a-z]+[!@#$%&*]\d{2}", pw), pw


def test_complex_password_varies():
    assert len({generate_memorable_complex_password() for _ in range(20)}) > 1


def test_memorable_alphanumeric_password_is_re_exported():
    # CREATE_ACCOUNT uses the existing GameVault generator; gameroom re-exports for clarity.
    pw = generate_memorable_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw
