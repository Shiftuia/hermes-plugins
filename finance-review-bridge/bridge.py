import asyncio
import json
import os
import re
import socket
import sqlite3
import stat
import struct
from pathlib import Path
from typing import Any

CASE = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
SNOWFLAKE = re.compile(r"^[1-9]\d{16,19}$")
DECISION = re.compile(r"^(approve|ignore|pending) ([a-f0-9-]{36}) v([1-9]\d{0,8}) ([a-f0-9]{64})$")


def descriptor(value):
    if (not isinstance(value, dict) or set(value) != {"caseId", "version", "digest"}
            or not isinstance(value["caseId"], str) or not CASE.fullmatch(value["caseId"])
            or type(value["version"]) is not int or not 1 <= value["version"] <= 999999999
            or not isinstance(value["digest"], str) or not DIGEST.fullmatch(value["digest"])):
        raise ValueError("invalid case binding")
    return value


def rendered(value):
    d = descriptor(value)
    binding = f'{d["caseId"]} v{d["version"]} {d["digest"]}'
    return {
        "title": "SYNTHETIC TEST ONLY — finance review",
        "fields": [{"name": "#1", "value": f"Synthetic case {binding}\nNo bank data. No ledger writes."}],
        "footer": {"text": f"Reply: approve|ignore|pending {binding}. Synthetic decision only; never authorizes Actual writes."},
    }


def private_path(path, directory=False):
    path = Path(path).absolute()
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise ValueError("symlink state path")
    if directory:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("state must be owner-only")
    return path


def routing_snapshot(adapter):
    snapshot = getattr(adapter, "_gate_env_snapshot", None)
    if not isinstance(snapshot, dict) or not {"DISCORD_ALLOWED_CHANNELS", "DISCORD_FREE_RESPONSE_CHANNELS"} <= snapshot.keys():
        raise ValueError("connect-time routing snapshot unavailable")
    return {"allowedChannels": sorted(adapter._get_allowed_channels()),
            "freeResponseChannels": sorted(adapter._discord_free_response_channels())}


class Bridge:
    def __init__(self, native, root, embed_factory, object_factory, routing, *,
                 bot_id, actor_id, guild_id, channel_id):
        self.bot_id = bot_id
        self.actor_id = actor_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.native = native
        self.routing = routing
        self.root = private_path(root, directory=True)
        self.socket_path = self.root / "bridge.sock"
        self.embed_factory = embed_factory
        self.object_factory = object_factory
        state = self.root / "bridge.sqlite"
        if not state.exists():
            fd = os.open(state, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        private_path(state)
        self.db = sqlite3.connect(state)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS notices (nonce TEXT PRIMARY KEY, binding TEXT UNIQUE NOT NULL, message TEXT)")
        self.db.commit()
        self.lock = asyncio.Lock()
        self.server = None
        self.connections = asyncio.Semaphore(8)

    async def audit(self):
        if not self.native.is_ready() or self.native.is_closed() or self.native.user.id != self.bot_id:
            raise ValueError("wrong gateway identity or disconnected")
        channel = await self.native.fetch_channel(self.channel_id)
        if channel.guild.id != self.guild_id:
            raise ValueError("channel guild drift")
        if channel.id != self.channel_id or channel.type.value != 0 or channel.category_id is not None:
            raise ValueError("channel scope drift")
        overwrites = {target.id: overwrite for target, overwrite in channel.overwrites.items()}
        if self.guild_id not in overwrites or overwrites[self.guild_id].view_channel is not False:
            raise ValueError("everyone must be denied view")
        for identity, overwrite in overwrites.items():
            if overwrite.view_channel is True and identity not in {self.bot_id, self.actor_id}:
                raise ValueError("unexpected explicit viewer")
        for identity in (self.bot_id, self.actor_id):
            overwrite = overwrites.get(identity)
            if not overwrite:
                raise ValueError("interactive permissions missing")
            allow, deny = overwrite.pair()
            required = 1024 | 2048 | 65536
            if allow.value & required != required or deny.value & required:
                raise ValueError("interactive permissions missing")
        routing = self.routing()
        if set(routing) != {"allowedChannels", "freeResponseChannels"} or any(
                not isinstance(values, list) or len(values) > 1000 or
                any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in values)
                for values in routing.values()):
            raise ValueError("bounded routing snapshot required")
        for key, values in routing.items():
            if any(value != "*" and not SNOWFLAKE.fullmatch(value) and
                   not (key == "freeResponseChannels" and re.fullmatch(r"#?[a-z][a-z0-9_-]{0,63}", value)) for value in values):
                raise ValueError("invalid routing scope")
        routing["configuredForFinanceChannel"] = all(str(self.channel_id) in values and "*" not in values for values in routing.values())
        return channel, {"kind": "restricted-guild-text", "administratorsRetainOverride": True,
                         "operationalBotId": str(self.bot_id), "administratorVisibilityAccepted": True,
                         "syntheticOnly": True, "realDeliveryEnabled": False, "routing": routing}

    def message(self, message):
        ref = message.reference
        content = message.content
        if (message.channel.id != self.channel_id or message.guild.id != self.guild_id
                or message.author.id != self.actor_id or message.author.bot or message.webhook_id
                or message.type.value != 19 or not ref or ref.channel_id != self.channel_id
                or ref.type.value != 0 or not DECISION.fullmatch(content)):
            return None
        return {"id": str(message.id), "channel_id": str(self.channel_id), "type": 19,
                "author": {"id": str(self.actor_id), "bot": False}, "content": content,
                "message_reference": {"type": 0, "channel_id": str(self.channel_id), "message_id": str(ref.message_id)}}

    def verify_notice(self, message, binding):
        d = json.loads(binding)
        expected = rendered(d)
        embeds = [embed.to_dict() for embed in message.embeds]
        fields = [{"name": field.get("name"), "value": field.get("value")} for field in embeds[0].get("fields", [])] if len(embeds) == 1 else []
        if (message.channel.id != self.channel_id or message.author.id != self.bot_id or not message.author.bot
                or message.webhook_id or len(embeds) != 1
                or embeds[0].get("title") != expected["title"]
                or fields != expected["fields"]
                or embeds[0].get("footer", {}).get("text") != expected["footer"]["text"]):
            raise ValueError("notice readback mismatch")
        return {"id": str(message.id), "channel_id": str(self.channel_id), "author": {"id": str(self.bot_id), "bot": True},
                "embeds": embeds, "nonce": str(message.nonce) if message.nonce is not None else None}

    async def dispatch(self, request) -> Any:
        if (not isinstance(request, dict) or type(request.get("protocol")) is not int or request.get("protocol") != 1
                or request.get("op") not in {"audit", "notify", "read", "replies"}):
            raise ValueError("unknown protocol or operation")
        op = request["op"]
        keys = {"audit": set(), "notify": {"case", "nonce"}, "read": {"messageId"}, "replies": {"after"}}[op]
        if set(request) != keys | {"protocol", "op"}:
            raise ValueError("unknown request fields")
        async with self.lock:
            channel, audit = await self.audit()
            if op == "audit":
                return audit
            if not audit["routing"]["configuredForFinanceChannel"]:
                raise ValueError("exact finance chat routing not configured")
            if op == "notify":
                d = descriptor(request["case"])
                nonce = request["nonce"]
                if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{24}", nonce):
                    raise ValueError("invalid nonce")
                binding = json.dumps(d, sort_keys=True)
                old = self.db.execute("SELECT binding,message FROM notices WHERE nonce=?", (nonce,)).fetchone()
                if old and old[0] != binding:
                    raise ValueError("nonce rebound")
                if not old:
                    self.db.execute("INSERT INTO notices VALUES (?,?,NULL)", (nonce, binding))
                    self.db.commit()
                    message = await channel.send(embed=self.embed_factory(rendered(d)), nonce=nonce,
                                                 allowed_mentions=self.object_factory("mentions"))
                    readback = await channel.fetch_message(message.id)
                    self.verify_notice(readback, binding)
                    self.db.execute("UPDATE notices SET message=? WHERE nonce=?", (str(message.id), nonce))
                    self.db.commit()
                    return {"messageId": str(message.id)}
                if old[1]:
                    self.verify_notice(await channel.fetch_message(int(old[1])), binding)
                    return {"messageId": old[1]}
                # A persisted intent is never resent: Discord nonce dedup expires.
                matches = []
                async for message in channel.history(limit=200):
                    try:
                        readback = self.verify_notice(message, binding)
                        if readback["nonce"] != nonce:
                            continue
                    except ValueError:
                        continue
                    matches.append(message)
                if len(matches) != 1:
                    raise ValueError("uncertain notice requires owner reconciliation")
                message = matches[0]
                self.db.execute("UPDATE notices SET message=? WHERE nonce=?", (str(message.id), nonce))
                self.db.commit()
                return {"messageId": str(message.id)}
            key = "messageId" if op == "read" else "after"
            value = request[key]
            if not isinstance(value, str) or not SNOWFLAKE.fullmatch(value):
                raise ValueError("invalid message id")
            if op == "read":
                row = self.db.execute("SELECT binding FROM notices WHERE message=?", (value,)).fetchone()
                if not row:
                    raise ValueError("unbound message")
                return self.verify_notice(await channel.fetch_message(int(value)), row[0])
            messages = []
            async for message in channel.history(limit=100, after=self.object_factory(int(value)), oldest_first=True):
                messages.append(self.message(message) or {"id": str(message.id), "channel_id": str(self.channel_id)})
            return messages

    async def handle(self, reader, writer):
        try:
            async with self.connections:
                peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                if struct.unpack("3i", peer)[1] != os.getuid():
                    raise ValueError("unauthorized local peer")
                line = await asyncio.wait_for(reader.readline(), 5)
                if len(line) > 4096 or not line.endswith(b"\n"):
                    raise ValueError("bounded request required")
                result = await asyncio.wait_for(self.dispatch(json.loads(line)), 30)
                writer.write(json.dumps({"ok": True, "result": result}).encode() + b"\n")
                await writer.drain()
        except Exception:
            writer.write(b'{"ok":false,"error":"bridge request rejected; delivery may be uncertain"}\n')
            try:
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def start(self):
        if self.server or not self.native.user or self.native.user.id != self.bot_id:
            return
        await self.audit()
        if self.socket_path.exists():
            private_path(self.socket_path)
            if not stat.S_ISSOCK(self.socket_path.stat().st_mode):
                raise ValueError("socket path occupied")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1)
                probe.connect(str(self.socket_path))
            except ConnectionRefusedError:
                self.socket_path.unlink()
            else:
                raise ValueError("bridge already active")
            finally:
                probe.close()
        self.server = await asyncio.start_unix_server(self.handle, path=self.socket_path, limit=4096)
        self.socket_path.chmod(0o600)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


def register(ctx):
    identities = {}
    for key in ("bot_id", "actor_id", "guild_id", "channel_id"):
        env_key = f"FINANCE_REVIEW_BRIDGE_{key.upper()}"
        value = ctx.get_config(key, os.environ.get(env_key))
        if type(value) not in (str, int) or not SNOWFLAKE.fullmatch(str(value)):
            raise ValueError(f"configure {key} in plugin settings or {env_key}")
        identities[key] = int(value)

    def wire(native, adapter):
        from hermes_constants import get_hermes_home
        import discord

        root = get_hermes_home() / "plugin-data" / "finance-review-bridge"
        bridge = None

        def objects(value):
            return discord.AllowedMentions.none() if value == "mentions" else discord.Object(id=value)

        async def ready():
            nonlocal bridge
            if not native.user or native.user.id != identities["bot_id"]:
                return
            if bridge is None:
                bridge = Bridge(native, root, discord.Embed.from_dict, objects, lambda: routing_snapshot(adapter), **identities)
            await bridge.start()

        async def disconnected():
            if bridge:
                await bridge.stop()

        native.add_listener(ready, "on_ready")
        native.add_listener(disconnected, "on_disconnect")
        if native.is_ready():
            asyncio.create_task(ready())

    ctx.register_platform_handler("discord", wire)
