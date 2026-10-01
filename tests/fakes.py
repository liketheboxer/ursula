"""Shared fakes for the cog tests (they lived in Ursula's crew tests)."""
from types import SimpleNamespace


class FakeResponse:
    def __init__(self):
        self.messages, self.deferred = [], False

    async def send_message(self, content=None, **kw):
        self.messages.append(content)

    async def defer(self, **kw):
        self.deferred = True

    def is_done(self):
        return bool(self.messages) or self.deferred


class FakeMessage:
    def __init__(self, mid):
        self.id, self.edits = mid, []

    async def edit(self, **kw):
        self.edits.append(kw)

    async def delete(self):
        self.deleted = True


class FakeTextChannel:
    def __init__(self, cid):
        self.id, self.sent, self.category, self.messages = cid, [], None, {}

    async def send(self, content=None, **kw):
        msg = FakeMessage(1000 + len(self.sent))
        self.messages[msg.id] = msg
        self.sent.append((content, kw))
        return msg

    def get_partial_message(self, mid):
        return self.messages.setdefault(mid, FakeMessage(mid))


class FakeVoice:
    def __init__(self, vid, name):
        self.id, self.name, self.members, self.deleted = vid, name, [], False
        self.mention = f"<#{vid}>"

    async def delete(self, reason=None):
        self.deleted = True


class FakeGuild:
    def __init__(self, gid, text):
        self.id, self.text, self.voices, self.members = gid, text, {}, {}

    def get_channel(self, cid):
        if cid == self.text.id:
            return self.text
        v = self.voices.get(cid)
        return None if v is None or v.deleted else v

    def get_member(self, uid):
        return self.members.get(uid)

    def get_role(self, rid):
        return None

    async def create_voice_channel(self, name, category=None, user_limit=None, reason=None):
        vc = FakeVoice(5000 + len(self.voices), name)
        vc.user_limit = user_limit
        self.voices[vc.id] = vc
        return vc


def interaction_for(guild, uid, perms=None):
    user = SimpleNamespace(id=uid, mention=f"<@{uid}>", display_name=f"U{uid}",
                           guild_permissions=perms or SimpleNamespace(manage_channels=False, manage_guild=False,
                                                                      administrator=False))
    return SimpleNamespace(user=user, guild=guild, guild_id=guild.id, response=FakeResponse())



def stop_loops(bot) -> None:
    """Cancel every cog's background loop, so tests drive the clocks themselves."""
    from discord.ext import tasks
    for cog in bot.cogs.values():
        for value in vars(cog).values():
            if isinstance(value, tasks.Loop):
                value.cancel()
        for name in dir(type(cog)):
            value = getattr(cog, name, None)
            if isinstance(value, tasks.Loop):
                value.cancel()
