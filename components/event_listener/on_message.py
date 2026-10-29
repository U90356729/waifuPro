import json
import logging
import re
import time

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events, context
from langbot_plugin.api.entities.builtin.platform.message import Plain, MessageChain

logger = logging.getLogger(__name__)

# 心动值后缀，例如（12❤️）/（-8🖤）
HEART_MARKER = re.compile(r"（-?\d+(?:❤️|🖤)）")
# 按句末标点拆分，用于分段发送
SENTENCE_SPLIT = re.compile(r"([。！？])")
# 复读计数的有效期，避免旧计数一直累积
REPEAT_WINDOW_SECONDS = 60
# 分段发送的停顿：单段最多 0.35 秒，整条回复累计不超过 1.2 秒
MAX_SEGMENT_DELAY = 0.35
MAX_TOTAL_DELAY = 1.2
# 单张图片 base64 的长度上限，超过就跳过，避免请求体过大
MAX_IMAGE_B64_CHARS = 4 * 1024 * 1024


def _safe_get(obj, attr: str):
    try:
        return getattr(obj, attr)
    except Exception:
        return None


def _get_sender_display_name(event, fallback: str) -> str:
    """尽力从事件里取一个可读的发送者名字，取不到就用兜底值。"""
    for key in ("sender_name", "sender_nickname", "nickname", "display_name", "member_name", "name"):
        val = _safe_get(event, key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    sender = _safe_get(event, "sender")
    if sender is not None:
        for key in ("nickname", "display_name", "name", "username"):
            val = _safe_get(sender, key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return fallback


def _extract_plain_text(message_chain) -> str:
    """把消息链压成纯文本，@ 保留为 @名字。"""
    parts = []
    for elem in message_chain or []:
        text = getattr(elem, "text", None)
        if isinstance(text, str):
            parts.append(text)
            continue
        name = elem.__class__.__name__.lower()
        if "at" in name or "mention" in name or hasattr(elem, "target") or hasattr(elem, "targets"):
            display = _safe_get(elem, "display")
            if isinstance(display, str) and display.strip():
                parts.append(display if display.startswith("@") else f"@{display}")
            else:
                parts.append("@")
    return "".join(parts).strip()


def _extract_images(message_chain, limit: int = 3) -> list:
    """收集消息里的图片，返回 [{base64, url}]。QQ 图片链接会过期，优先用 base64。"""
    images = []
    for elem in message_chain or []:
        if "image" not in elem.__class__.__name__.lower():
            continue

        item = {}
        b64 = _safe_get(elem, "base64")
        if isinstance(b64, str) and b64.strip():
            if len(b64) > MAX_IMAGE_B64_CHARS:
                logger.warning(f"图片过大已跳过: {len(b64)} 字符")
                continue
            item["base64"] = b64.strip()

        url = _safe_get(elem, "url")
        if isinstance(url, str) and url.strip():
            item["url"] = url.strip()

        if item:
            images.append(item)
        if len(images) >= limit:
            break

    if images:
        logger.info(f"消息中检测到图片 {len(images)} 张")
    return images


def _strip_heart_markers(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return HEART_MARKER.sub("", text).strip()


class OnMessageEventListener(EventListener):
    def __init__(self):
        super().__init__()
        self.group_blacklist = set()
        # group_id -> {text: (count, last_ts)}
        self._repeat_state = {}

        @self.handler(events.PersonNormalMessageReceived)
        async def on_person_message_received(ctx: context.EventContext):
            await self.handle_person_message(ctx)

        @self.handler(events.GroupNormalMessageReceived)
        async def on_group_message_received(ctx: context.EventContext):
            await self.handle_group_message(ctx)

    # ------------------------------------------------------------------ 主动发言

    async def _prime_proactive(self, plugin):
        """把当前这次调用里的插件配置交给主动发言模块，并确保它的循环在跑。

        插件配置只在一次调用期间可见，离开调用上下文就是空的，
        所以必须趁处理消息的时候存一份快照，后台循环才有配置可用。
        """
        greeter = getattr(plugin, "proactive", None)
        if greeter is None:
            return
        try:
            greeter.absorb_config(plugin.get_config())
        except Exception as e:
            logger.debug(f"同步主动发言配置失败: {e}")
        try:
            greeter.ensure_started()
        except Exception as e:
            logger.error(f"启动主动发言循环失败: {e}", exc_info=True)

    # ------------------------------------------------------------------ 私聊

    async def handle_person_message(self, ctx: context.EventContext):
        plugin = self.plugin
        try:
            message_chain = _safe_get(getattr(ctx, "event", None), "message_chain")
            text = _extract_plain_text(message_chain)
            images = _extract_images(message_chain)
            if not text and not images:
                logger.debug("私聊消息内容为空，忽略")
                return
            if not text:
                text = "[发来一张图片]"

            # 确定要处理这条消息了，再拦掉框架流水线，避免重复回复
            ctx.prevent_default()

            # 趁这次调用还在，把配置交给主动发言模块（后台读不到配置）
            await self._prime_proactive(plugin)

            plugin.current_bot_uuid = await ctx.get_bot_uuid()
            user_id = str(ctx.event.sender_id)

            # 记住目标用户在哪个机器人上说话，主动消息走同一条通道
            greeter = getattr(plugin, "proactive", None)
            if greeter is not None:
                greeter.remember_bot_for_target(user_id, plugin.current_bot_uuid)

            # 一个用户一个上下文，key 就是记忆存储用的 id，保证老数据能继续读到
            conv = await plugin.get_context(user_id)

            async with conv.lock:
                await conv.ensure_ready(user_id, "Waifu")
                memory, cards = conv.memory, conv.cards

                character = self._pick_character(user_id, "person")
                logger.info(f"用户 {user_id} 使用角色卡: {character}")
                await cards.load_config(character, "person")

                await memory.add_short_term_memory("user", text)

                value_game = await self._roll_affection(character, user_id, "person", memory, text)
                prompt, _ = await plugin.thoughts.generate_person_prompt(
                    memory, cards, attitude_prompt=value_game.get_attitude_prompt()
                )

                response = await plugin.generator.generate_response(prompt, ctx=ctx, cards=cards, images=images)
                response = _strip_heart_markers(response)
                response = await plugin.generator.apply_personification(response)
                response = _strip_heart_markers(response)

                if plugin.get_config().get("display_value", False):
                    response += value_game.get_manner_value_str()

                await self.send_with_typing_effect(ctx, response)

                response_content = _strip_heart_markers(response).split("\n")[0]
                await memory.add_short_term_memory("bot", response_content)

            logger.info(f"私信回复用户 {user_id}: {response_content}")

        except Exception as e:
            logger.error(f"处理私信消息失败: {e}", exc_info=True)
            await self._reply_safe(ctx, "抱歉，我现在无法回复你的消息。")

    # ------------------------------------------------------------------ 群聊

    async def handle_group_message(self, ctx: context.EventContext):
        plugin = self.plugin
        try:
            message_chain = _safe_get(getattr(ctx, "event", None), "message_chain")
            text = _extract_plain_text(message_chain)
            images = _extract_images(message_chain)
            if not text and not images:
                logger.debug("群聊消息内容为空，忽略")
                return
            if not text:
                text = "[发来一张图片]"

            # 确定要处理这条消息了，再拦掉框架流水线，避免重复回复
            ctx.prevent_default()

            # 群聊也算一次调用，顺手把配置同步给主动发言模块
            await self._prime_proactive(plugin)

            plugin.current_bot_uuid = await ctx.get_bot_uuid()
            user_id = str(ctx.event.sender_id)
            group_id = self._resolve_group_id(ctx.event)

            if group_id in self.group_blacklist:
                logger.debug(f"群聊 {group_id} 在黑名单中，忽略消息")
                return

            # 复读：达到配置的重复次数就把这句话原样发回去，repeat_trigger <= 0 关闭
            if await self.should_repeat(group_id, text):
                logger.info(f"群聊 {group_id} 触发复读: {text}")
                await ctx.reply(MessageChain([Plain(text=text)]))
                return

            user_name = _get_sender_display_name(ctx.event, f"用户{user_id}")
            logger.info(f"群聊 {group_id} 处理用户 {user_name} 的消息: {text}")

            # 群是共享上下文：一个群一个记忆，key 保持 group_{group_id} 以兼容旧数据
            conv = await plugin.get_context(f"group_{group_id}")

            async with conv.lock:
                await conv.ensure_ready("Group", "Waifu")
                memory, cards = conv.memory, conv.cards

                character = self._pick_character(user_id, "group")
                await cards.load_config(character, "group")

                await memory.add_short_term_memory(user_name, text)
                await memory.append_group_chat_log(user_id, user_name, text)

                value_game = await self._roll_affection(
                    character, f"group_{group_id}_{user_id}", "group", memory, text
                )
                prompt, _ = await plugin.thoughts.generate_group_prompt(
                    memory, cards, attitude_prompt=value_game.get_attitude_prompt()
                )

                response = await plugin.generator.generate_response(prompt, ctx=ctx, cards=cards, images=images)
                response = _strip_heart_markers(response)
                response = await plugin.generator.apply_personification(response)
                response = _strip_heart_markers(response)

                if plugin.get_config().get("display_value", False):
                    response += value_game.get_manner_value_str()

                await self.send_with_typing_effect(ctx, response)

                clean_response = _strip_heart_markers(response)
                await memory.add_short_term_memory(cards.get_assistant_name(), clean_response)
                await memory.append_group_chat_log(
                    plugin.current_bot_uuid or "bot", cards.get_assistant_name(), clean_response
                )

            logger.info(f"群聊 {group_id} 回复用户 {user_name}: {response}")

        except Exception as e:
            logger.error(f"处理群聊消息失败: {e}", exc_info=True)
            await self._reply_safe(ctx, "抱歉，我现在无法回复消息。")

    # ------------------------------------------------------------------ 内部

    def _resolve_group_id(self, event) -> str:
        """群号在不同平台字段名不一样，逐个试。"""
        for key in ("launcher_id", "group_id", "target_id", "channel_id"):
            val = _safe_get(event, key)
            if val is not None and str(val).strip():
                return str(val).strip()
        logger.warning("无法找到群聊ID属性，使用默认值: default_group")
        return "default_group"

    def _pick_character(self, user_id: str, mode: str) -> str:
        """私聊按用户映射或默认配置选角色卡；群聊统一用群聊角色卡。"""
        config = self.plugin.get_config() or {}
        default = config.get("group_character" if mode == "group" else "character", "cute_neko")
        if mode == "group":
            return default

        mappings = config.get("user_character_mappings") or {}
        if isinstance(mappings, str):
            try:
                mappings = json.loads(mappings)
            except (json.JSONDecodeError, TypeError):
                logger.warning(f"user_character_mappings 不是合法 JSON，使用默认角色卡: {default}")
                mappings = {}
        if not isinstance(mappings, dict):
            mappings = {}
        return mappings.get(user_id) or default

    async def _roll_affection(self, character, launcher_id, launcher_type, memory, text):
        """算一次心动值变化，返回带本次结果的 ValueGame 实例。"""
        from systems.value_game import ValueGame

        value_game = ValueGame(self.plugin)
        await value_game.load_config(character, launcher_id, launcher_type)
        await value_game.determine_manner_change(
            memory.get_short_term_memory_text(), 0, last_user_text=text
        )
        return value_game

    async def _reply_safe(self, ctx, message: str):
        try:
            await ctx.reply(MessageChain([Plain(text=message)]))
        except Exception as e:
            logger.error(f"发送兜底回复失败: {e}")

    async def send_with_typing_effect(self, ctx, message: str, delay: float = 0.05):
        """
        按句末标点把回复拆成几段发送，模拟边说边想的节奏。
        """
        import asyncio

        if not message:
            return

        segments = SENTENCE_SPLIT.split(message)
        combined = []
        i = 0
        while i < len(segments):
            if i + 1 < len(segments):
                combined.append(segments[i] + segments[i + 1])
                i += 2
            else:
                combined.append(segments[i])
                i += 1

        combined = [seg for seg in combined if seg.strip()]
        remaining_budget = MAX_TOTAL_DELAY
        for segment in combined:
            pause = min(delay * len(segment), MAX_SEGMENT_DELAY, remaining_budget)
            if pause > 0:
                remaining_budget -= pause
                await asyncio.sleep(pause)
            await ctx.reply(MessageChain([Plain(text=segment)]))

    async def should_repeat(self, group_id: str, text: str) -> bool:
        """判断是否该复读这条消息，计数超过时间窗口会自动过期。"""
        threshold = int(self.plugin.get_config().get("repeat_trigger", 0) or 0)
        if threshold <= 0:
            return False

        now = time.time()
        bucket = self._repeat_state.setdefault(group_id, {})

        for key, (_, last_ts) in list(bucket.items()):
            if now - last_ts > REPEAT_WINDOW_SECONDS:
                bucket.pop(key, None)

        count = bucket.get(text, (0, now))[0] + 1
        if count >= threshold:
            bucket.pop(text, None)
            return True

        bucket[text] = (count, now)
        return False

    async def add_to_blacklist(self, group_id: str):
        self.group_blacklist.add(group_id)
        logger.info(f"群聊 {group_id} 已添加到黑名单")

    async def remove_from_blacklist(self, group_id: str):
        if group_id in self.group_blacklist:
            self.group_blacklist.remove(group_id)
            logger.info(f"群聊 {group_id} 已从黑名单移除")
