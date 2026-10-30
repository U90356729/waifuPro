# WaifuBot · 情感陪聊插件

一个运行在 [LangBot](https://langbot.app/) 上的聊天机器人插件。它不只是接一句回一句：有人格设定、会记事、会攒好感度，还会在你半天不理它的时候自己来找你说话。

支持私聊与群聊，角色人格由 YAML 角色卡定义，不写代码也能换人设。

- 插件标识：`waifu_bot`
- 当前版本：`2.2.1`
- 依赖：`pyyaml`（见 `requirements.txt`）

---

## 特性

- **角色卡人格** —— 用一份 YAML 定义名字、性格、背景、说话规则、开场白，私聊与群聊可各用一张卡。
- **记忆系统** —— 短期对话窗口 + 长期记忆。长期记忆会自动提取关键词标签，聊天时按相关性检索注入提示词。
- **好感度（心动值）** —— 根据对话内容与情绪关键词增减，长期不互动会自然衰减。数值高低会反过来影响语气：低了冷淡，高了亲昵。
- **主动发言** —— 用户一段时间没说话时，机器人可以自己发起对话。支持概率、冷却间隔、勿扰时段，避免半夜骚扰或连续刷屏。
- **图片理解** —— 用户发来的图片会随消息一起交给模型，能看图说话（需要模型本身支持视觉）。
- **AI 生图** —— 想让机器人"拍张照"时，它可以按角色参考图现场生成一张发过来，能换动作、换衣服、换场景，长相保持一致（需要支持参考图的图像模型）。
- **思考分析（可选）** —— 回复前先过一轮意图分析，默认关闭，开启后每条消息多一次模型调用。
- **拟人化细节** —— 可配置的括号语气词、分段发送、打字停顿、群聊复读。
- **管理命令** —— 查好感度、查记忆、清记忆、改记忆。

---

## 快速开始

### 1. 安装

在 LangBot 的插件市场安装 `WaifuBot`，或把本仓库内容放到 LangBot 的插件目录：

```
<LangBot 数据目录>/plugins/<发布者>__waifu_bot/
```

放好后在 LangBot 网页端「插件 → 已安装」中重载插件。

### 2. 选择模型

网页端 → 插件 → WaifuBot → 配置 → **对话模型**，从下拉里选一个已配置的模型。

留空时的回退顺序：角色卡里的 `llm_model_uuid` → 当前最新的可用模型。

> 如果用的是中转站 + OpenAI 兼容接口，模型名需要带 `openai/` 前缀（如 `openai/grok-4.5`），否则底层会报「未提供 LLM Provider」。详见「已知限制」。

### 3. 开始聊天

私聊直接说话即可。群聊中默认需要 @ 或符合触发条件，可在 LangBot 的流水线配置里调整。

---

## 配置项

全部配置都在 LangBot 网页端完成，保存即生效；只有增删字段（改 `manifest.yaml` 结构）才需要重载插件。

### 模型与接口

- `llm_model_uuid` —— 对话模型。留空则回退到角色卡或最新可用模型。
- `temperature` —— 回复随机性，默认 `0.8`；填 `0` 表示不干预，用模型自带参数。
- `max_tokens` —— 单次回复最大长度，默认 `512`。
- `api_key` —— 可选，仅当本插件要单独用另一个 Key 时才填；一般留空。
- `use_custom_api` —— 打开后完全绕开 LangBot 的模型配置，直连 OpenAI 兼容接口。
- `custom_api_base` —— 自定义接口地址，会自动补 `/chat/completions`。
- `custom_model_name` —— 自定义接口下的模型名。

### 角色卡

- `character` —— 私聊使用的角色卡名，对应 `config/cards/<name>.yaml`；填 `off` 关闭。
- `group_character` —— 群聊使用的角色卡名。
- `user_character_mappings` —— 按用户 ID 指定角色卡，JSON 对象，例如 `{"ou_xxx":"warm_brother"}`。

> 用户 ID 的获取方式：让用户发一条消息，然后看运行时日志里的 `用户 ou_xxx 使用角色卡: ...`。

### 对话与提示词

- `ignore_prefix` —— 忽略这些前缀开头的消息，避免把命令当闲聊处理。
- `analyze_max_conversations` —— 意图分析最多回看多少条对话。
- `max_thinking_words` —— 分析结果的最大字数。
- `enable_thinking_analysis` —— 是否开启回复前的意图分析，默认关闭。
- `max_prompt_chars` —— 提示词长度上限，超出会截断中间部分，保留角色设定与回复要求。

### 记忆

- `short_term_memory_size` —— 短期记忆字符上限。
- `retrieve_top_n` —— 长期记忆每次检索注入的条数。
- `summary_max_tags` —— 每条长期记忆最多提取多少关键词标签。

### 表现细节

- `repeat_trigger` —— 群聊复读触发阈值，`0` 关闭。
- `bracket_rate` —— 回复末尾追加括号语气词的概率。
- `display_value` —— 是否在回复末尾显示好感度数值。

### 主动发言

- `proactive_greeting_enabled` —— 总开关。
- `proactive_target_user_id` —— 主动消息发给谁。
- `proactive_greeting_probability` —— 触发概率，`0`–`100`。
- `proactive_min_inactive_minutes` —— 用户安静满多少分钟才可能触发。
- `proactive_min_gap_minutes` —— 两条主动消息之间的最小间隔，防止连续刷屏。
- `proactive_do_not_disturb_start` / `proactive_do_not_disturb_end` —— 勿扰时段，格式 `HH:MM`。
- `loop_time` —— 后台检查循环的间隔秒数。

### AI 生图

- `image_gen_enabled` —— 总开关，默认关闭。
- `image_gen_reference_image` —— 参考图路径，相对插件根目录，默认 `config/reference/character.jpg`。这是硬性要求：读不到就不出图。
- `image_gen_base_url` —— OpenAI 兼容的图像接口地址，填到 `/v1` 为止。
- `image_gen_api_key` —— 生图专用密钥，留空则复用对话的 `api_key`。
- `image_gen_model` —— 图像模型名，必须填接口真正支持的模型。
- `image_gen_api_mode` —— `auto` 先试对话接口再试 `/images/edits`，两条都带参考图；`chat` / `edits` 强制走其中一种。
- `image_gen_cooldown_minutes` —— 两次生成之间的最小间隔，防止连续刷图烧额度。
- `image_gen_timeout_seconds` —— 单张图的等待上限，超时就这一轮不发。
- `image_gen_style` —— 固定的人物外形描述，留空则用内置的「以参考图为准」那段。
- `image_gen_trigger_words` —— 兜底触发词，模型没领会意图时按这些词判断。

### 权限

- `admin_ids` —— 管理员用户 ID 列表，用于执行管理类命令（如改他人记忆）。

---

## 角色卡

角色卡放在 `config/cards/` 下，文件名即卡名。改完保存即生效，下一条消息就会用新设定，不需要重启。

仓库自带 4 张示例卡：

- `default.yaml` —— 通用助手
- `cute_neko.yaml` —— 猫娘
- `lively_assistant.yaml` —— 活泼助手
- `warm_brother.yaml` —— 温柔兄长

### 最小可用示例

```yaml
user_name: 你          # 如何称呼用户
assistant_name: 小鱼    # 角色名
language: 简体中文

Profile:
  - 你是一条刚学会说话的小鱼，对人类世界很好奇
  - 说话简短，喜欢用问句

Rules:
  - 回复不超过两句话
  - 不确定的事情就直说不懂
```

### 完整字段

- `user_name` —— 对用户的称呼
- `assistant_name` —— 角色名
- `language` —— 对话语言，建议 `简体中文`
- `Profile` —— 角色设定（列表）
- `Skills` —— 技能（列表，可选）
- `Background` —— 背景故事（列表，可选）
- `Rules` —— 行为规则（列表，可选）
- `Prologue` —— 开场场景（列表，可选）
- `Background_group` / `Rules_group` —— 群聊专用设定（可选）
- `max_manner_change` —— 单次好感度最大变化量（可选）
- `value_descriptions` —— 按好感度区间描述角色表现（可选）
- `llm_model_uuid` —— 这张卡指定使用的模型（可选）

> 角色卡内容每轮都会发给模型。写进露骨或违规描述，可能被服务端内容审核拦截，表现为机器人一直回兜底话术。

---

## 命令

- `/affection`（别名 `af`、`check`）—— 查看当前好感度
- `/affection set <用户> <数值>` —— 设置好感度
- `/affection list` —— 列出好感度记录
- `/memory` —— 查看当前用户的记忆
- `/memory clear` —— 清除记忆
- `/memory edit <user_id> <memory_id> <content>` —— 修改指定记忆（管理员）

---

## 图片理解

用户发图时，插件会把图片放进多模态消息一起交给模型，最多 3 张。

- 单张图片 base64 超过 4MB 会被跳过，表现是机器人只回文字、不提图片。
- 只有支持视觉输入的模型才能看图；纯文本模型会忽略图片。
- 图片按 token 计费，比纯文字贵。

---

## AI 生图

打开 `image_gen_enabled` 之后，机器人可以"当场拍一张照片"发给对方。

**怎么触发**

默认交给模型自己判断。跟它说"想看看你"、"你在干嘛呢"、"换件衣服看看"，它会先回一句"等我一下"这类话，然后在回复末尾写一行 `[PHOTO:画面描述]`，插件把这个标记摘掉，拿描述去画图，画完补发。用户看不到标记本身。

模型没领会时，还有一层兜底：消息里出现 `image_gen_trigger_words` 里的词（默认"发张图"、"看看你"这类），也会直接出图，画面取用户原话里除触发词之外的部分。

群里只在模型自己写标记时才画，不按关键词触发，免得刷屏。

**参考图是关键**

要让人物每次长得一样，接口必须支持"带参考图生成"。把角色形象照放到 `image_gen_reference_image`（默认 `config/reference/character.jpg`），插件每次都会连图一起提交，并提示模型"以参考图里的人为唯一主角，只改动作、表情、衣着和场景"。

参考图不入库，需要自己放。不放也能出图，但每次生成的脸都不一样。

**接口要求**

需要 OpenAI 兼容的图像接口，且模型支持带参考图出图。实测可用的是 `gemini-3.1-flash-image-preview`，单张十几到二十秒。`image_gen_api_mode` 保持 `auto` 即可，插件会先试对话接口（多数图像模型走这条），失败再试 `/images/edits`，两条都画不出来就这一轮不发。

生图不接受不带参考图的降级：纯文字生图画出的人跟角色对不上，比不发更糟。同理，生成失败时也不会拿本地图库的存货顶上——图库只在模型主动写 `[IMAGE:类别]` 时才用。

**注意**

- 生成一张图要十几秒，期间不阻塞别的消息。
- 冷却默认 5 分钟，连续要图只会发一张，按需调小。
- 接口本身可能有内容审核，被拦时退回家里的图库发一张存货。

---

## 项目结构

```
main.py                     插件入口，装配各模块
manifest.yaml               插件清单与配置项定义
cells/
  generator.py              调用模型生成回复（含拟人化、多模态）
  cards.py                  角色卡读取与缓存
  config.py                 配置加载
  text_analyzer.py          文本分析工具
organs/
  memories.py               短期/长期记忆
  thoughts.py               意图分析与提示词组装
  proactive.py              主动发言后台循环
  image_gen.py              AI 生图（参考图 + 付费/免费两条后端）
  images.py                 本地图库（按类别发存货）
systems/
  value_game.py             好感度（心动值）系统
components/
  event_listener/           消息事件处理（私聊/群聊分流）
  commands/                 affection、memory 命令
config/
  cards/                    角色卡
  reference/                角色参考图（自备，不入库）
  sentiment.yaml            好感度关键词与正则
  positive.yaml / negative.yaml / meaningless.yaml   文本分类词表
assets/icon.svg             插件图标
```

---

## 本仓库的改动

本仓库基于上游 `WaifuBot 2.1.2` 继续维护，主要修复与增强：

- **主动发言可用性修复** —— 原实现从插件加载期读配置，后台任务拿不到值。改为在消息处理时保存配置快照，后台循环只读快照；后台任务使用独立的清洁上下文启动，不再继承已结束的调用上下文。
- **时区修正** —— 原实现用进程本地时间判断勿扰时段，容器内为 UTC，导致下午被误判成深夜。统一改为北京时间。
- **主动消息冷却** —— 新增 `proactive_min_gap_minutes`，防止后台循环每轮都发一条，触发平台频率限制。
- **目标用户通道匹配** —— 按目标用户实际所在的机器人/适配器选择发送通道，避免用错账号。
- **模型名兼容** —— 修正 OpenAI 兼容接口下模型名缺少 `openai/` 前缀导致调用失败的问题。
- **图片理解** —— 新增多模态支持，消息中的图片随提示词一起提交给模型。
- **AI 生图** —— 新增 `organs/image_gen.py`，支持带角色参考图的图像生成；模型自己写画面描述，可换动作、衣服、场景。附本地图库 `organs/images.py`，模型写 `[IMAGE:类别]` 时发存货。

---

## 已知限制

- **模型名前缀**：LangBot 只在 openrouter 场景下自动补 provider 前缀。用中转站接 OpenAI 兼容接口时，模型名必须写成 `openai/<模型名>`，否则会报「LLM Provider NOT provided」或 `Unknown error`。
- **内容审核**：部分中转站对请求做关键词审核，命中会直接返回 500，LangBot 不识别这个错误类型，统一显示为 `Unknown error`，表现是机器人回兜底话术。角色卡与长期记忆都会进入请求体，任意一处命中都会触发。
- **主动消息频率**：平台对主动推送限制比回复更严。间隔调得太短，消息可能返回成功但不投递。
- **升级会覆盖改动**：插件重新安装或升级会更换代码目录，本仓库中的手工修改会丢失，需要重新部署。
- **记忆常驻内存**：长期记忆在插件启动时一次性载入。直接改数据库后必须重启插件运行时，否则插件下次保存会把内存里的旧内容写回。

---

## 开发

### 安装插件 CLI

```bash
pip install langbot-plugin          # 或 uv tool install langbot-plugin
lbp --help
```

### 打包成安装包

在插件根目录（有 `manifest.yaml` 的那一层）执行：

```bash
lbp build -o dist
# 等价写法
python -m langbot_plugin.cli build -o dist
```

产物是 `dist/<作者>-<插件名>-<版本>.lbpkg`，本质是一个 zip，可以直接在 LangBot 网页端「插件 → 安装插件 → 上传文件」里安装。

版本号取自 `manifest.yaml` 的 `metadata.version`，发布新版本时记得先改它。

### 本地语法校验

```bash
python -m compileall .
```

### 发布到插件市场

需要先有 [LangBot Space](https://space.langbot.app) 账号（用 GitHub 授权登录）。

登录 CLI，两种方式任选：

```bash
# 方式一：个人访问令牌（推荐）
# 在 Space 头像 → Account Settings → Personal Access Tokens 生成
lbp login --token lbpat_xxxxxxxx

# 方式二：设备码
lbp login
# 按提示打开 https://space.langbot.app/auth/device 输入验证码
```

然后在插件根目录发布：

```bash
lbp publish
```

`publish` 会自动打包并上传，成功后到 [space.langbot.app/market](https://space.langbot.app/market) 补充截图与介绍，提交审核。审核通过后才会出现在市场里。

---

## 致谢

原始插件作者：`Liujiajiajia01`。运行平台：[LangBot](https://langbot.app/)。
