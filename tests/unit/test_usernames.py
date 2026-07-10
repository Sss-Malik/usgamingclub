import re

from app.backends.usernames import generate_username

# Contract: lowercase [a-z0-9] base (<=12) + 3 random digits, <=15 total.


def test_derives_from_full_name_alnum_lowercase():
    u = generate_username("John O'Brien")
    assert re.fullmatch(r"[a-z]+[0-9]{3}", u)
    assert u.startswith("johnobrien")


def test_truncates_long_names_to_15_total():
    u = generate_username("A" * 50)
    # base capped at 12 chars + 3 digits = 15 total
    assert len(u) <= 15
    assert re.fullmatch(r"a{12}[0-9]{3}", u)


def test_provided_username_wins_over_full_name():
    u = generate_username("Jane Doe", provided="cooljohn")
    assert u.startswith("cooljohn")
    assert re.fullmatch(r"cooljohn[0-9]{3}", u)


def test_provided_username_is_sanitized_and_capped():
    u = generate_username(provided="WAY-too-LONG-name!!")
    # lowercased, non-alphanumerics stripped ("waytoolongna"), truncated to 12, + 3 digits
    assert re.fullmatch(r"[a-z0-9]{1,12}[0-9]{3}", u)
    assert len(u) <= 15
    assert u.startswith("waytoolongna")


def test_falls_back_to_full_name_when_provided_is_empty_after_sanitize():
    u = generate_username("Jane Doe", provided="***")
    assert u.startswith("janedoe")


def test_falls_back_to_user_when_nothing_usable():
    assert re.fullmatch(r"user[0-9]{3}", generate_username("***", provided="!!!"))
    assert re.fullmatch(r"user[0-9]{3}", generate_username())


def test_is_nondeterministic_suffix():
    assert generate_username(provided="janedoe") != generate_username(provided="janedoe")
