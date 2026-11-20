"""Test harness for applications that call heliopuddle (CLI-14).

``FakeHelioPuddleClient`` is an in-memory drop-in for ``HelioPuddleClient``
that needs only the core (client) install. The pytest plugin in
:mod:`heliopuddle.pytest_plugin` (loaded automatically when
heliopuddle is installed) provides it as the ``heliopuddle_fake`` fixture::

    def test_publishes(heliopuddle_fake):
        my_app.publish(client=heliopuddle_fake)
        assert heliopuddle_fake.live_paths() == ["reports/a.csv"]
"""

from heliopuddle.testing.fake import Call, FakeHelioPuddleClient

__all__ = ["Call", "FakeHelioPuddleClient"]
