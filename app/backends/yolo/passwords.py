# YOLO rules: Accounts/LogonPass/reset password all require alphanumeric, min 6 chars.
# The existing memorable generator (word + digits) satisfies this.
from app.backends.gamevault.passwords import generate_memorable_password  # noqa: F401
