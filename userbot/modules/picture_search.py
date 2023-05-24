from asyncio import TimeoutError
from functools import reduce
from itertools import takewhile
from typing import Dict, List, Optional, Union

from cachetools import TTLCache
from cachetools.keys import hashkey
from httpx import AsyncClient
from loguru import logger
from PicImageSearch import Network
from telethon import TelegramClient, events
from telethon.errors import MediaCaptionTooLongError, MessageNotModifiedError
from telethon.events import CallbackQuery
from telethon.hints import EntityLike
from telethon.tl.custom import Button
from telethon.tl.patched import Message
from tenacity import retry, stop_after_attempt, stop_after_delay

from .. import SEARCH_FUNCTION_TYPE, SEARCH_RESULT_TYPE, bot
from ..ascii2d import ascii2d_search
from ..config import config
from ..ehentai import ehentai_search
from ..iqdb import iqdb_search
from ..saucenao import saucenao_search
from ..utils import async_cached, get_first_frame_from_video
from ..whatanime import whatanime_search

bot_name = ""
allowed_users = [config.owner_id] + config.allowed_users
search_mode_tips = "请选择搜图模式"
search_buttons = [
    [
        Button.inline("Ascii2D"),
        Button.inline("Iqdb"),
        Button.inline("SauceNAO"),
    ],
    [
        Button.inline("Pixiv"),
        Button.inline("Danbooru"),
    ],
    [
        Button.inline("WhatAnime"),
        Button.inline("Anime"),
    ],
    [
        Button.inline("EHentai"),
        Button.inline("Doujin"),
    ],
]


def check_permission(
    event: Union[events.NewMessage.Event, events.Album.Event, CallbackQuery.Event]
) -> bool:
    return event.sender_id in allowed_users or event.chat_id in config.allowed_chats


@bot.on(events.NewMessage(from_users=allowed_users, pattern="/start"))  # type: ignore
async def start(event: events.NewMessage.Event) -> None:
    await event.reply("请发送图片，然后选择搜图模式")


async def is_mentioned_or_get_command(
    event: Union[events.NewMessage.Event, events.Album.Event]
) -> bool:
    global bot_name
    if not bot_name:
        bot_name = (await bot.get_me()).username
    return f"@{bot_name}" in event.text or "搜图" in event.text


def is_photo_or_video(
    event_or_message: Union[events.NewMessage.Event, Message]
) -> bool:
    if event_or_message.photo:
        return True
    elif document := event_or_message.document:
        if document.mime_type.startswith("image/") or document.mime_type == "video/mp4":
            return True
    return False


async def wait_callback(
    event: Union[events.NewMessage.Event, events.Album.Event], reply_to_msg: Message
) -> None:
    if event.is_private:
        await bot.send_message(
            event.chat_id,
            search_mode_tips,
            buttons=search_buttons,
            reply_to=reply_to_msg,
        )
    else:
        async with bot.conversation(
            event.chat_id, timeout=180, exclusive=False
        ) as conv:
            msg = await conv.send_message(
                search_mode_tips,
                buttons=search_buttons,
                reply_to=reply_to_msg,
            )
            while True:
                try:
                    response = await conv.wait_event(
                        events.CallbackQuery(
                            func=lambda e: e.sender_id == event.sender_id
                        )
                    )
                    await handle_search(response)
                except TimeoutError:
                    break
        await msg.delete()


@bot.on(events.NewMessage(func=check_permission))  # type: ignore
@bot.on(events.Album(func=check_permission))  # type: ignore
async def handle_message_event(
    event: Union[events.NewMessage.Event, events.Album.Event]
) -> None:
    if (event.is_group or event.is_channel) and not await is_mentioned_or_get_command(
        event
    ):
        return
    if (
        isinstance(event, events.NewMessage.Event)
        and not event.grouped_id
        and (is_photo_or_video(event) or event.is_reply)
    ):
        reply_to_msg = (
            await event.get_reply_message() if event.is_reply else event.message
        )
        await wait_callback(event, reply_to_msg)
    elif isinstance(event, events.Album.Event):
        await wait_callback(event, event.messages[0])


@bot.on(CallbackQuery(func=lambda e: e.is_private))  # type: ignore
async def handle_search(event: events.CallbackQuery.Event) -> None:
    reply_to_msg: Message = await event.get_message()
    buttons = [
        i
        for i in reduce(lambda x, y: x + y, reply_to_msg.buttons)
        if i.data != event.data
    ]
    buttons = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    # 奇怪的 BUG ：有时候会间隔 N 秒连续触发同一个按钮的点击事件
    try:
        await reply_to_msg.edit(text="正在进行搜索，请稍候", buttons=None)
    except MessageNotModifiedError:
        return
    if is_photo_or_video(reply_to_msg):
        msgs = [reply_to_msg]
    else:
        msgs = await get_messages_to_search(reply_to_msg)
        if not msgs:
            await bot.send_message(event.chat_id, "没有获取到图片或视频", reply_to=reply_to_msg)
            return
    network = (
        Network(proxies=config.proxy, cookies=config.exhentai_cookies, timeout=60)
        if event.data == b"EHentai"
        else Network(proxies=config.proxy)
    )
    async with network as client:
        for msg in msgs:
            try:
                _file = await get_file_from_message(msg, event.chat_id)
                if not _file:
                    continue
                result = await handle_search_mode(event.data, _file, client)
                # 如果媒体文件为视频，就不往回发了
                if (document := msg.document) and document.mime_type == "video/mp4":
                    _file = None
                for caption, __file in result:
                    await send_search_results(
                        bot,
                        event.chat_id,
                        caption,
                        msg,
                        file=__file,
                        extra_file=_file,
                        buttons=buttons,
                    )
            except Exception as e:
                logger.exception(e)
                await bot.send_message(
                    event.chat_id, f"该图搜图失败\nE: {repr(e)}", reply_to=msg
                )
    await reply_to_msg.edit(text=search_mode_tips, buttons=buttons)


@async_cached(cache=TTLCache(maxsize=16, ttl=180), key=lambda msg, chat_id: hashkey(msg.id, chat_id))  # type: ignore
async def get_file_from_message(msg: Message, chat_id: EntityLike) -> Optional[bytes]:
    if (document := msg.document) and document.mime_type == "video/mp4":
        if document.size > 10 * 1024 * 1024:
            await bot.send_message(chat_id, "跳过超过 10M 的视频", reply_to=msg)
            return None
        _file = await get_first_frame_from_video(
            await bot.download_media(document, file=bytes)
        )
    elif msg.photo:
        _file = await bot.download_media(msg.photo, file=bytes)
    else:
        _file = await bot.download_media(msg.document, file=bytes)
    if not _file:
        await bot.send_message(chat_id, "图片或视频获取失败", reply_to=msg)
        return None
    return _file


async def get_messages_to_search(msg: Message) -> List[Message]:
    msgs = await bot.get_messages(msg.peer_id, ids=[msg.reply_to.reply_to_msg_id])
    if grouped_id := msgs[0].grouped_id:
        last_100_messages = await bot.get_messages(
            msg.peer_id, ids=list(range(msgs[0].id, msgs[0].id + 100))
        )
        msgs = list(
            takewhile(
                lambda x: x and x.grouped_id == grouped_id,
                last_100_messages,
            )
        )
    return [i for i in msgs if is_photo_or_video(i)]


@retry(stop=(stop_after_attempt(3) | stop_after_delay(30)), reraise=True)
@async_cached(cache=TTLCache(maxsize=16, ttl=180))  # type: ignore
async def handle_search_mode(
    event_data: bytes, file: bytes, client: AsyncClient
) -> SEARCH_RESULT_TYPE:
    search_function_dict: Dict[bytes, SEARCH_FUNCTION_TYPE] = {
        b"Ascii2D": ascii2d_search,
        b"EHentai": ehentai_search,  # type: ignore
        b"Iqdb": iqdb_search,
        b"WhatAnime": whatanime_search,
        b"SauceNAO": lambda file, client: saucenao_search(file, client, "all"),  # type: ignore
    }
    search_function = search_function_dict.get(
        event_data,
        lambda: lambda file, client: saucenao_search(
            file, client, event_data.decode().lower()
        ),
    )
    return await search_function(file, client)


async def send_search_results(
    _bot: TelegramClient,
    send_to: int,
    caption: str,
    reply_to: Message,
    file: Union[List[str], List[bytes], str, bytes, None] = None,
    extra_file: Union[str, bytes, None] = None,
    buttons: Optional[List[List[Button]]] = None,
) -> None:
    if send_to != config.owner_id and "已收藏" in caption:
        caption = caption.replace("❤️ 已收藏\n", "")

    if file:
        try:
            await _bot.send_file(send_to, file=file, caption=caption, reply_to=reply_to)
        except MediaCaptionTooLongError:
            await _bot.send_message(
                send_to, caption, reply_to=reply_to, link_preview=False
            )
            await _bot.send_file(send_to, file=file, reply_to=reply_to)
    else:
        if "使用次数" in caption:
            buttons = None
        if extra_file:
            await _bot.send_file(
                send_to,
                file=extra_file,
                caption=caption,
                reply_to=reply_to,
                buttons=buttons,
            )
        else:
            await _bot.send_message(
                send_to, caption, reply_to=reply_to, link_preview=False
            )
