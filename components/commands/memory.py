import logging
from typing import AsyncGenerator
from langbot_plugin.api.definition.components.command.command import Command
from langbot_plugin.api.entities.builtin.command.context import ExecuteContext
from langbot_plugin.api.entities.builtin.command.context import CommandReturn

logger = logging.getLogger(__name__)

class MemoryCommand(Command):
    def __init__(self):
        super().__init__()
        
        @self.subcommand(
            name="",  # empty string means the root command
            help="查看用户记忆",
            usage="memory",
            aliases=["mem", "view"],
        )
        async def view_memory(self, ctx: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            """查看用户记忆"""
            try:
                logger.info("收到查看记忆命令")
                
                # 获取用户信息
                if hasattr(ctx.query.sender, 'user_id'):
                    user_id = str(ctx.query.sender.user_id)
                    user_name = ctx.query.sender.nickname or f"用户{user_id}"
                else:
                    yield CommandReturn(
                        text="无法获取用户信息"
                    )
                    return
                
                # 获取插件实例
                plugin = self.get_plugin()

                # 命令要和消息用同一个会话上下文，否则查的不是同一份记忆
                conv = await plugin.get_context(user_id)
                async with conv.lock:
                    await conv.ensure_ready(user_id, "Waifu")
                    short_term_memory = conv.memory.get_short_term_memory_text()
                    long_term_count = len(conv.memory.long_term_memories)

                # 构造回复
                response = "短期记忆：\n"
                if short_term_memory:
                    # 只显示最近的10条消息
                    recent_messages = short_term_memory.split("\n")[-10:]
                    response += "\n".join(recent_messages)
                else:
                    response += "暂无短期记忆"

                response += f"\n\n长期记忆数量：{long_term_count}"
                
                # 返回回复
                yield CommandReturn(
                    text=response
                )
                
                logger.info(f"用户 {user_name} 查看了记忆")
                
            except Exception as e:
                logger.error(f"查看记忆失败: {e}", exc_info=True)
                yield CommandReturn(
                    text="查看记忆失败，请稍后重试"
                )
        
        @self.subcommand(
            name="clear",
            help="清除用户记忆",
            usage="memory clear",
            aliases=["c"],
        )
        async def clear_memory(self, ctx: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            """清除用户记忆"""
            try:
                logger.info("收到清除记忆命令")
                
                # 获取用户信息
                if hasattr(ctx.query.sender, 'user_id'):
                    user_id = str(ctx.query.sender.user_id)
                    user_name = ctx.query.sender.nickname or f"用户{user_id}"
                else:
                    yield CommandReturn(
                        text="无法获取用户信息"
                    )
                    return
                
                # 获取插件实例
                plugin = self.get_plugin()

                conv = await plugin.get_context(user_id)
                async with conv.lock:
                    await conv.ensure_ready(user_id, "Waifu")
                    await conv.memory.clear_all_memories()
                
                # 返回回复
                yield CommandReturn(
                    text="你的所有记忆已清除"
                )
                
                logger.info(f"用户 {user_name} 清除了记忆")
                
            except Exception as e:
                logger.error(f"清除记忆失败: {e}", exc_info=True)
                yield CommandReturn(
                    text="清除记忆失败，请稍后重试"
                )
        
        @self.subcommand(
            name="edit",
            help="编辑记忆内容（管理员）",
            usage="memory edit <user_id> <memory_id> <content>",
            aliases=["e"],
        )
        async def edit_memory(self, ctx: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            """编辑记忆内容（管理员）"""
            try:
                logger.info("收到编辑记忆命令")
                
                # 获取插件实例
                plugin = self.get_plugin()
                
                # 检查是否为管理员
                config = plugin.get_config()
                admin_ids = config.get("admin_ids", [])
                user_id = str(ctx.query.sender.user_id)
                
                if user_id not in admin_ids:
                    yield CommandReturn(
                        text="权限不足，只有管理员可以使用此命令"
                    )
                    return
                
                # 获取参数
                args = ctx.query.args
                if len(args) < 3:
                    yield CommandReturn(
                        text="使用方法：/memory edit <user_id> <memory_id> <content>"
                    )
                    return
                
                target_user_id = args[0]
                memory_id = args[1]
                content = " ".join(args[2:])
                
                # 加载目标用户的记忆
                # 注意：这里需要实现加载指定用户记忆的功能
                yield CommandReturn(
                    text="当前版本暂不支持编辑记忆功能"
                )
                
            except Exception as e:
                logger.error(f"编辑记忆失败: {e}", exc_info=True)
                yield CommandReturn(
                    text="编辑记忆失败，请稍后重试"
                )

# 创建命令实例
command = MemoryCommand()
