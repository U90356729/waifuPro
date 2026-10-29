import logging
from typing import Tuple
import sys
import os

# 添加插件根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))))

logger = logging.getLogger(__name__)

class Thoughts:
    """思维系统"""
    def __init__(self, plugin):
        self.plugin = plugin
        self._generator = None

    def initialize(self):
        """
        初始化思维系统
        """
        from cells.generator import Generator
        self._generator = Generator(self.plugin)

    def _analysis_enabled(self) -> bool:
        """思考分析会额外调用一次模型，默认关闭，由配置开关控制。"""
        return bool((self.plugin.get_config() or {}).get("enable_thinking_analysis", False))

    async def _analyze_person_conversations(self, memory, profile: str, background: str, manner: str) -> str:
        """
        分析私信对话
        :param memory: 记忆系统实例
        :param profile: 角色简介
        :param background: 角色背景
        :param manner: 行为准则
        :return: 分析结果
        """
        if not hasattr(memory, 'short_term_memory') or not hasattr(memory, 'analyze_max_conversations'):
            return ""
            
        conversations = memory.short_term_memory[-memory.analyze_max_conversations:]
        if not conversations:
            return ""
            
        # 简化版的对话分析
        last_message = conversations[-1]
        last_content = last_message.get('content', '')
        last_speaker = last_message.get('speaker', '')
        
        # 获取最大思考字数配置
        max_thinking_words = self.plugin.get_config().get("max_thinking_words", 30)
        
        # 构建分析提示
        user_prompt = f"分析用户说的话：'{last_content}'，站在助手的角度思考用户的意图。"
        if profile:
            user_prompt += f" 角色设定：{profile}"
        if background:
            user_prompt += f" 背景信息：{background}"
        if manner:
            user_prompt += f" 行为准则：{manner}"
        user_prompt += f" 分析结果要简明扼要，不超过{max_thinking_words}个字。"
        
        # 调用生成器生成分析结果
        analysis_result = await self._generator.generate_response(user_prompt)
        return analysis_result.strip()

    async def generate_person_prompt(self, memory, card, attitude_prompt: str = "") -> Tuple[str, str]:
        """
        生成私信提示
        :param memory: 记忆系统实例
        :param card: 角色卡实例
        :return: 提示和分析结果
        """
        if not hasattr(memory, 'short_term_memory'):
            return "", ""
            
        conversations = memory.short_term_memory
        if not conversations:
            return "", ""
            
        # 获取最近的对话内容（使用更多的短期记忆）
        recent_conversations = conversations[-10:]  # 最近10条消息
        conversation_str = "\n".join([f"{msg['speaker']}: {msg['content']}" for msg in recent_conversations])
        
        # 获取最后一条用户消息作为查询，检索相关的长期记忆
        last_user_message = ""
        for msg in reversed(conversations):
            if msg['speaker'] == 'user':
                last_user_message = msg['content']
                break
        
        # 检索相关的长期记忆
        if last_user_message and hasattr(memory, 'retrieve_related_memories'):
            related_memories = await memory.retrieve_related_memories(last_user_message)
            if related_memories:
                # 添加相关的长期记忆到对话历史
                conversation_str += "\n\n相关记忆："
                for mem in related_memories[:5]:  # 最多添加5条相关记忆
                    conversation_str += f"\n- {mem['content']}"
        
        # 获取角色信息
        profile = " ".join(card.get_profile(mode="person"))
        background = " ".join(card.get_background(mode="person"))
        manner = " ".join(card.get_rules(mode="person"))
        
        # 生成分析
        analysis = ""
        if self._analysis_enabled():
            analysis = await self._analyze_person_conversations(memory, profile, background, manner)
            logger.debug(f"对话分析结果: {analysis}")
        
        # 构建最终提示
        prompt = f"角色设定：{profile} {background}\n"
        prompt += f"行为准则：{manner}\n"
        if attitude_prompt:
            prompt += f"当前语气要求：{attitude_prompt}\n"
        prompt += "回复内容不要包含任何心动值/好感度数值，也不要输出类似（数字❤️/🖤）的格式，系统会自动追加。\n"
        prompt += f"对话历史：\n{conversation_str}\n"
        if analysis:
            prompt += f"思考分析：{analysis}\n"
        prompt += f"请以{card.get_assistant_name()}的身份回复用户。"
        
        return prompt, analysis

    async def generate_group_prompt(self, memory, card, attitude_prompt: str = "") -> Tuple[str, str]:
        """
        生成群聊提示
        :param memory: 记忆系统实例
        :param card: 角色卡实例
        :return: 提示和分析结果
        """
        if not hasattr(memory, 'short_term_memory'):
            return "", ""
            
        conversations = memory.short_term_memory
        if not conversations:
            return "", ""
            
        # 获取最近的对话内容
        recent_conversations = conversations[-20:]  # 最近20条消息
        conversation_str = "\n".join([f"{msg['speaker']}: {msg['content']}" for msg in recent_conversations])
        
        # 获取角色信息
        profile = " ".join(card.get_profile(mode="group"))
        background = " ".join(card.get_background(mode="group"))
        manner = " ".join(card.get_rules(mode="group"))
        
        # 构建群聊提示
        prompt = f"角色设定：{profile} {background}\n"
        prompt += f"行为准则：{manner}\n"
        prompt += "你现在在群聊中，需要保持友好、得体的发言风格。\n"
        prompt += "回答要简洁明了，避免过于私人化的内容。\n"
        prompt += "如果有人@你，要礼貌回应；如果是群聊氛围活跃，可以适当参与讨论。\n"
        if attitude_prompt:
            prompt += f"当前语气要求：{attitude_prompt}\n"
        prompt += "回复内容不要包含任何心动值/好感度数值，也不要输出类似（数字❤️/🖤）的格式，系统会自动追加。\n"
        prompt += f"对话历史：\n{conversation_str}\n"
        prompt += f"请以{card.get_assistant_name()}的身份回复用户。"
        
        return prompt, ""
