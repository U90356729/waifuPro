import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events, context
from langbot_plugin.api.entities.builtin.platform.message import Plain, MessageChain, Image

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
# 模型在回复里用 [IMAGE:类别] 表示"想发一张这个类别的图"
IMAGE_MARKER = re.compile(r"[\[【]\s*IMAGE\s*[:：]\s*([^\]】]*?)\s*[\]】]", re.I)
# 模型想现场拍一张时写的标记，冒号后面是画面描述
PHOTO_MARKER = re.compile(r"[\[【]\s*(?:PHOTO|SELFIE|拍照)\s*[:：]\s*([^\]】]*?)\s*[\]】]", re.I)


def _split_photo_marker(text):
    """把 [PHOTO:画面描述] 从回复里摘出来，返回 (干净文本, 描述)。"""
    if not text:
        return text, ""
    match = PHOTO_MARKER.search(text)
    if not match:
        return text, ""
    desc = (match.group(1) or "").strip()
    return PHOTO_MARKER.sub("", text).strip(), desc


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
        # 生图器，第一次用到时才初始化
        self._image_gen = None

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
            photo_desc = ""

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
                prompt += self._image_hint()

                response = await plugin.generator.generate_response(prompt, ctx=ctx, cards=cards, images=images)
                response = _strip_heart_markers(response)
                response = await plugin.generator.apply_personification(response)
                response = _strip_heart_markers(response)

                if plugin.get_config().get("display_value", False):
                    response += value_game.get_manner_value_str()

                response, photo_desc = _split_photo_marker(response)
                clean = await self.send_with_image(ctx, response)

                response_content = _strip_heart_markers(clean).split("\n")[0]
                await memory.add_short_term_memory("bot", response_content)

            # 画图要几十秒，放在会话锁外面，避免把后面的消息堵住
            await self._maybe_send_generated_image(ctx, text, photo_desc)

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
            photo_desc = ""

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
                prompt += self._image_hint()

                response = await plugin.generator.generate_response(prompt, ctx=ctx, cards=cards, images=images)
                response = _strip_heart_markers(response)
                response = await plugin.generator.apply_personification(response)
                response = _strip_heart_markers(response)

                if plugin.get_config().get("display_value", False):
                    response += value_game.get_manner_value_str()

                response, photo_desc = _split_photo_marker(response)
                clean = await self.send_with_image(ctx, response)

                clean_response = _strip_heart_markers(clean)
                await memory.add_short_term_memory(cards.get_assistant_name(), clean_response)
                await memory.append_group_chat_log(
                    plugin.current_bot_uuid or "bot", cards.get_assistant_name(), clean_response
                )

            # 群里只在模型自己说要拍的时候才画，不按关键词触发，免得刷屏
            if photo_desc:
                await self._maybe_send_generated_image(ctx, "", photo_desc)

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

    # ------------------------------------------------------------------ 发图

    def _image_library(self):
        """图库放在插件目录的 config/images/<类别>/ 下，首次使用时初始化。"""
        lib = getattr(self, "_image_lib", None)
        if lib is None:
            from organs.images import ImageLibrary

            root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            lib = ImageLibrary(os.path.join(root, "config"))
            self._image_lib = lib
        return lib

    def _image_hint(self) -> str:
        """告诉模型：手里有图可以发，或者能现场画一张。"""
        hints = []

        try:
            cats = self._image_library().categories()
        except Exception as e:
            logger.debug(f"读取图库失败: {e}")
            cats = []

        if cats:
            hints.append(
                "\n\n你手边有自己的照片可以发给对方。真的需要发图时，"
                "在回复最后单独一行写 [IMAGE:类别]，系统会自动补一张图。"
                f"可用类别：{'、'.join(cats)}。不需要图就不要写这个标记。"
            )

        try:
            if self._image_generator().enabled():
                hints.append(
                    "\n\n你还能当场拍张新照片发给对方（不是现成的图，是现拍的）。"
                    "当对方想看你、想看你的样子、让你换个动作、换身衣服、去某个地方、"
                    "或者问你现在在干嘛时，你可以真的拍一张：嘴上先自然接一句「等我一下」这类话，"
                    "然后在回复最末尾另起一行写 [PHOTO:画面描述]，描述用中文写清你在做什么动作、"
                    "穿什么衣服、在什么场景、什么光线。"
                    "例如 [PHOTO:坐在窗边托腮，穿米白色针织衫，午后阳光，回头看镜头，笑了一下]。"
                    "对方没提要看你就别写这个标记，不要滥用；也不要说自己没有照片、不能拍照。"
                )
        except Exception as e:
            logger.debug(f"读取生图开关失败: {e}")

        return "".join(hints)

    async def send_with_image(self, ctx, message: str) -> str:
        """发文字；模型要了图就补一张。返回清理过标记的文本。"""
        category = ""
        text = message or ""
        match = IMAGE_MARKER.search(text)
        if match:
            category = (match.group(1) or "").strip()
            text = IMAGE_MARKER.sub("", text).strip()

        if text:
            await self.send_with_typing_effect(ctx, text)

        await self._maybe_send_image(ctx, category)
        return text

    async def _maybe_send_image(self, ctx, category: str) -> None:
        try:
            lib = self._image_library()
        except Exception as e:
            logger.error(f"初始化图库失败: {e}")
            return

        config = self.plugin.get_config() or {}
        gap_seconds = int(config.get("image_min_gap_minutes", 3) or 0) * 60
        if not lib.cooldown_ok(gap_seconds):
            logger.debug("发图冷却中，跳过")
            return

        picked = lib.pick(category)
        if not picked:
            logger.debug("图库里没有可发的图片")
            return

        name, path = picked
        data_uri = lib.to_data_uri(path)
        if not data_uri:
            return

        try:
            await ctx.reply(MessageChain([Image(base64=data_uri, path=path)]))
            lib.mark_sent()
            logger.info(f"已发送图片: 类别={name} 文件={os.path.basename(path)}")
        except Exception as e:
            logger.error(f"发送图片失败: {e}")

    # ------------------------------------------------------------------ 生图

    def _image_generator(self):
        gen = getattr(self, "_image_gen", None)
        if gen is None:
            from organs.image_gen import ImageGenerator

            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            gen = ImageGenerator(self.plugin.get_config, base_dir=root)
            self._image_gen = gen
        return gen

    async def _maybe_send_generated_image(self, ctx, user_text: str, photo_desc: str = "") -> None:
        """模型自己说要拍，或者用户明确要照片时，调 AI 现场画一张发过去。"""
        try:
            gen = self._image_generator()
        except Exception as e:
            logger.error(f"初始化生图器失败: {e}")
            return

        if not gen.enabled():
            return

        # 优先用模型自己写的画面描述，没有再退回用户原话
        desc = (photo_desc or "").strip()
        if not desc:
            if not gen.match_trigger(user_text):
                return
            desc = user_text

        if not gen.cooldown_ok():
            logger.info("生图冷却中，跳过")
            return
        if getattr(self, "_gen_busy", False):
            logger.info("上一张还没画完，这次跳过")
            return

        self._gen_busy = True
        try:
            logger.info(f"开始生图: {desc[:60]}")
            data_uri = ""
            try:
                data_uri = await asyncio.wait_for(gen.generate(desc), timeout=180)
            except asyncio.TimeoutError:
                logger.warning("生图超时，放弃这一张")
            except Exception as e:
                logger.error(f"生成图片失败: {e}", exc_info=True)

            if not data_uri:
                # 画不出来就退回家里的图库，别把话掉在地上
                await self._maybe_send_image(ctx, "")
                return

            tmp_path = ""
            try:
                raw = base64.b64decode(data_uri.split("base64,", 1)[-1])
                fd, tmp_path = tempfile.mkstemp(prefix="waifu_gen_", suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                await ctx.reply(MessageChain([Image(base64=data_uri, path=tmp_path)]))
                gen.mark_sent()
                logger.info(f"已发送 AI 生成图，约 {len(raw) // 1024} KB")
            except Exception as e:
                logger.error(f"发送生成图失败: {e}")
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        finally:
            self._gen_busy = False

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
