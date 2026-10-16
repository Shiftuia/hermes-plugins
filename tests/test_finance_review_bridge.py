"""Import/register smoke tests; never connect to Discord or touch live state."""

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml


@pytest.fixture
def plugin(monkeypatch):
    name = "finance_review_bridge_smoke"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "finance-review-bridge" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(f"{name}.bridge", None)


def test_manifest_and_package_register(plugin):
    manifest = yaml.safe_load((Path(__file__).parents[1] / "finance-review-bridge" / "plugin.yaml").read_text())
    assert manifest["name"] == "finance-review-bridge"
    settings = {key: str(10**17 + offset) for offset, key in enumerate(
        ("bot_id", "actor_id", "guild_id", "channel_id"))}
    ctx = Mock()
    ctx.get_config.side_effect = lambda key, default=None: settings.get(key, default)
    plugin.register(ctx)
    ctx.register_platform_handler.assert_called_once()
    platform, factory = ctx.register_platform_handler.call_args.args
    assert platform == "discord"
    assert callable(factory)


def test_environment_configuration(plugin, monkeypatch):
    for offset, key in enumerate(("BOT_ID", "ACTOR_ID", "GUILD_ID", "CHANNEL_ID")):
        monkeypatch.setenv(f"FINANCE_REVIEW_BRIDGE_{key}", str(10**17 + offset))
    ctx = Mock()
    ctx.get_config.side_effect = lambda key, default=None: default
    plugin.register(ctx)
    ctx.register_platform_handler.assert_called_once()


def test_configured_scope_reaches_bridge_on_ready(plugin, monkeypatch, tmp_path):
    settings = {key: 10**17 + offset for offset, key in enumerate(
        ("bot_id", "actor_id", "guild_id", "channel_id"))}
    ctx = Mock()
    ctx.get_config.side_effect = lambda key, default=None: settings.get(key, default)
    plugin.register(ctx)
    factory = ctx.register_platform_handler.call_args.args[1]
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(
        get_hermes_home=lambda: tmp_path))
    monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(
        Embed=SimpleNamespace(from_dict=lambda value: value)))
    bridge_type = Mock(return_value=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()))
    monkeypatch.setattr(sys.modules[f"{plugin.__name__}.bridge"], "Bridge", bridge_type)
    native = Mock(user=SimpleNamespace(id=settings["bot_id"]))
    native.is_ready.return_value = False
    factory(native, Mock())
    ready = native.add_listener.call_args_list[0].args[0]
    asyncio.run(ready())
    assert bridge_type.call_args.kwargs == settings
    bridge_type.return_value.start.assert_awaited_once()


@pytest.mark.parametrize("value", [None, "*", "", True, 123, "123,456"])
def test_invalid_scope_fails_before_handler_registration(plugin, value):
    ctx = Mock()
    ctx.get_config.return_value = value
    with pytest.raises(ValueError, match="configure bot_id"):
        plugin.register(ctx)
    ctx.register_platform_handler.assert_not_called()
