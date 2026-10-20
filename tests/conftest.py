import os

import pytest

from search_router.config import Settings


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, request):
    if request.node.path.name == "test_live.py":
        return
    original = Settings.__init__

    def isolated(self, **values):
        values.setdefault("quota_cache_enabled", False)
        values.setdefault("quota_refresh_enabled", False)
        original(self, **values)

    monkeypatch.setattr(Settings, "__init__", isolated)
    for name in os.environ:
        if name.startswith("ROUTER_"):
            monkeypatch.delenv(name, raising=False)
