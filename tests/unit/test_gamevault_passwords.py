# tests/unit/test_gamevault_passwords.py
import re

from app.backends.gamevault.passwords import generate_memorable_password


def test_password_format_word_plus_digits():
    pw = generate_memorable_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw


def test_password_length_within_gamevault_bounds():
    for _ in range(50):
        pw = generate_memorable_password()
        assert 6 <= len(pw) <= 32
        assert pw.isalnum()


def test_passwords_vary():
    assert len({generate_memorable_password() for _ in range(20)}) > 1
