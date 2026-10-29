import asyncio
import logging
import random

logger = logging.getLogger(__name__)

DEFAULT_MAX_PROMPT_CHARS = 3000


class Generator:
    def __init__(self, plugin):
        self.plugin = plugin

    async def generate_response(self, prompt: str, llm_model_uuid: str = None, ctx=None, cards=None,
                                system_prompt: str = None, images: list = None) -> str:
        """
        生成回复内容
        :param prompt: LLM提示
        :param llm_model_uuid: 指定模型UUID，不传则按 插件配置/角色卡/可用模型 顺序解析
        :param ctx: 事件上下文（保留参数，便于以后按流水线取模型）
        :param cards: 当前会话的角色卡，用来读角色卡里配置的模型
        :param system_prompt: 自定义 system 提示，传入时优先使用（主动消息用它注入人设）
        :param images: 用户随消息发来的图片，[{base64|url}]，有值时按多模态发给模型
        :return: 生成的回复
        """
        try:
            from langbot_plugin.api.entities.builtin.provider.message import Message

            prompt = self._clip_prompt(prompt)
            config = self.plugin.get_config() or {}

            # 自定义接口模式：地址、模型名、Key 全用插件配置，不经过 LangBot 的模型管理
            use_custom = bool(config.get("use_custom_api")) and bool(
                str(config.get("custom_model_name") or "").strip()
            )

            if not use_custom:
                if not llm_model_uuid:
                    llm_model_uuid = self._model_from_config(cards)
                if not llm_model_uuid:
                    llm_model_uuid = await self._pick_available_model()
                if not llm_model_uuid:
                    logger.error("没有可用的LLM模型，无法生成回复")
                    return "抱歉，我现在无法生成回复。"

            # 添加系统提示，明确限制回复长度
            if system_prompt:
                system_content = system_prompt
            else:
                system_content = "你是一个有情感的聊天机器人，用自然、友好的语言回复用户。"
                system_content += "回复要简洁，一般情况下不超过50个汉字。"
                system_content += "只有在非常激动、非常开心，或者非常难受、非常难过的情况下，可以到达100个汉字。"

            user_content = self._build_user_content(prompt, images)
            messages = [
                Message(role="system", content=system_content),
                Message(role="user", content=user_content)
            ]

            if use_custom:
                logger.info(f"使用自定义接口: {config.get('custom_api_base')} / {config.get('custom_model_name')}")
                # [patched 2026-10-28] 超时 60s -> 120s，失败/超时各重试一次
                raw = None
                last_err = None
                for _attempt in range(2):
                    try:
                        raw = await asyncio.wait_for(
                            self._call_custom_api(messages, config),
                            timeout=120,
                        )
                        break
                    except Exception as _e:
                        last_err = _e
                        logger.error(
                            f"自定义接口调用失败(第 {_attempt + 1}/2 次): "
                            f"{type(_e).__name__}: {_e}"
                        )
                        if _attempt == 0:
                            await asyncio.sleep(1.5)
                if raw is None:
                    logger.error(f"自定义接口两次均未成功，返回兜底文案: {last_err}")
                    return "抱歉，我现在无法生成回复。"
                return self._strip_think(raw)

            extra_args = self._build_extra_args(config)

            # 带重试的调用，应对框架层瞬时超时
            max_retries = 3
            retry_delay = 2
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    response = await self.plugin.invoke_llm(
                        llm_model_uuid=llm_model_uuid,
                        messages=messages,
                        extra_args=extra_args
                    )
                    break
                except Exception as e:
                    last_exc = e
                    err_str = str(e).lower()
                    if ('timed out' in err_str or 'timeout' in err_str) and attempt < max_retries:
                        logger.warning(
                            f'invoke_llm 超时 (第 {attempt + 1}/{max_retries + 1} 次)，{retry_delay}s 后重试'
                        )
                        await asyncio.sleep(retry_delay)
                        continue
                    raise
            else:
                raise last_exc

            result = self._strip_think(response.content or "")

            logger.debug(f"LLM回复: {result}")
            return result

        except Exception as e:
            logger.error(f"生成回复失败: {e}", exc_info=True)
            return "抱歉，我现在无法生成回复。"

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _generation_args(config) -> dict:
        """前端填的生成参数；填 0 或非法值就不传，交给模型自带设置。"""
        args = {}

        temperature = config.get("temperature", 0.8)
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool) and temperature > 0:
            args["temperature"] = float(temperature)

        max_tokens = config.get("max_tokens", 512)
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            args["max_tokens"] = max_tokens

        return args

    def _build_extra_args(self, config) -> dict:
        """走 LangBot 模型时的调用参数。"""
        extra_args = {"timeout": 30}
        extra_args.update(self._generation_args(config))

        # 只有显式填了才覆盖，否则沿用 LangBot 模型供应商里的 Key
        api_key = config.get("api_key", "")
        if api_key:
            extra_args["api_key"] = api_key
        return extra_args

    @staticmethod
    def _build_user_content(prompt: str, images: list = None):
        """有图片就组装成多模态 content，没有就还是纯文本。"""
        if not images:
            return prompt

        from langbot_plugin.api.entities.builtin.provider.message import (
            ContentElement,
            ImageURLContentObject,
        )

        parts = [ContentElement(type="text", text=prompt or "看看这张图。")]
        for item in images[:3]:
            if isinstance(item, str):
                url = item
                b64 = ""
            else:
                url = str(item.get("url") or "").strip()
                b64 = str(item.get("base64") or "").strip()

            if b64:
                if not b64.startswith("data:"):
                    b64 = "data:image/jpeg;base64," + b64
                parts.append(
                    ContentElement(type="image_url", image_url=ImageURLContentObject(url=b64))
                )
            elif url:
                parts.append(
                    ContentElement(type="image_url", image_url=ImageURLContentObject(url=url))
                )

        if len(parts) == 1:
            return prompt
        logger.info(f"本次请求附带图片 {len(parts) - 1} 张")
        return parts

    @staticmethod
    def _content_to_payload(content):
        """把 Message.content 转成 OpenAI 兼容接口能吃的结构。"""
        if isinstance(content, str) or content is None:
            return content

        parts = []
        for elem in content:
            kind = getattr(elem, "type", None)
            if kind == "text":
                parts.append({"type": "text", "text": getattr(elem, "text", "") or ""})
            elif kind == "image_url":
                obj = getattr(elem, "image_url", None)
                url = getattr(obj, "url", None) if obj is not None else None
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                b64 = getattr(elem, "image_base64", None)
                if b64:
                    parts.append({"type": "image_url", "image_url": {"url": b64}})
        return parts

    @staticmethod
    def _strip_think(result: str) -> str:
        """去掉 <think> 思考段，只留正文。"""
        if "<think>" in result and "</think>" in result:
            return result.split("</think>")[-1].strip()
        return result

    async def _call_custom_api(self, messages, config) -> str:
        """自定义接口模式：直接请求 OpenAI 兼容接口，不经过 LangBot 的模型管理。"""
        import json
        import urllib.request

        base = str(config.get("custom_api_base") or "").strip().rstrip("/")
        model_name = str(config.get("custom_model_name") or "").strip()
        api_key = str(config.get("api_key") or "").strip()

        if not base:
            raise ValueError("自定义接口地址为空")

        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": m.role, "content": self._content_to_payload(m.content)}
                for m in messages
            ],
        }
        payload.update(self._generation_args(config))

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        def _post():
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=50) as response:
                return json.loads(response.read().decode("utf-8"))

        data = await asyncio.to_thread(_post)
        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"接口没有返回 choices: {str(data)[:200]}")
        return choices[0].get("message", {}).get("content") or ""

    def _model_from_config(self, cards=None) -> str:
        """插件配置优先（前端选了就以它为准），角色卡里的值只作为默认值。"""
        model_uuid = (self.plugin.get_config() or {}).get("llm_model_uuid", "")
        if model_uuid:
            logger.info(f"使用插件配置的模型: {model_uuid}")
            return model_uuid

        if cards is not None:
            model_uuid = (cards.get_additional_keys() or {}).get("llm_model_uuid", "")
            if model_uuid:
                logger.info(f"使用角色卡配置的模型: {model_uuid}")
                return model_uuid
        return ""

    async def _pick_available_model(self) -> str:
        """配置里没写模型时，从可用模型里挑一个（优先最近创建的）。"""
        try:
            llm_models = await self.plugin.get_llm_models()
        except Exception as e:
            logger.error(f"获取LLM模型列表失败: {e}")
            return ""

        if not llm_models or not isinstance(llm_models, list):
            logger.error(f"没有可用的LLM模型: {llm_models}")
            return ""

        if isinstance(llm_models[0], dict):
            latest_model = None
            latest_time = None
            for model in llm_models:
                created_at = model.get('created_at', '')
                if created_at and (not latest_time or created_at > latest_time):
                    latest_time = created_at
                    latest_model = model
            model_info = latest_model or llm_models[0]
            model_uuid = model_info.get('uuid') or model_info.get('id') or ""
            logger.info(f"自动选择模型: {model_info.get('name')} ({model_uuid})")
            return model_uuid

        logger.info(f"自动选择模型: {llm_models[0]}")
        return str(llm_models[0])

    def _clip_prompt(self, prompt: str) -> str:
        """提示词过长时保留开头（角色设定）和结尾（回复要求），砍掉中间的历史对话。"""
        max_chars = int((self.plugin.get_config() or {}).get("max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS) or 0)
        if max_chars <= 0:
            max_chars = DEFAULT_MAX_PROMPT_CHARS
        if len(prompt) <= max_chars:
            return prompt

        head_len = int(max_chars * 0.6)
        tail_len = max_chars - head_len
        logger.debug(f"提示词过长({len(prompt)}字符)，裁剪至 {max_chars} 字符")
        return prompt[:head_len] + "\n……\n" + prompt[-tail_len:]

    async def apply_personification(self, response: str) -> str:
        """
        应用拟人化效果
        :param response: 原始回复
        :return: 拟人化后的回复
        """
        config = self.plugin.get_config() or {}
        bracket_rate = config.get("bracket_rate", [0.1, 0.1])
        if not isinstance(bracket_rate, (list, tuple)) or len(bracket_rate) < 2:
            bracket_rate = [0.1, 0.1]

        if random.random() < bracket_rate[0]:
            brackets = ["（笑）", "（开心）", "（思考）", "（点头）", "（眨眼）"]
            response += random.choice(brackets)
        elif random.random() < bracket_rate[1]:
            brackets = ["（", "（嗯...", "（思考中", "（犹豫", "（开心"]
            response += random.choice(brackets)

        return response
