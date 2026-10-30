"""AI 生图：拿角色参考图当基准画图，返回 data URI。

只有一条路：必须带参考图。参考图读不到、接口画不出来，就这一轮不出图，
绝不退化成纯文字生图——那样画出来的是另一个人，发出去比不发更糟。

接口按 openai 兼容格式调：先试对话式（Gemini 这类），再试 /images/edits（Grok 这类）。

只依赖标准库，网络请求放到线程里跑，不阻塞事件循环。
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import random
import re
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# 单张图大小上限，QQ 通道发太大的图会被吞
MAX_IMAGE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 120

# 参考图的默认位置（相对插件根目录）
DEFAULT_REFERENCE = os.path.join("config", "reference", "character.jpg")

# 没有指定场景时随机挑一个日常画面，避免每次都是同一张
SCENES = [
    "在房间里趴在桌上，抬眼看镜头，表情有点懒",
    "抱着一条小鱼干坐在窗边吃，嘴角有碎屑",
    "站在阳台吹风，头发被风吹起，回头看镜头",
    "窝在沙发里抱着抱枕，眼睛半睁，像刚睡醒",
]

# 带参考图时用这段：以参照图为准，不硬套画风，免得把半写实照片画成二次元
STYLE_REF = (
    "以参考图里的少女为唯一主角：脸型、五官比例、眼睛形状与颜色、唇形、妆容、肤色、"
    "发型与发色必须和参考图完全一致，长相是同一个人；画风、质感、光影也沿用参考图，"
    "不要改成别的画风。服装默认就穿参考图那一套（深藏青女仆装配白色蕾丝围裙），"
    "但如果下面的画面内容里明确说了换衣服，就按说的穿；头饰和鲸鱼尾巴一直保留。"
    "只改变她的动作、表情、衣着和所在场景。"
)

DEFAULT_TRIGGERS = [
    "看看照片", "看看相片", "发张照片", "发张相片", "发张图",
    "来张照片", "来张图", "看看你", "给我看看", "长什么样", "自拍",
]

# 从各种返回格式里捞出图片数据
_DATA_URI_RE = re.compile(r"data:image/[a-zA-Z+]+;base64,[A-Za-z0-9+/=]+")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+\.(?:png|jpe?g|webp)")


# --------------------------------------------------------------------- 网络

def _http_request(url, payload=None, api_key="", timeout=120, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": "Mozilla/5.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_bytes(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _file_to_data_uri(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    if size <= 0 or size > MAX_IMAGE_BYTES:
        return ""
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        return "data:%s;base64,%s" % (mime, base64.b64encode(f.read()).decode())


class ImageGenerator:
    """按配置调用生图接口。配置每次现读，改完不用重启。"""

    def __init__(self, get_config, base_dir=""):
        self._get_config = get_config
        self._base_dir = base_dir
        self._last_sent = 0.0

    # ------------------------------------------------------------------ 配置

    def _conf(self):
        try:
            return self._get_config() or {}
        except Exception:
            return {}

    def enabled(self):
        return bool(self._conf().get("image_gen_enabled"))

    def _triggers(self):
        raw = self._conf().get("image_gen_trigger_words")
        if isinstance(raw, list) and raw:
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str) and raw.strip():
            return [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        return list(DEFAULT_TRIGGERS)

    def match_trigger(self, text):
        if not text:
            return False
        low = text.lower()
        return any(word.lower() in low for word in self._triggers())

    def cooldown_ok(self):
        minutes = int(self._conf().get("image_gen_cooldown_minutes", 5) or 0)
        return (time.time() - self._last_sent) >= max(0, minutes) * 60

    def mark_sent(self):
        self._last_sent = time.time()

    # ------------------------------------------------------------------ 提示词

    def _strip_triggers(self, text):
        out = text or ""
        for word in self._triggers():
            out = out.replace(word, " ")
        return re.sub(r"[，。！？、,.!?~～\s]+", " ", out).strip()

    def _reference_uri(self):
        rel = str(self._conf().get("image_gen_reference_image") or "").strip() or DEFAULT_REFERENCE
        path = rel if os.path.isabs(rel) else os.path.join(self._base_dir, rel)
        uri = _file_to_data_uri(path)
        if not uri:
            logger.warning(f"[生图] 参考图缺失或不可用：{path}")
        return uri

    def build_prompt(self, user_text):
        style = str(self._conf().get("image_gen_style") or "").strip() or STYLE_REF
        scene = self._strip_triggers(user_text)
        if len(scene) < 2:
            scene = random.choice(SCENES)
        else:
            scene = scene[:120]
        return f"{style}现在的动作和场景：{scene}。"

    # ------------------------------------------------------------------ 生成

    async def generate(self, user_text):
        """返回 data URI；没画出来返回空串。

        只有带参考图这一条路：参考图拿不到、接口没给图，都直接放弃这一张，
        不退成纯文字生图（画出来的人完全不像，用户明确不要这种图）。
        """
        conf = self._conf()
        base = str(conf.get("image_gen_base_url") or "").strip().rstrip("/")
        key = str(conf.get("image_gen_api_key") or "").strip() or str(conf.get("api_key") or "").strip()
        model = str(conf.get("image_gen_model") or "").strip()
        timeout = int(conf.get("image_gen_timeout_seconds", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)

        if not (base and key and model):
            logger.warning("[生图] 接口配置不全：需要地址、密钥、模型名")
            return ""

        ref = self._reference_uri()
        if not ref:
            logger.warning("[生图] 参考图不可用，这一张不生成（免得画出另一个人）")
            return ""

        uri = await self._openai_chain(base, key, model, user_text, timeout, ref)
        if uri:
            return uri

        logger.warning("[生图] 带参考图的接口都没出图，放弃这一张")
        return ""

    # ------------------------------------------------ openai 兼容（必须带参考图）

    def _api_mode(self):
        """请求方式：auto / chat / edits。换模型时这里可能要跟着改。"""
        mode = str(self._conf().get("image_gen_api_mode") or "auto").strip().lower()
        return mode if mode in ("auto", "chat", "edits") else "auto"

    async def _openai_chain(self, base, key, model, user_text, timeout, ref):
        mode = self._api_mode()
        prompt = self.build_prompt(user_text)

        if mode == "chat":
            return await self._chat_with_image(base, key, model, prompt, ref, timeout)
        if mode == "edits":
            return await self._edits_api(base, key, model, prompt, ref, timeout)

        # auto：对话式（Gemini 这类）→ 图像编辑（Grok 这类），两条都带参考图
        uri = await self._chat_with_image(base, key, model, prompt, ref, timeout)
        if uri:
            logger.info("[生图] 出图路径：对话接口 + 参考图")
            return uri

        uri = await self._edits_api(base, key, model, prompt, ref, timeout)
        if uri:
            logger.info("[生图] 出图路径：图像编辑 + 参考图")
            return uri

        return ""

    async def _post_json(self, url, payload, key, timeout):
        try:
            raw = await asyncio.to_thread(_http_request, url, payload, key, timeout)
            return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            logger.warning(f"[生图] {url} 返回 HTTP {e.code}：{detail}")
        except Exception as e:
            logger.warning(f"[生图] {url} 调用失败：{type(e).__name__}: {e}")
        return {}

    async def _edits_api(self, base, key, model, prompt, ref, timeout):
        """图像编辑，把参考图一起送进去，人物才长得像。走 /images/edits。"""
        data = await self._post_json(
            base + "/images/edits",
            {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "image": {"url": ref, "type": "image_url"},
            },
            key, timeout,
        )
        return await self._extract(data, timeout) if data else ""

    async def _chat_with_image(self, base, key, model, prompt, ref, timeout):
        content = [{"type": "text", "text": prompt}]
        if ref:
            content.append({"type": "image_url", "image_url": {"url": ref}})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content if ref else prompt}],
        }
        if ref:
            payload["modalities"] = ["image", "text"]
        data = await self._post_json(base + "/chat/completions", payload, key, timeout)
        return await self._extract(data, timeout) if data else ""

    # ------------------------------------------------------------------ 解析

    async def _extract(self, data, timeout):
        candidates = []
        for item in data.get("data") or []:
            if not isinstance(item, dict):
                continue
            if item.get("b64_json"):
                candidates.append(("b64", item["b64_json"]))
            url = item.get("url")
            if isinstance(url, str) and url.startswith("data:"):
                candidates.append(("b64", url.split("base64,", 1)[-1]))
            elif url:
                candidates.append(("url", url))

        for choice in data.get("choices") or []:
            message = (choice or {}).get("message") or {}
            for image in message.get("images") or []:
                if not isinstance(image, dict):
                    continue
                url = (image.get("image_url") or {}).get("url") or image.get("url") or ""
                if not url and image.get("b64_json"):
                    candidates.append(("b64", image["b64_json"]))
                elif isinstance(url, str) and url.startswith("data:"):
                    candidates.append(("b64", url.split("base64,", 1)[-1]))
                elif url:
                    candidates.append(("url", url))
            content = message.get("content")
            if isinstance(content, str) and content:
                for uri in _DATA_URI_RE.findall(content):
                    candidates.append(("b64", uri.split("base64,", 1)[1]))
                for url in _MD_IMAGE_RE.findall(content) + _URL_RE.findall(content):
                    candidates.append(("url", url))
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    raw = part.get("image_url")
                    url = raw.get("url") if isinstance(raw, dict) else raw
                    if isinstance(url, str) and url:
                        if url.startswith("data:image"):
                            candidates.append(("b64", url.split("base64,", 1)[1]))
                        else:
                            candidates.append(("url", url))

        for kind, value in candidates:
            raw = self._decode_b64(value) if kind == "b64" else await self._download(value, timeout)
            wrapped = self._wrap(raw)
            if wrapped:
                return wrapped

        logger.warning("[生图] 返回里没找到可用图片")
        # 接口通了却不给图，多半是内容被审核挡了：模型只回一段文字。
        # 把这句摘出来，日志里一眼能看出是被拒还是格式没解析对。
        for choice in data.get("choices") or []:
            text = ((choice or {}).get("message") or {}).get("content")
            if isinstance(text, str) and text.strip():
                logger.warning(f"[生图] 接口返回的文字：{text.strip()[:120]}")
                break
        return ""

    def _wrap(self, raw):
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            return ""
        return "data:image/" + self._guess_mime(raw) + ";base64," + base64.b64encode(raw).decode()

    @staticmethod
    def _decode_b64(value):
        try:
            text = str(value).strip()
            if text.startswith("data:"):
                text = text.split("base64,", 1)[-1]
            return base64.b64decode(text, validate=False)
        except Exception:
            return b""

    async def _download(self, url, timeout):
        try:
            return await asyncio.to_thread(_http_get_bytes, url, timeout)
        except Exception as e:
            logger.warning(f"[生图] 下载生成图失败：{type(e).__name__}: {e}")
            return b""

    @staticmethod
    def _guess_mime(raw):
        if raw[:3] == b"\xff\xd8\xff":
            return "jpeg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "webp"
        if raw[:3] == b"GIF":
            return "gif"
        return "jpeg"
