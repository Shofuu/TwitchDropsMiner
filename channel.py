from __future__ import annotations

import re
import json
import asyncio
import logging
from base64 import b64encode
from functools import cached_property
from typing import Any, Optional, List, SupportsInt, TYPE_CHECKING

from utils import Game, invalidate_cache
from exceptions import MinerException, RequestException
from constants import JsonType, BASE_URL, GQL_OPERATIONS, ONLINE_DELAY, DROPS_ENABLED_TAG

if TYPE_CHECKING:
    from twitch import Twitch
    from gui import ChannelList


logger = logging.getLogger("TwitchDrops")


class Stream:
    def __init__(
        self,
        channel: Channel,
        *,
        id: SupportsInt,
        game: Optional[JsonType],
        viewers: int,
        title: str,
        tags: List[JsonType],
    ):
        self.channel: Channel = channel
        self.broadcast_id = int(id)
        self.viewers: int = viewers
        self.drops_enabled: bool = any(t["id"] == DROPS_ENABLED_TAG for t in tags)
        self.game: Optional[Game] = Game(game) if game else None
        self.title: str = title

    @classmethod
    def from_get_stream(cls, channel: Channel, data: JsonType) -> Stream:
        stream = data["stream"]
        settings = data["broadcastSettings"]
        return cls(
            channel,
            id=stream["id"],
            game=settings["game"],
            viewers=stream["viewersCount"],
            title=settings["title"],
            tags=stream["tags"],
        )

    @classmethod
    def from_directory(cls, channel: Channel, data: JsonType) -> Stream:
        return cls(
            channel,
            id=data["id"],
            game=data["game"],  # has to be there since we searched with it
            viewers=data["viewersCount"],
            title=data["title"],
            tags=data["tags"],
        )


class Channel:
    def __init__(
        self,
        twitch: Twitch,
        *,
        id: SupportsInt,
        login: str,
        display_name: Optional[str] = None,
        priority: bool = False,
    ):
        self._twitch: Twitch = twitch
        self._gui_channels: ChannelList = twitch.gui.channels
        self.id: int = int(id)
        self._login: str = login
        self._display_name: Optional[str] = display_name
        self._spade_url: Optional[str] = None
        self.points: Optional[int] = None
        self._stream: Optional[Stream] = None
        self._pending_stream_up: Optional[asyncio.Task[Any]] = None
        # Priority channels are:
        # • considered first when switching channels
        # • if we're watching a non-priority channel, a priority channel going up triggers a switch
        # • not cleaned up unless they're streaming a game we haven't selected
        self.priority: bool = priority

    @classmethod
    def from_acl(cls, twitch: Twitch, data: JsonType) -> Channel:
        return cls(
            twitch,
            id=data["id"],
            login=data["name"],
            display_name=data.get("displayName"),
            priority=True,
        )

    @classmethod
    def from_directory(cls, twitch: Twitch, data: JsonType) -> Channel:
        channel = data["broadcaster"]
        self = cls(
            twitch, id=channel["id"], login=channel["login"], display_name=channel["displayName"]
        )
        self._stream = Stream.from_directory(self, data)
        return self

    @classmethod
    async def from_name(
        cls, twitch: Twitch, channel_login: str, *, priority: bool = False
    ) -> Channel:
        self = cls(twitch, id=0, login=channel_login, priority=priority)
        # id and display name to be filled/overwritten by get_stream
        stream = await self.get_stream()
        if stream is not None:
            self._stream = stream
        return self

    def __repr__(self) -> str:
        if self._display_name is not None:
            name = f"{self._display_name}({self._login})"
        else:
            name = self._login
        return f"Channel({name}, {self.id})"

    def __eq__(self, other: object):
        if isinstance(other, self.__class__):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.__class__.__name__, self.id))

    @property
    def name(self) -> str:
        if self._display_name is not None:
            return self._display_name
        return self._login

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self._login}"

    @property
    def iid(self) -> str:
        """
        Returns a string to be used as ID/key of the columns inside channel list.
        """
        return str(self.id)

    @property
    def online(self) -> bool:
        """
        Returns True if the streamer is online and is currently streaming, False otherwise.
        """
        return self._stream is not None

    @property
    def offline(self) -> bool:
        """
        Returns True if the streamer is offline and isn't about to come online, False otherwise.
        """
        return self._stream is None and self._pending_stream_up is None

    @property
    def pending_online(self) -> bool:
        """
        Returns True if the streamer is about to go online (most likely), False otherwise.
        This is because 'stream-up' event is received way before
        stream information becomes available.
        """
        return self._pending_stream_up is not None

    @property
    def game(self) -> Optional[Game]:
        if self._stream is not None and self._stream.game is not None:
            return self._stream.game
        return None

    @property
    def viewers(self) -> Optional[int]:
        if self._stream is not None:
            return self._stream.viewers
        return None

    @viewers.setter
    def viewers(self, value: int):
        if self._stream is not None:
            self._stream.viewers = value

    @property
    def drops_enabled(self) -> bool:
        if self._stream is not None:
            return self._stream.drops_enabled
        return False

    def display(self, *, add: bool = False):
        self._gui_channels.display(self, add=add)

    def remove(self):
        if self._pending_stream_up is not None:
            self._pending_stream_up.cancel()
            self._pending_stream_up = None
        self._gui_channels.remove(self)

    async def get_spade_url(self) -> str:
        """
        To get this monstrous thing, you have to walk a chain of requests.
        Streamer page (HTML) --parse-> Streamer Settings (JavaScript) --parse-> Spade URL
        """
        async with self._twitch.request("GET", self.url) as response:
            streamer_html: str = await response.text(encoding="utf8")
        match = re.search(
            r'src="(https://static\.twitchcdn\.net/config/settings\.[0-9a-f]{32}\.js)"',
            streamer_html,
            re.I,
        )
        if not match:
            raise MinerException("Error while spade_url extraction: step #1")
        streamer_settings = match.group(1)
        async with self._twitch.request("GET", streamer_settings) as response:
            settings_js: str = await response.text(encoding="utf8")
        match = re.search(
            r'"spade_url": ?"(https://video-edge-[.\w\-/]+\.ts)"', settings_js, re.I
        )
        if not match:
            raise MinerException("Error while spade_url extraction: step #2")
        return match.group(1)

    async def get_stream(self) -> Optional[Stream]:
        response: Optional[JsonType] = await self._twitch.gql_request(
            GQL_OPERATIONS["GetStreamInfo"].with_variables({"channel": self._login})
        )
        if not response:
            return None
        stream_data: Optional[JsonType] = response["data"]["user"]
        if not stream_data:
            return None
        # fill in channel_id and display name
        self.id = int(stream_data["id"])
        self._display_name = stream_data["displayName"]
        if not stream_data["stream"]:
            return None
        return Stream.from_get_stream(self, stream_data)

    async def check_online(self) -> bool:
        self._stream = stream = await self.get_stream()
        if stream is None:
            invalidate_cache(self, "_payload")
            return False
        return True

    async def _online_delay(self):
        """
        The 'stream-up' event is sent before the stream actually goes online,
        so just wait a bit and check if it's actually online by then.
        """
        await asyncio.sleep(ONLINE_DELAY.total_seconds())
        online = await self.check_online()
        self._pending_stream_up = None  # for 'display' to work properly
        self.display()
        if online:
            self._twitch.on_online(self)

    def set_online(self):
        """
        Sets the channel status to PENDING_ONLINE, where after ONLINE_DELAY,
        it's going to be set to ONLINE.

        This is called externally, if we receive an event about this happening.
        """
        if self.offline:
            self._pending_stream_up = asyncio.create_task(self._online_delay())
            self.display()

    def set_offline(self):
        """
        Sets the channel status to OFFLINE. Cancels PENDING_ONLINE if applicable.

        This is called externally, if we receive an event about this happening.
        """
        if self._pending_stream_up is not None:
            self._pending_stream_up.cancel()
            self._pending_stream_up = None
            self.display()
        if self.online:
            self._stream = None
            invalidate_cache(self, "_payload")
            self.display()
            self._twitch.on_offline(self)

    async def claim_bonus(self):
        """
        This claims bonus points if they're available, and fills out the 'points' attribute.
        """
        response = await self._twitch.gql_request(
            GQL_OPERATIONS["ChannelPointsContext"].with_variables({"channelLogin": self._login})
        )
        channel_data: JsonType = response["data"]["community"]["channel"]
        self.points = channel_data["self"]["communityPoints"]["balance"]
        claim_available: JsonType = (
            channel_data["self"]["communityPoints"]["availableClaim"]
        )
        if claim_available:
            await self._twitch.claim_points(channel_data["id"], claim_available["id"])
            logger.info("Claimed bonus points")
        else:
            # calling 'claim_points' is going to refresh the display via the websocket payload,
            # so if we're not calling it, we need to do it ourselves
            self.display()

    @cached_property
    def _payload(self) -> JsonType:
        assert self._stream is not None
        payload = [
            {
                "event": "minute-watched",
                "properties": {
                    "channel_id": self.id,
                    "broadcast_id": self._stream.broadcast_id,
                    "player": "site",
                    "user_id": self._twitch._user_id,
                }
            }
        ]
        json_event = json.dumps(payload, separators=(",", ":"))
        return {"data": (b64encode(json_event.encode("utf8"))).decode("utf8")}

    async def send_watch(self) -> bool:
        """
        This uses the encoded payload on spade url to simulate watching the stream.
        Optimally, send every 60 seconds to advance drops.
        """
        if not self.online:
            return False
        if self._spade_url is None:
            self._spade_url = await self.get_spade_url()
        logger.debug(f"Sending minute-watched to {self.name}")
        try:
            async with self._twitch.request(
                "POST", self._spade_url, data=self._payload
            ) as response:
                return response.status == 204
        except RequestException:
            return False
