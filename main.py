import logging
import asyncio
from langbot_plugin.api.definition.plugin import BasePlugin

logger = logging.getLogger(__name__)

# 同时保留的会话上下文上限，超过后按最久未用淘汰
MAX_CONTEXTS = 200


class ConversationContext:
    """一个会话（一个私聊用户或一个群）独占的记忆与角色卡。

    以前所有会话共用一份 Memory / Cards，只能靠一把全局锁串行处理；
    现在每个会话各拿一份，锁也只锁自己，用户之间互不影响。
    """

    def __init__(self, plugin, key: str):
        self.plugin = plugin
        self.key = key
        self.memory = None
        self.cards = None
        self.lock = asyncio.Lock()
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    async def ensure_ready(self, user_name: str = "user", bot_name: str = "Waifu"):
        """首次使用时装载记忆和角色卡，之后重复调用是空操作。"""
        if self._ready:
            return self

        from cells.cards import Cards
        from organs.memories import Memory

        self.memory = Memory(self.plugin)
        await self.memory.initialize(user_name, bot_name, self.key)
        self.cards = Cards(self.plugin)
        self._ready = True
        logger.info(f"会话上下文就绪: {self.key}")
        return self


class WaifuBotPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.config = None
        self.text_analyzer = None
        self.config_manager = None
        self.generator = None
        self.thoughts = None
        self.proactive = None
        self.value_game = None
        self.current_bot_uuid = None  # 保存当前机器人的UUID
        self._contexts = {}
        self._contexts_guard = asyncio.Lock()

    async def initialize(self):
        """插件加载时调用"""
        logger.info("WaifuBot插件加载中...")

        # 获取插件配置
        self.config = self.get_config()
        safe_config = self.config.copy() if isinstance(self.config, dict) else {}
        if "api_key" in safe_config:
            safe_config["api_key"] = "***"
        logger.info(f"插件配置已加载: keys={list(safe_config.keys())}")

        # 初始化各个模块
        try:
            import sys
            import os
            sys.path.append(os.path.dirname(os.path.abspath(__file__)))

            from cells.text_analyzer import TextAnalyzer
            from cells.config import ConfigManager
            from cells.generator import Generator
            from organs.thoughts import Thoughts
            from organs.proactive import ProactiveGreeter
            from systems.value_game import ValueGame

            self.text_analyzer = TextAnalyzer(self)
            self.config_manager = ConfigManager(self)
            self.generator = Generator(self)
            self.thoughts = Thoughts(self)
            self.thoughts.initialize()
            self.value_game = ValueGame(self)
            self.proactive = ProactiveGreeter(self, "default")
            self.proactive.initialize()

            logger.info("所有模块初始化完成")

            # 启动主动交互系统
            await self.proactive.load_config()
            await self.proactive.start()

            logger.info("WaifuBot插件加载完成！")

        except Exception as e:
            logger.error(f"模块初始化失败: {e}", exc_info=True)
            raise

    async def get_context(self, key: str) -> ConversationContext:
        """取会话上下文，不存在就建一个空壳（记忆在首次加锁后装载）。"""
        async with self._contexts_guard:
            conv = self._contexts.get(key)
            if conv is None:
                if len(self._contexts) >= MAX_CONTEXTS:
                    oldest = next(iter(self._contexts))
                    self._contexts.pop(oldest, None)
                    logger.info(f"会话上下文超过 {MAX_CONTEXTS} 个，淘汰最久未用的: {oldest}")
                conv = ConversationContext(self, key)
                self._contexts[key] = conv
            return conv

    def peek_memory(self, key: str):
        """不初始化、只读取已有会话的记忆，供主动发言等场景使用。"""
        conv = self._contexts.get(key)
        if conv is None or not conv.ready:
            return None
        return conv.memory

    def peek_context(self, key: str):
        """不初始化、只读取已有会话上下文（memory + cards），供主动发言复用角色卡。"""
        conv = self._contexts.get(key)
        if conv is None or not conv.ready:
            return None
        return conv

    def __del__(self):
        """插件卸载时调用"""
        logger.info("WaifuBot插件卸载中...")
        # 注意：__del__方法不能调用异步方法，数据已在正常操作中保存
        logger.info("WaifuBot插件卸载完成！")


# 创建插件实例
bot = WaifuBotPlugin()

# 导出插件实例，必须使用此变量名
plugin = bot
