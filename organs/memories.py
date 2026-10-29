import json
import logging
import time
from typing import List, Dict, Any, Tuple

logger = logging.getLogger(__name__)

class Memory:
    def __init__(self, plugin):
        self.plugin = plugin
        self.user_id = ""
        self.user_name = ""
        self.bot_name = ""
        self.short_term_memory = []
        self.long_term_memories = []
        self.group_chat_log = []
        self.group_chat_log_max_entries = 5000

        # 最新情感状态（供其他模块使用）
        self.current_emotion_score = 0.0
        self.current_emotion_type = "neutral"

        # 配置参数（initialize 时会用插件配置覆盖）
        self.short_term_memory_size = 2000
        self.retrieve_top_n = 3
        self.summary_max_tags = 30
        self.analyze_max_conversations = 9

        # 记忆权重参数
        self.memory_weight_max = 1.0  # 最大记忆权重
        self.memory_boost_rate = 0.15  # 记忆激活时的权重提升率

        # 情感关键词库
        self.emotion_keywords = {
            "positive": ["喜欢", "爱", "开心", "高兴", "快乐", "幸福", "满足", "兴奋", "感激"],
            "negative": ["不喜欢", "讨厌", "难过", "伤心", "生气", "愤怒", "失望", "焦虑", "害怕"],
            "neutral": ["知道", "了解", "明白", "收到", "好的", "是的", "可以", "谢谢"]
        }

    async def initialize(self, user_name: str, bot_name: str, user_id: str = None):
        """
        初始化记忆系统
        :param user_name: 用户名称
        :param bot_name: 机器人名称
        :param user_id: 用户ID（唯一标识）
        """
        self.user_name = user_name
        self.bot_name = bot_name
        self.user_id = user_id if user_id else user_name  # 如果没有提供ID，使用用户名作为备选

        # 换人（或换群）时先清空短期记忆，避免上一个会话的内容混进来
        self.short_term_memory = []
        self.group_chat_log = []

        # 加载配置
        config = self.plugin.get_config() or {}
        self.short_term_memory_size = config.get("short_term_memory_size", 2000)  # 短期记忆的最大长度
        self.retrieve_top_n = config.get("retrieve_top_n", 3)  # 每次提取的相关长期记忆数量
        self.summary_max_tags = config.get("summary_max_tags", 30)  # 每段长期记忆的最大标签数量
        self.analyze_max_conversations = config.get("analyze_max_conversations", 9)  # 用于生成分析的最大对话数量

        # 加载长期记忆
        await self.load_long_term_memories()

        if self.user_id.startswith("group_"):
            await self.load_group_chat_log()
            for item in self.group_chat_log[-200:]:
                speaker = str(item.get("speaker_name", "") or "")
                content = str(item.get("content", "") or "")
                if speaker and content:
                    self.short_term_memory.append({
                        "speaker": speaker,
                        "content": content,
                        "timestamp": float(item.get("timestamp", time.time()) or time.time()),
                        "emotion_score": 0.0,
                        "emotion_type": "neutral"
                    })

        logger.info("记忆系统初始化完成")
    
    async def save_long_term_memories(self):
        """
        保存长期记忆到文件
        """
        try:
            memory_key = f"long_term_memories_{self.user_id}"
            memory_data = json.dumps(self.long_term_memories, ensure_ascii=False).encode("utf-8")
            await self.plugin.set_plugin_storage(memory_key, memory_data)
            
            logger.info(f"保存了 {len(self.long_term_memories)} 条长期记忆到插件存储: {memory_key}")
        except Exception as e:
            logger.error(f"保存长期记忆失败: {e}", exc_info=True)
            return
    
    async def load_long_term_memories(self):
        """
        加载长期记忆
        """
        try:
            memory_key = f"long_term_memories_{self.user_id}"
            memory_data = await self.plugin.get_plugin_storage(memory_key)
            if memory_data:
                self.long_term_memories = json.loads(memory_data.decode("utf-8"))
                logger.info(f"加载了 {len(self.long_term_memories)} 条长期记忆: {memory_key}")
            else:
                logger.info("没有找到长期记忆，将使用空记忆列表")
                self.long_term_memories = []
        except Exception as e:
            if "Storage with key" in str(e) and "not found" in str(e):
                logger.info("首次使用，尚未创建长期记忆存储")
                self.long_term_memories = []
            else:
                logger.error(f"加载长期记忆失败: {e}", exc_info=True)
                self.long_term_memories = []
    
    async def add_short_term_memory(self, speaker: str, content: str):
        """
        添加短期记忆
        :param speaker: 发言者（user/bot）
        :param content: 发言内容
        """
        # 分析情感倾向
        emotion_score, emotion_type = self._analyze_emotion(content)
        
        # 创建记忆项，包含情感信息
        memory_item = {
            "speaker": speaker,
            "content": content,
            "timestamp": time.time(),
            "emotion_score": emotion_score,
            "emotion_type": emotion_type
        }
        
        self.short_term_memory.append(memory_item)
        
        # 存储最新的情感分析结果，供其他模块使用
        self.current_emotion_score = emotion_score
        self.current_emotion_type = emotion_type
        
        # 检查短期记忆大小，如果超过限制则总结为长期记忆
        total_length = self._calc_short_term_memory_size()
        logger.info(f"当前短期记忆大小: {total_length} 字符, 允许最大值: {self.short_term_memory_size} 字符")
        
        if self.user_id.startswith("group_"):
            if total_length >= self.short_term_memory_size:
                self._drop_short_term_memory(max(50, len(self.short_term_memory) // 2))
            logger.debug(f"添加短期记忆: {speaker} - {content} [情感: {emotion_type}, 分数: {emotion_score:.2f}]")
            return

        should_summarize = False
        # 命中"重要信息"关键词时提前总结，但至少要有两句话，否则总结里只有半句对话
        if (
            speaker == "user"
            and len(self.short_term_memory) >= 2
            and any(keyword in content for keyword in ["穿", "衣服", "颜色", "喜欢", "爱好", "生日", "年龄"])
        ):
            should_summarize = True
        if total_length >= self.short_term_memory_size:
            should_summarize = True

        if should_summarize:
            logger.info("触发长期记忆总结")
            await self.summarize_long_term_memory()
        
        logger.debug(f"添加短期记忆: {speaker} - {content} [情感: {emotion_type}, 分数: {emotion_score:.2f}]")

    async def load_group_chat_log(self):
        try:
            memory_key = f"group_chat_log_{self.user_id}"
            memory_data = await self.plugin.get_plugin_storage(memory_key)
            if memory_data:
                self.group_chat_log = json.loads(memory_data.decode("utf-8"))
            else:
                self.group_chat_log = []
        except Exception as e:
            if "Storage with key" in str(e) and "not found" in str(e):
                self.group_chat_log = []
            else:
                logger.error(f"加载群聊聊天记录失败: {e}", exc_info=True)
                self.group_chat_log = []

    async def append_group_chat_log(self, speaker_id: str, speaker_name: str, content: str):
        if not self.user_id.startswith("group_"):
            return
        item = {
            "speaker_id": str(speaker_id),
            "speaker_name": str(speaker_name),
            "content": str(content),
            "timestamp": time.time(),
        }
        self.group_chat_log.append(item)
        if len(self.group_chat_log) > self.group_chat_log_max_entries:
            self.group_chat_log = self.group_chat_log[-self.group_chat_log_max_entries:]
        try:
            memory_key = f"group_chat_log_{self.user_id}"
            memory_data = json.dumps(self.group_chat_log, ensure_ascii=False).encode("utf-8")
            await self.plugin.set_plugin_storage(memory_key, memory_data)
        except Exception as e:
            logger.error(f"保存群聊聊天记录失败: {e}", exc_info=True)
    
    def _analyze_emotion(self, text: str) -> Tuple[float, str]:
        """
        分析文本的情感倾向
        :param text: 要分析的文本
        :return: (情感分数, 情感类型)
        """
        text_lower = text.lower()
        emotion_counts = {
            "positive": 0,
            "negative": 0,
            "neutral": 0
        }
        
        # 统计各类情感关键词的出现次数
        for emotion, keywords in self.emotion_keywords.items():
            for keyword in keywords:
                emotion_counts[emotion] += text_lower.count(keyword)
        
        # 计算情感分数
        total = sum(emotion_counts.values())
        if total == 0:
            return 0.0, "neutral"
        
        # 正面情感加1分，负面情感减1分
        score = (emotion_counts["positive"] - emotion_counts["negative"]) / total
        
        # 确定情感类型
        if score > 0.3:
            return score, "positive"
        elif score < -0.3:
            return score, "negative"
        else:
            return score, "neutral"
        
    def _calc_short_term_memory_size(self) -> int:
        """
        计算短期记忆的总长度
        :return: 总长度（字符数）
        """
        return sum(len(item["content"]) for item in self.short_term_memory)
    
    async def summarize_long_term_memory(self):
        """
        总结长期记忆
        """
        if not self.short_term_memory:
            return
            
        try:
            # 分析短期记忆的整体情感趋势
            overall_emotion = self._analyze_overall_emotion()
            
            # 使用所有短期记忆进行总结，保留完整上下文
            memory_lines = []
            for item in self.short_term_memory:
                emotion_mark = f" [{item['emotion_type']}]" if item['emotion_type'] != "neutral" else ""
                memory_lines.append(f"{item['speaker']}: {item['content']}{emotion_mark}")
            
            memory_text = "\n".join(memory_lines)
            
            # 使用简单的方式总结长期记忆，避免LLM超时
            # 提取关键词标签
            tags = await self._extract_tags(memory_text)
            
            # 生成简洁的总结：取用户最近说的几句，去重后截断
            user_messages = []
            for item in self.short_term_memory:
                if item['speaker'] != 'user':
                    continue
                content = (item['content'] or '').strip()
                if content and content not in user_messages:
                    user_messages.append(content)
            if not user_messages:
                return

            mentioned = "，".join(user_messages[-5:])[:200]
            summary = f"用户与机器人进行了对话，主要提到了：{mentioned}"
            
            # 如果有情感信息，添加到总结中
            if overall_emotion['score'] != 0:
                summary += f" 对话情感主要是{overall_emotion['type']}。"
            
            # 创建记忆项
            memory_item = {
                "id": str(time.time()),
                "content": summary,
                "tags": tags[:self.summary_max_tags],  # 限制标签数量
                "timestamp": time.time(),
                "emotion_type": overall_emotion['type'],
                "emotion_score": overall_emotion['score'],
                "weight": 0.5
            }
            
            # 添加到长期记忆
            self.long_term_memories.append(memory_item)
            
            # 保存长期记忆
            await self.save_long_term_memories()
            
            # 清空短期记忆，只保留最新的几条
            self.short_term_memory = self.short_term_memory[-5:]
            
            logger.info(f"长期记忆总结完成，添加了新的记忆项")
            return

        except Exception as e:
            logger.error(f"总结长期记忆失败: {e}", exc_info=True)
    
    def _analyze_overall_emotion(self) -> Dict[str, Any]:
        """
        分析短期记忆的整体情感趋势
        :return: {"type": 情感类型, "score": 情感分数}
        """
        if not self.short_term_memory:
            return {"type": "neutral", "score": 0.0}
        
        total_score = sum(item.get("emotion_score", 0.0) for item in self.short_term_memory)
        avg_score = total_score / len(self.short_term_memory)
        
        if avg_score > 0.3:
            return {"type": "positive", "score": avg_score}
        elif avg_score < -0.3:
            return {"type": "negative", "score": avg_score}
        else:
            return {"type": "neutral", "score": avg_score}
    
    def _drop_short_term_memory(self, max_remain: int):
        """
        清理短期记忆，保留指定数量的最新记忆
        :param max_remain: 最多保留的记忆数量
        """
        if len(self.short_term_memory) > max_remain:
            removed_count = len(self.short_term_memory) - max_remain
            removed_items = self.short_term_memory[:removed_count]
            self.short_term_memory = self.short_term_memory[removed_count:]
            logger.info(f"清理了 {removed_count} 条短期记忆，保留了 {len(self.short_term_memory)} 条")
            for item in removed_items:
                logger.debug(f"移除的短期记忆: {item['speaker']} - {item['content'][:50]}...")
    
    async def _extract_tags(self, text: str) -> List[str]:
        """
        从文本中提取关键词标签
        :param text: 文本内容
        :return: 关键词标签列表
        """
        try:
            # 使用text_analyzer提取关键词
            counter, i18n_list, related_list = await self.plugin.text_analyzer.term_freq(text)
            
            # 合并所有可能的标签
            tags = list(counter.keys()) + i18n_list + related_list
            
            # 按词频排序并限制数量
            sorted_tags = sorted(tags, key=lambda x: counter.get(x, 0), reverse=True)
            return sorted_tags[:self.summary_max_tags]
            
        except Exception as e:
            logger.error(f"提取标签失败: {e}", exc_info=True)
            return []
    
    async def retrieve_related_memories(self, query: str) -> List[Dict[str, Any]]:
        """
        检索相关的长期记忆（增强版）
        :param query: 查询内容
        :return: 相关记忆列表
        """
        if not self.long_term_memories:
            return []
        
        try:
            # 分析查询的情感倾向
            query_emotion_score, query_emotion_type = self._analyze_emotion(query)

            # 用和记忆标签同一套切词，中文按空格切是切不出东西的
            query_words = set(self.plugin.text_analyzer.tokens(query))
            if not query_words:
                query_words = set(word.lower() for word in query.split() if word)

            related_memories = []

            for memory in self.long_term_memories:
                memory_content = memory.get("content", "").lower()
                memory_tags = {tag.lower() for tag in memory.get("tags", [])}
                memory_emotion_type = memory.get("emotion_type", "neutral")
                memory_emotion_score = memory.get("emotion_score", 0.0)
                memory_weight = memory.get("weight", 0.5)

                # 内容命中数 / 标签命中数
                content_match_count = sum(1 for word in query_words if word in memory_content)
                tag_match_count = len(query_words & memory_tags)

                # 情感匹配度（相同情感类型加分）
                emotion_match = 1.0 if memory_emotion_type == query_emotion_type else 0.5
                emotion_score_match = 1.0 - abs(memory_emotion_score - query_emotion_score)

                content_weight = 0.3
                tag_weight = 0.5
                emotion_weight = 0.2
                word_count = max(len(query_words), 1)

                base_match = (
                    (content_match_count / word_count) * content_weight
                    + (tag_match_count / word_count) * tag_weight
                    + emotion_match * emotion_score_match * emotion_weight
                )

                # 时间衰减：隔得越久权重越低，但保留 0.1 的底线
                time_delta = time.time() - memory.get("timestamp", 0)
                time_decay = max(0.1, 1 / (1 + (time_delta / (24 * 3600)) * 0.1))

                match_score = base_match * time_decay * memory_weight

                # 穿着类信息是角色扮演里最常被追问的，命中时加分
                if any(keyword in memory_content for keyword in ["穿", "衣服", "颜色"]):
                    match_score += 0.2

                if match_score > 0.05:
                    memory_with_score = memory.copy()
                    memory_with_score["match_score"] = match_score
                    related_memories.append(memory_with_score)

            related_memories.sort(key=lambda x: x["match_score"], reverse=True)

            # 被检索到的记忆加重，下次更容易被想起
            boosted_ids = {item["id"] for item in related_memories[:self.retrieve_top_n]}
            for original_memory in self.long_term_memories:
                if original_memory["id"] in boosted_ids:
                    original_memory["weight"] = min(
                        original_memory.get("weight", 0.5) + self.memory_boost_rate,
                        self.memory_weight_max
                    )

            return related_memories[:self.retrieve_top_n]
            
        except Exception as e:
            logger.error(f"检索相关记忆失败: {e}", exc_info=True)
            return []
    
    def get_short_term_memory_text(self) -> str:
        """
        获取短期记忆文本
        :return: 短期记忆文本
        """
        max_length = 8000 if self.user_id.startswith("group_") else 3000
        memory_text = ""
        
        # 从最新的记忆开始添加
        for memory in reversed(self.short_term_memory):
            memory_line = f"{memory['speaker']}: {memory['content']}\n"
            if len(memory_text) + len(memory_line) > max_length:
                break
            memory_text = memory_line + memory_text
        
        return memory_text.strip()
    
    async def clear_all_memories(self):
        """
        清除所有记忆
        """
        self.short_term_memory = []
        self.long_term_memories = []
        self.group_chat_log = []
        
        # 清除存储的长期记忆
        try:
            memory_key = f"long_term_memories_{self.user_id}"
            await self.plugin.delete_plugin_storage(memory_key)
            if self.user_id.startswith("group_"):
                group_key = f"group_chat_log_{self.user_id}"
                await self.plugin.delete_plugin_storage(group_key)
            logger.info("所有记忆已清除")
        except Exception as e:
            logger.error(f"清除记忆失败: {e}", exc_info=True)
