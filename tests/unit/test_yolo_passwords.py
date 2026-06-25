from app.backends.yolo.passwords import generate_memorable_password


def test_password_is_alphanumeric_min6():
    for _ in range(20):
        pw = generate_memorable_password()
        assert len(pw) >= 6 and pw.isalnum()
