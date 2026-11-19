"""Stand-ins for Redis in integration tests."""

import redis


class DownRedis:
    """A Redis that cannot be reached: every command, and opening a pipeline, fails with a connection error."""

    def pipeline(self, *args, **kwargs):
        raise redis.ConnectionError("Connection refused")

    def __getattr__(self, command):
        def _fail(*args, **kwargs):
            raise redis.ConnectionError("Connection refused")

        return _fail
