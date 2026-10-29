import logging
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Tuple

import yaml

logger = logging.getLogger(__name__)

# 切词时用来过滤虚词的字符集，避免产出 "色的""了吗" 这种没用的标签
STOP_CHARS = set("的了着过是在有和与就都也还很太又再被把让给对从向于以其之呢吗吧啊呀哦嗯嘛啦哈"
                 "我你他她它们这那哪个不没要会能可上下里外好")


class TextAnalyzer:
    LOADED_DICTIONARIES = {}

    def __init__(self, plugin):
        self.plugin = plugin

    async def _load_yaml_dict(self, file: str) -> Dict[str, list]:
        """
        Load yaml dictionary file.
        :param file: yaml file path
        """
        if file == "sentiment":
            now = time.time()
            cached = TextAnalyzer.LOADED_DICTIONARIES.get(file)
            cached_ts = float(TextAnalyzer.LOADED_DICTIONARIES.get(f"{file}__ts", 0) or 0)
            if (
                isinstance(cached, dict)
                and (now - cached_ts) < 5
                and "positive_keywords" in cached
                and "negative_keywords" in cached
                and "strong_negative_patterns" in cached
            ):
                return cached
        else:
            if file in TextAnalyzer.LOADED_DICTIONARIES:
                return TextAnalyzer.LOADED_DICTIONARIES[file]

        local_candidates = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", f"{file}.yaml")),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "config", f"{file}.yaml")),
        ]
        for local_path in local_candidates:
            if os.path.exists(local_path):
                try:
                    with open(local_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    TextAnalyzer.LOADED_DICTIONARIES[file] = data
                    if file == "sentiment":
                        TextAnalyzer.LOADED_DICTIONARIES[f"{file}__ts"] = time.time()
                    return data
                except Exception as e:
                    logger.error(f"读取本地配置文件失败: {local_path}, {e}")

        # 兜底：使用LangBot的配置文件API加载（如果可用）
        config_path = f"assets/config/{file}.yaml"
        try:
            file_bytes = await self.plugin.get_config_file(config_path)
            data = yaml.safe_load(file_bytes) or {}
        except Exception as e:
            logger.error(f"加载配置文件 {config_path} 失败: {e}")
            try:
                config_path = f"config/{file}.yaml"
                file_bytes = await self.plugin.get_config_file(config_path)
                data = yaml.safe_load(file_bytes) or {}
            except Exception as e:
                logger.error(f"尝试从 {config_path} 加载也失败: {e}")
                if file == "sentiment":
                    return {"positive_keywords": [], "negative_keywords": [], "strong_negative_patterns": []}
                data = {file: []}

        TextAnalyzer.LOADED_DICTIONARIES[file] = data
        if file == "sentiment":
            TextAnalyzer.LOADED_DICTIONARIES[f"{file}__ts"] = time.time()
        return data

    # ------------------------------------------------------------------ 分词

    def _tokenize(self, text: str) -> List[str]:
        """没有分词库，就用中文二元组（2-gram）+ 英文单词，至少能做出有意义的词频。"""
        tokens: List[str] = []
        for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
            if len(run) == 1:
                continue
            if len(run) == 2:
                tokens.append(run)
                continue
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        tokens.extend(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", text or ""))
        return tokens

    @staticmethod
    def _is_noise(token: str) -> bool:
        if len(token) < 2:
            return True
        return any(ch in STOP_CHARS for ch in token)

    async def term_freq(self, text: str) -> Tuple[Counter, List[str], List[str]]:
        """
        统计词频，供记忆标签使用。
        :param text: text string
        """
        text = await self._remove_meaningless(text or "")
        return Counter(self.tokens(text)), [], []

    def tokens(self, text: str) -> List[str]:
        """对外暴露切词结果，记忆检索也用同一套，避免各处口径不一致。"""
        return [t for t in self._tokenize(text or "") if not self._is_noise(t)]

    # ------------------------------------------------------------------ 情感

    async def sentiment(self, text: str) -> Dict[str, Any]:
        """
        统计正负向关键词命中数。
        :param text: text string
        """
        text = text or ""
        result_dict = {"positive_num": 0, "negative_num": 0, "word_num": 0}

        sentiment_dict = await self._load_yaml_dict("sentiment")
        positive_cfg = sentiment_dict.get("positive_keywords", [])
        negative_cfg = sentiment_dict.get("negative_keywords", [])
        strong_patterns_cfg = sentiment_dict.get("strong_negative_patterns", [])

        if not positive_cfg and not negative_cfg and not strong_patterns_cfg:
            pos_fallback = await self._load_yaml_dict("positive")
            neg_fallback = await self._load_yaml_dict("negative")
            positive_cfg = pos_fallback.get("positive", [])
            negative_cfg = neg_fallback.get("negative", [])
            strong_patterns_cfg = []

        positive_set = {str(x).strip() for x in (positive_cfg or []) if str(x).strip()}
        negative_set = {str(x).strip() for x in (negative_cfg or []) if str(x).strip()}

        pos_hits, neg_hits = self._match_sentiment(text, positive_set, negative_set)

        strong_negative_patterns = []
        for pat in (strong_patterns_cfg or []):
            try:
                strong_negative_patterns.append(re.compile(str(pat), re.IGNORECASE))
            except re.error:
                continue

        # 强负向正则命中按两次负向计，放大脏话/辱骂的惩罚
        for i, pat in enumerate(strong_negative_patterns):
            if pat.search(text):
                neg_hits.add(f"强负向{i}")
                neg_hits.add(f"强负向{i}#2")

        result_dict["positive_num"] = len(pos_hits)
        result_dict["negative_num"] = len(neg_hits)
        result_dict["word_num"] = len(self._tokenize(text))

        logger.debug(
            f"情感分析: 正={sorted(pos_hits)}, 负={sorted(neg_hits)}, "
            f"词典规模={len(positive_set)}/{len(negative_set)}"
        )
        return result_dict

    @staticmethod
    def _match_sentiment(text: str, positive_set: set, negative_set: set):
        """
        最长优先匹配，命中过的片段会被挖掉，
        这样 "爱你" 不会同时算成 "爱" 和 "爱你" 两次命中。
        """
        remaining = text
        pos_hits = set()
        neg_hits = set()

        for word in sorted(negative_set, key=len, reverse=True):
            if word and word in remaining:
                neg_hits.add(word)
                remaining = remaining.replace(word, "\u0000")

        for word in sorted(positive_set, key=len, reverse=True):
            if not word or word not in remaining:
                continue
            if f"不{word}" in text:
                neg_hits.add(f"否定{word}")
            else:
                pos_hits.add(word)
            remaining = remaining.replace(word, "\u0000")

        return pos_hits, neg_hits

    # ------------------------------------------------------------------ 辅助

    async def _remove_meaningless(self, text: str) -> str:
        """
        Remove meaningless words and punctuation from the text.
        :param text: input text
        """
        meaningless_dict = await self._load_yaml_dict("meaningless")
        meaningless = meaningless_dict.get("meaningless", [])

        for word in meaningless:
            if word:
                text = text.replace(word, "")

        return text
