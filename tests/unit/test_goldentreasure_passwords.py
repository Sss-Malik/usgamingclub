import re

from app.backends.goldentreasure.passwords import generate_memorable_password


def test_password_satisfies_goldentreasure_rule_letters_and_digits_6_to_16():
    # Golden Treasure rule: 6-16 chars, must combine letters and numbers.
    # The re-exported generator yields "{Word}{4 digits}" (e.g. "Tiger4827") -> 9-11 chars,
    # always has both letters and digits.
    for _ in range(50):
        pw = generate_memorable_password()
        assert 6 <= len(pw) <= 16
        assert any(c.isalpha() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert re.fullmatch(r"[A-Z][a-z]+\d{4}", pw), pw


def test_passwords_vary():
    assert len({generate_memorable_password() for _ in range(20)}) > 1
