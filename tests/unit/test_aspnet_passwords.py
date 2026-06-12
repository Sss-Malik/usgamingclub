import re

from app.backends._aspnet_cashier.passwords import generate_aspnet_password


def test_password_charset_letters_digits_underscore_only():
    for _ in range(50):
        pw = generate_aspnet_password()
        assert re.fullmatch(r"[A-Za-z0-9_]+", pw), pw


def test_password_length_within_limits():
    for _ in range(50):
        pw = generate_aspnet_password()
        assert 1 <= len(pw) <= 32, pw


def test_password_varies():
    assert len({generate_aspnet_password() for _ in range(20)}) > 1


def test_password_is_memorable_word_plus_digits():
    # Format: capitalized word + 4 digits (e.g. "Tiger4783").
    pw = generate_aspnet_password()
    assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw
