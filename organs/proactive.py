import asyncio
import contextvars
import datetime
import json
import logging
import os
import random
import re
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

# 发送前清掉表情和特殊符号，部分平台对这类字符有限制
EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0001F000-\U0001F0FF]"
)
SQLITE_FALLBACK_PATH = os.environ.get("LANGBOT_DB_PATH", "/app/data/langbot.db")

# 插件进程的时区可能不是东八区（实测为 UTC），所以时间一律显式按北京时间算，
# 不能直接用 datetime.now()，否则勿扰判断和不活跃时长都会差 8 小时。
CN_TZ = datetime.timezone(datetime.timedelta(hours=8))


def _now_cn() -> datetime.datetime:
    """当前北京时间（带时区，跨环境都准确）。"""
    return datetime.datetime.now(CN_TZ)


# 两条主动消息之间的硬性最小间隔（分钟）的兜底默认值。
# 实际取值来自配置项 proactive_min_gap_minutes，配错了就用这个值。
# 就算把「不活跃阈值」调到 1 分钟，也不会一分钟一条 —— 那种频率平台会当成骚扰，
# 接口照样返回成功，但消息不会真的送达。
# 业务口径：每小时最多一条，所以默认间隔定 60 分钟。
MIN_PROACTIVE_GAP_MINUTES = 60.0
DEFAULT_MIN_GAP_MINUTES = 60.0


class ProactiveGreeter:
    """用户长时间不活跃时，主动发一条消息。"""

    def __init__(self, plugin, launcher_id: str = "default"):
        self.plugin = plugin
        self._generator = None
        self._main_task: Optional[asyncio.Task] = None
        self._launcher_id = launcher_id
        self._config_loaded = False
        self._retry_task = None
        # 插件配置是按「每次调用」临时绑定的，后台任务里读不到。
        # 所以在处理消息时把配置存一份快照，后台循环只认这份快照。
        self._captured_config = {}
        self._warned_no_config = False
        self._last_skip_reason = None

        self._proactive_target_user_id = "off"
        self._proactive_greeting_enabled = False
        self._proactive_greeting_probability = 0
        self._proactive_min_inactive_minutes = 180.0
        self._proactive_do_not_disturb_start = "23:00"
        self._proactive_do_not_disturb_end = "08:00"
        self._loop_time = 60
        # 上次真正发出主动消息的时间。两轮之间必须拉开间隔，
        # 否则每轮都满足条件，会把同一个人连续刷屏（微信会直接静默投递）。
        self._last_proactive_sent_at = 0.0
        self._min_gap_minutes = MIN_PROACTIVE_GAP_MINUTES
        # 目标用户实际是通过哪个机器人说话的。主动消息必须走同一条通道，
        # 否则会把 QQ 用户塞给微信机器人发。
        self._target_bot_uuid = None

    def initialize(self):
        """初始化主动交互系统"""
        from cells.generator import Generator
        self._generator = Generator(self.plugin)

    def absorb_config(self, config=None) -> bool:
        """在处理消息时把插件配置存成快照。

        插件配置只在一次调用期间可见，离开调用上下文就变成空字典。
        所以必须趁处理消息的时候存一份，后台的主动循环才有得用。
        """
        try:
            cfg = dict(config) if config is not None else dict(self.plugin.get_config() or {})
        except Exception as e:
            logger.warning(f"读取插件配置失败: {e}")
            return False
        if not cfg:
            return False
        self._captured_config = cfg
        logger.info(
            "[主动诊断] 消息里收到配置: 启用={} 阈值={}分钟 间隔={}秒 勿扰={}-{} 目标={}".format(
                cfg.get("proactive_greeting_enabled"),
                cfg.get("proactive_min_inactive_minutes"),
                cfg.get("loop_time"),
                cfg.get("proactive_do_not_disturb_start"),
                cfg.get("proactive_do_not_disturb_end"),
                cfg.get("proactive_target_user_id"),
            )
        )
        return True

    async def load_config(self):
        """加载配置。

        优先用消息处理期间存下来的快照；没有快照才尝试实时读取和数据库兜底。
        插件刚加载时这三条路都是空的，等用户说第一句话后快照就有了。
        """
        config = dict(self._captured_config or {})
        if not config:
            try:
                config = dict(self.plugin.get_config() or {})
            except Exception:
                config = {}
        if not config:
            try:
                config = dict(getattr(self.plugin, "config", None) or {})
            except Exception:
                config = {}
        if not config:
            config = await asyncio.to_thread(self._read_config_from_db)

        self._config_loaded = bool(config)
        if not config:
            if not self._warned_no_config:
                self._warned_no_config = True
                logger.warning("暂时读不到插件配置，主动交互先待机，等用户说一句话就会自动生效")
            return
        self._warned_no_config = False

        self._proactive_target_user_id = str(config.get("proactive_target_user_id", "off") or "off")
        self._proactive_greeting_enabled = bool(config.get("proactive_greeting_enabled", False))
        self._proactive_greeting_probability = int(config.get("proactive_greeting_probability", 0) or 0)

        # 兼容旧配置：早先的单位是小时
        if "proactive_min_inactive_minutes" in config:
            self._proactive_min_inactive_minutes = float(config.get("proactive_min_inactive_minutes", 180) or 180)
        else:
            self._proactive_min_inactive_minutes = float(config.get("proactive_min_inactive_hours", 3.0) or 3.0) * 60

        self._proactive_do_not_disturb_start = str(config.get("proactive_do_not_disturb_start", "23:00") or "23:00")
        self._proactive_do_not_disturb_end = str(config.get("proactive_do_not_disturb_end", "08:00") or "08:00")
        self._loop_time = max(10, int(config.get("loop_time", 60) or 60))

        # 两条主动消息之间的最小间隔，独立于「不活跃阈值」，防止连续刷屏。
        # 注意：实测 LangBot 推给插件的配置里可能不带 proactive_min_gap_minutes，
        # 这时只能靠这里的默认值兜底，所以默认值直接定成业务想要的 60 分钟。
        gap_raw = config.get("proactive_min_gap_minutes")
        if gap_raw in (None, ""):
            self._min_gap_minutes = DEFAULT_MIN_GAP_MINUTES
            logger.warning(
                f"配置未提供 proactive_min_gap_minutes，按默认 {DEFAULT_MIN_GAP_MINUTES:.0f} 分钟执行"
            )
        else:
            try:
                self._min_gap_minutes = max(1.0, float(gap_raw))
            except (TypeError, ValueError):
                self._min_gap_minutes = DEFAULT_MIN_GAP_MINUTES

        # 联调开关：存在 /tmp/waifu_fast_test 时，阈值和间隔都压到 1 分钟，
        # 方便立刻验证主动问候，不用等真实配置的 30/120 分钟。删掉该文件即恢复。
        try:
            if os.path.exists("/tmp/waifu_fast_test"):
                self._proactive_min_inactive_minutes = 1.0
                self._min_gap_minutes = 1.0
                logger.warning("[联调模式] 主动问候阈值与最小间隔强制为 1 分钟（/tmp/waifu_fast_test 生效中）")
        except Exception:
            pass

        snapshot = (
            self._proactive_greeting_enabled,
            self._proactive_target_user_id,
            self._proactive_greeting_probability,
            self._proactive_min_inactive_minutes,
            self._min_gap_minutes,
            self._proactive_do_not_disturb_start,
            self._proactive_do_not_disturb_end,
            self._loop_time,
        )
        if snapshot != getattr(self, "_config_snapshot", None):
            self._config_snapshot = snapshot
            logger.info(
                "主动交互配置加载完成: 启用={}, 目标={}, 概率={}, 不活跃阈值={}分钟, 最小间隔={}分钟, 循环={}秒".format(
                    self._proactive_greeting_enabled,
                    self._proactive_target_user_id,
                    self._proactive_greeting_probability,
                    self._proactive_min_inactive_minutes,
                    self._min_gap_minutes,
                    self._loop_time,
                )
            )

    @staticmethod
    def _read_config_from_db() -> dict:
        """兜底：直接从 LangBot 的 sqlite 里读插件配置。"""
        try:
            if not os.path.exists(SQLITE_FALLBACK_PATH):
                return {}
            conn = sqlite3.connect(SQLITE_FALLBACK_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT config FROM plugin_settings WHERE plugin_name='waifu_bot'")
                row = cursor.fetchone()
            finally:
                conn.close()
            if row:
                data = json.loads(row[0])
                logger.info(f"从数据库直接读取配置成功: keys={list(data.keys())}")
                return data
        except Exception as e:
            logger.warning(f"从数据库读取配置失败: {e}")
        return {}

    async def start(self):
        """启动主动交互循环。

        配置暂时读不到也照常起循环：循环每轮都会重新读配置，
        等第一条消息把配置快照存进来，下一轮就自动生效，不用重启插件。
        """
        if not self._config_loaded:
            logger.info("插件配置暂不可读，主动交互循环先待机，配置到手后自动开始工作")
        self.ensure_started()

    def ensure_started(self):
        """确保主动循环在跑，重复调用无副作用。"""
        if self._main_task is not None and not self._main_task.done():
            return self._main_task

        # 必须用干净的上下文创建任务：如果在消息处理过程中创建，
        # 任务会继承那次调用的上下文，等这次调用结束，里面的一切调用都会报
        # RuntimeError: Plugin invocation has ended。
        try:
            loop = asyncio.get_running_loop()
            self._main_task = loop.create_task(
                self._proactive_loop(), context=contextvars.Context()
            )
        except TypeError:
            # Python 3.10 及更早不支持 context 参数
            self._main_task = asyncio.create_task(self._proactive_loop())
        logger.info("启动主动交互循环")
        return self._main_task

    def remember_bot_for_target(self, user_id, bot_uuid):
        """记住目标用户是用哪个机器人说话的。

        一个实例下可能挂多个机器人（微信、QQ 等），主动消息必须走用户
        真正在用的那条通道，不能顺手拿「最近收到消息的机器人」去发。
        """
        if not user_id or not bot_uuid:
            return
        target = str(self._proactive_target_user_id or "").strip()
        if target and str(user_id) == target:
            self._target_bot_uuid = str(bot_uuid)

    async def stop(self):
        """停止主动交互循环"""
        if getattr(self, "_retry_task", None) is not None and not self._retry_task.done():
            self._retry_task.cancel()
            self._retry_task = None
        if self._main_task is not None and not self._main_task.done():
            logger.info("停止主动交互循环")
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass
            self._main_task = None

    async def _proactive_loop(self):
        tick = 0
        while True:
            try:
                await asyncio.sleep(self._loop_time)
                tick += 1
                # 每轮都重读配置：前端改了开关/目标/概率，不用重载插件就能生效
                await self.load_config()
                logger.info(
                    "[主动诊断] 第{}轮 {} 快照={} 阈值={}分钟 间隔={}秒 勿扰={}-{} 当前允许={}".format(
                        tick,
                        _now_cn().strftime("%H:%M:%S"),
                        "有" if self._config_loaded else "无",
                        self._proactive_min_inactive_minutes,
                        self._loop_time,
                        self._proactive_do_not_disturb_start,
                        self._proactive_do_not_disturb_end,
                        self._is_in_allowed_time_window(),
                    )
                )
                if await self._should_send_proactive_message():
                    await self._send_proactive_message()
                    self._last_skip_reason = None
            except asyncio.CancelledError:
                logger.info("主动交互循环被取消")
                break
            except Exception as e:
                logger.error(f"主动交互循环出错: {e}", exc_info=True)
                await asyncio.sleep(self._loop_time)

    def _note_skip(self, reason: str, detail: str):
        """同一个原因只记一次 info，避免每分钟刷屏；原因变了再记一次。"""
        if self._last_skip_reason == reason:
            logger.debug(detail)
            return
        self._last_skip_reason = reason
        logger.info(detail)

    async def _should_send_proactive_message(self) -> bool:
        if not self._config_loaded:
            self._note_skip("no_config", "主动交互待机：还没拿到插件配置（等用户说一句话）")
            return False

        if not self._proactive_greeting_enabled:
            self._note_skip("disabled", "主动交互未启动：配置里的「主动发言」没有打开")
            return False

        target = str(self._proactive_target_user_id or "").strip()
        for prefix in ("person_", "user_", "friend_"):
            if target.lower().startswith(prefix) and len(target) > len(prefix):
                target = target[len(prefix):].strip()
                break
        if target in ("", "off"):
            self._note_skip("no_target", "主动交互未启动：目标用户ID还是空的或 off")
            return False
        self._proactive_target_user_id = target

        if not self._is_in_allowed_time_window():
            self._note_skip("dnd", "本轮不发：现在处于勿扰时间段内")
            return False

        # 距上次主动发言太近就直接跳过，避免一轮接一轮地刷屏
        if self._last_proactive_sent_at > 0:
            since_last = (_now_cn().timestamp() - self._last_proactive_sent_at) / 60
            if since_last < self._min_gap_minutes:
                self._note_skip(
                    f"gap_{int(since_last)}",
                    f"本轮不发：距上条主动消息才 {since_last:.0f} 分钟，"
                    f"至少要隔 {self._min_gap_minutes:.0f} 分钟",
                )
                return False

        if not await self._check_inactivity_time():
            return False

        random_num = random.randint(1, 100)
        if random_num > self._proactive_greeting_probability:
            self._note_skip(
                "probability",
                f"本轮不发：概率没过（随机数 {random_num} > 阈值 {self._proactive_greeting_probability}）",
            )
            return False

        return True

    def _is_in_allowed_time_window(self) -> bool:
        """勿扰时间段内不发消息，跨天的情况也处理了。"""
        now = _now_cn().time()
        try:
            start_hour, start_minute = map(int, self._proactive_do_not_disturb_start.split(":"))
            end_hour, end_minute = map(int, self._proactive_do_not_disturb_end.split(":"))
            start_time = datetime.time(start_hour, start_minute)
            end_time = datetime.time(end_hour, end_minute)
        except (ValueError, AttributeError):
            logger.warning("勿扰时间格式不正确，按不限制处理")
            return True

        if start_time == end_time:
            return True

        if start_time < end_time:
            return not (start_time <= now < end_time)
        return not (now >= start_time or now < end_time)

    async def _check_inactivity_time(self) -> bool:
        """看目标用户最后一次说话到现在隔了多久。"""
        memory = self.plugin.peek_memory(self._proactive_target_user_id)
        if memory is None or not memory.short_term_memory:
            self._note_skip(
                "no_memory",
                f"本轮不发：找不到 {self._proactive_target_user_id} 的会话记录"
                "（插件本次启动后这个用户还没私聊过，或者ID对不上）",
            )
            return False

        last_time = float(memory.short_term_memory[-1].get('timestamp', 0) or 0)
        if last_time <= 0:
            self._note_skip("no_timestamp", "本轮不发：会话里没有可用的时间戳")
            return False

        # 自己发过的主动消息也算一次「打扰」：用户不回话就一遍遍发，是最典型的刷屏。
        if self._last_proactive_sent_at > last_time:
            last_time = self._last_proactive_sent_at

        inactive_minutes = (_now_cn().timestamp() - last_time) / 60
        if inactive_minutes < self._proactive_min_inactive_minutes:
            self._note_skip(
                f"inactive_{int(inactive_minutes)}",
                f"本轮不发：才安静 {inactive_minutes:.0f} 分钟，"
                f"要满 {self._proactive_min_inactive_minutes:.0f} 分钟才发",
            )
            return False
        return True

    async def _send_proactive_message(self):
        try:
            bot_uuid = await self._resolve_bot_uuid()
            if not bot_uuid:
                logger.error("找不到可用的机器人UUID")
                return

            message_content = await self._generate_proactive_message()
            if not message_content:
                logger.error("生成主动消息失败")
                return

            from langbot_plugin.api.entities.builtin.platform.message import MessageChain, Plain

            simple_message = EMOJI_PATTERN.sub("", message_content).strip()
            if not simple_message:
                simple_message = message_content

            # 只有真正发出去才记账：发送失败（例如机器人掉线 ApiNotAvailable）
            # 必须回滚时间戳，否则一次失败会把后面两小时全堵死，用户看到的就是「不主动」。
            prev_sent_at = self._last_proactive_sent_at

            try:
                await self.plugin.send_message(
                    bot_uuid=bot_uuid,
                    target_type="person",
                    target_id=str(self._proactive_target_user_id),
                    message_chain=MessageChain([Plain(text=simple_message)])
                )
            except Exception as send_err:
                self._last_proactive_sent_at = prev_sent_at
                err_name = type(send_err).__name__
                if 'ApiNotAvailable' in err_name or 'ApiNotAvailable' in str(send_err):
                    logger.error(
                        f"主动消息发送失败：机器人 {bot_uuid} 的通道未连接（大概率是 QQ/微信 掉线），"
                        f"本次不计入发送记录，等通道恢复后会重试"
                    )
                else:
                    logger.error(f"主动消息发送失败（不计入发送记录）: {send_err}")
                raise

            self._last_proactive_sent_at = _now_cn().timestamp()
            logger.info(f"发送主动消息给用户 {self._proactive_target_user_id}: {simple_message}")

        except Exception as e:
            logger.error(f"发送主动消息失败: {e}", exc_info=True)

    async def _resolve_bot_uuid(self) -> str:
        """优先用目标用户实际说话的那个机器人，其次才用最近收到消息的机器人。"""
        bots = []
        try:
            bots = await self.plugin.get_bots()
        except Exception as e:
            logger.error(f"获取机器人列表失败: {e}")

        known = set()
        if isinstance(bots, list):
            for bot in bots:
                bot_id = bot.get('uuid') if isinstance(bot, dict) else bot
                if bot_id:
                    known.add(str(bot_id))

        for candidate, label in (
            (self._target_bot_uuid, "目标用户当前使用的"),
            (getattr(self.plugin, "current_bot_uuid", None), "最近收到消息的"),
        ):
            if not candidate:
                continue
            if known and str(candidate) not in known:
                logger.warning(f"{label}机器人 {candidate} 不在机器人列表里，换下一个")
                continue
            return str(candidate)

        if isinstance(bots, list) and bots:
            # 目标 ID 是纯数字（QQ 号）时，优先挑 QQ / OneBot 适配器的机器人，
            # 否则可能拿微信机器人去发给 QQ 号，直接 ApiNotAvailable。
            target = str(self._proactive_target_user_id or "")
            if target.isdigit():
                for bot in bots:
                    if not isinstance(bot, dict):
                        continue
                    adapter = str(bot.get('adapter') or '').lower()
                    if 'qq' in adapter or 'onebot' in adapter or 'napcat' in adapter:
                        logger.info(f"目标 {target} 是 QQ 号，改用 {adapter} 机器人发送")
                        return str(bot.get('uuid') or bot.get('id') or '')

            first = bots[0]
            if isinstance(first, dict):
                return str(first.get('uuid') or first.get('id') or "")
            return str(first)

        return ""

    async def _build_proactive_prompt(self):
        """拼装带角色卡与记忆的主动消息提示，拿不到上下文时返回 None。"""
        target = str(self._proactive_target_user_id or "")
        if not target or target == "off":
            return None

        peek = getattr(self.plugin, "peek_context", None)
        conv = peek(target) if peek else None
        if conv is None:
            logger.warning("目标用户还没有会话上下文，主动消息拿不到角色卡和记忆")
            return None

        memory = getattr(conv, "memory", None)
        cards = getattr(conv, "cards", None)
        if memory is None or cards is None:
            return None

        # 角色卡：优先用户映射，其次默认私聊角色卡
        character = ""
        try:
            cfg = dict(self._captured_config or {})
            mappings = cfg.get("user_character_mappings") or ""
            if isinstance(mappings, str) and mappings.strip():
                mappings = json.loads(mappings)
            if isinstance(mappings, dict):
                character = str(mappings.get(target) or "")
            if not character:
                character = str(cfg.get("character") or "")
        except Exception as e:
            logger.warning(f"主动消息读取角色配置失败: {e}")

        try:
            await cards.load_config(character, "person")
        except Exception as e:
            logger.warning(f"主动消息加载角色卡失败: {e}")

        short = list(getattr(memory, "short_term_memory", None) or [])
        history = []
        for msg in short[-10:]:
            content = str((msg or {}).get("content") or "").strip()
            if content:
                history.append(f"{msg.get('speaker')}: {content}")

        last_user_text = ""
        for msg in reversed(short):
            if (msg or {}).get("speaker") == "user":
                last_user_text = str(msg.get("content") or "")
                break

        related = []
        if last_user_text and hasattr(memory, "retrieve_related_memories"):
            try:
                related = await memory.retrieve_related_memories(last_user_text) or []
            except Exception as e:
                logger.warning(f"主动消息检索长期记忆失败: {e}")

        try:
            profile = " ".join(cards.get_profile(mode="person"))
            background = " ".join(cards.get_background(mode="person"))
            manner = " ".join(cards.get_rules(mode="person"))
            assistant_name = cards.get_assistant_name()
        except Exception as e:
            logger.warning(f"主动消息读取人设失败: {e}")
            return None

        inactive_minutes = 0
        if short:
            try:
                last_ts = float((short[-1] or {}).get("timestamp", 0) or 0)
                if last_ts > 0:
                    inactive_minutes = max(0, int((_now_cn().timestamp() - last_ts) // 60))
            except Exception:
                inactive_minutes = 0

        now = _now_cn()
        prompt = f"角色设定：{profile} {background}\n"
        prompt += f"行为准则：{manner}\n"
        if history:
            prompt += "最近对话：\n" + "\n".join(history) + "\n"
        if related:
            items = []
            for mem in related[:5]:
                if isinstance(mem, dict):
                    text = str(mem.get("content") or "").strip()
                    if text:
                        items.append(f"- {text}")
            if items:
                prompt += "你记得的事：\n" + "\n".join(items) + "\n"
        prompt += (
            f"当前情境：现在是{now.strftime('%m月%d日 %H:%M')}（北京时间），"
            f"对方已经 {inactive_minutes} 分钟没有说话了。\n"
            f"请以{assistant_name}的身份主动发一条消息把话题接起来：顺着最近聊过的事、"
            "对方提过的细节，或者你记得的事情开口，语气要符合角色人设；"
            "不要抱怨对方不回消息，不要重复之前已经发过的内容，不要连着追问；"
            "不超过50个字，纯文本，不要表情符号、颜文字或括号里的动作描写。"
        )
        system_prompt = (
            f"你就是{assistant_name}，请始终以这个身份的说话习惯、语气和关系边界来交流，"
            "不要跳出角色，也不要自称AI。回复简短口语化，不超过50个字，只要正文。"
        )
        return prompt, cards, system_prompt

    async def _generate_proactive_message(self) -> str:
        """生成主动消息内容：优先带角色卡与记忆，拿不到上下文时退回通用问候。"""
        built = None
        try:
            built = await self._build_proactive_prompt()
        except Exception as e:
            logger.error(f"拼装主动消息提示失败: {e}", exc_info=True)

        if built:
            prompt, cards, system_prompt = built
            logger.info(f"主动消息已注入角色卡与记忆，目标={self._proactive_target_user_id}")
            message = await self._generator.generate_response(
                prompt, cards=cards, system_prompt=system_prompt
            )
        else:
            logger.warning("主动消息缺少角色卡/记忆上下文，退回通用问候")
            prompt = "生成一条友好的主动问候消息，用于开启对话。消息要自然、亲切，不超过50个字，不要使用表情符号或特殊格式，使用纯文本即可。"
            message = await self._generator.generate_response(prompt)

        text = (message or "").strip()
        # 生成器在 LLM 调用失败时返回兜底文案，这种不能当问候发出去，
        # 否则用户收到的是「抱歉，我现在无法生成回复。」这种机器故障话。
        if not text or "抱歉，我现在无法生成回复" in text:
            logger.warning(f"主动消息生成失败，本轮不发送（返回内容: {text!r}）")
            return ""
        return text
