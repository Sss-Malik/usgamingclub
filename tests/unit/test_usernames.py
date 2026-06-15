import re

from app.backends.usernames import generate_username


def test_derives_from_full_name_alnum_lowercase():
    u = generate_username("John O'Brien")
    assert re.fullmatch(r"[a-z]+[0-9]{4}", u)
    assert u.startswith("johnobrien")


def test_truncates_long_names():
    u = generate_username("A" * 50)
    # base capped at 12 chars + 4 digits
    assert len(u) <= 16
    assert re.fullmatch(r"a{1,12}[0-9]{4}", u)


def test_falls_back_when_no_alnum():
    u = generate_username("***")
    assert re.fullmatch(r"user[0-9]{4}", u)


def test_is_nondeterministic_suffix():
    assert generate_username("Jane Doe") != generate_username("Jane Doe")
