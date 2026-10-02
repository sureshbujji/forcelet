"""Suite-wide fixtures for the Forcelet test suite."""

import pytest

from forcelet import security as _security


@pytest.fixture(autouse=True)
def _isolate_login_rate_limiter():
    """Clear the in-process brute-force tracker before each test.

    The tracker is module-global, so failed logins from one test (e.g. a
    negative-login test, or threads racing a password change) would otherwise
    trip the IP-wide lockout for every later test in the same worker process.
    Lockout tests still work: they perform all of their attempts inside a
    single test.
    """
    _security._LOGIN_ATTEMPTS.clear()
    yield
    _security._LOGIN_ATTEMPTS.clear()
