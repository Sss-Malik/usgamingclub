import re

from app.backends.ultrapanda.passwords import generate_vpower_password


def test_password_is_memorable_word_plus_digits():
    pw = generate_vpower_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw


def test_password_charset_alphanumeric_only():
    for _ in range(50):
        pw = generate_vpower_password()
        assert re.fullmatch(r"[A-Za-z0-9]+", pw), pw


def test_password_varies():
    assert len({generate_vpower_password() for _ in range(20)}) > 1
