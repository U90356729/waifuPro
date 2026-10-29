import logging
from typing import AsyncGenerator
from langbot_plugin.api.definition.components.command.command import Command
from langbot_plugin.api.entities.builtin.command.context import ExecuteContext
from langbot_plugin.api.entities.builtin.command.context import CommandReturn

logger = logging.getLogger(__name__)

class AffectionCommand(Command):
    def __init__(self):
        super().__init__()
        
        @self.subcommand(
            name="",  # empty string means the root command
            help="查看当前用户好感度", # command help message
            usage="affection", # command usage example, displayed in the command help message
            aliases=["af", "check"], # command aliases，和 affection.yaml 里声明的 check 保持一致
        )
        async def check_affection(self, ctx: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            """查看当前用户好感度"""
            try:
                logger.info("收到好感度查询命令")
                
                # 获取用户ID
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
                
                # 判断是群聊还是私聊
                is_group = hasattr(ctx.query, 'group')
                if is_group:
                    group_id = str(ctx.query.group.id)
                    launcher_id = f"group_{group_id}_{user_id}"
                else:
                    launcher_id = user_id
                
                # 加载好感度系统
                character = plugin.get_config().get("group_character" if is_group else "character", "default")
                await plugin.value_game.load_config(
                    character=character,
                    launcher_id=launcher_id,
                    launcher_type="group" if is_group else "person"
                )
                
                # 获取好感度
                affection = plugin.value_game.get_value()
                max_value = plugin.value_game.get_max_value()
                description = plugin.value_game.get_manner_description()
                suffix = plugin.value_game.get_manner_value_str()
                
                # 返回回复
                yield CommandReturn(
                    text=f"{user_name}，你的当前心动值是：{affection}/{max_value}\n{suffix}\n{description}"
                )
                
                logger.info(f"用户 {user_name} 查看了好感度: {affection}")
                
            except Exception as e:
                logger.error(f"查看好感度失败: {e}", exc_info=True)
                yield CommandReturn(
                    text="查看好感度失败，请稍后重试"
                )
    
        @self.subcommand(
            name="set",
            help="设置用户好感度（管理员）",
            usage="affection set <user_id> <value>",
            aliases=["s"],
        )
        async def set_affection(self, ctx: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            """设置用户好感度（管理员）"""
            try:
                logger.info("收到设置好感度命令")
                
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
                if len(args) < 2:
                    yield CommandReturn(
                        text="使用方法：/affection set <user_id> <value>"
                    )
                    return
                
                target_user_id = args[0]
                try:
                    value = int(args[1])
                except ValueError:
                    yield CommandReturn(
                        text="好感度必须是整数"
                    )
                    return
                
                # 判断是群聊还是私聊
                is_group = hasattr(ctx.query, 'group')
                if is_group:
                    group_id = str(ctx.query.group.id)
                    launcher_id = f"group_{group_id}_{target_user_id}"
                else:
                    launcher_id = target_user_id
                
                # 加载好感度系统
                character = plugin.get_config().get("group_character" if is_group else "character", "default")
                await plugin.value_game.load_config(
                    character=character,
                    launcher_id=launcher_id,
                    launcher_type="group" if is_group else "person"
                )
                
                # 设置好感度
                await plugin.value_game.change_manner_value(value - plugin.value_game.get_value())
                new_affection = plugin.value_game.get_value()
                max_value = plugin.value_game.get_max_value()
                
                # 返回回复
                yield CommandReturn(
                    text=f"已将用户 {target_user_id} 的好感度设置为：{new_affection}/{max_value}"
                )
                
                logger.info(f"管理员 {user_id} 将用户 {target_user_id} 的好感度设置为: {new_affection}")
                
            except Exception as e:
                logger.error(f"设置好感度失败: {e}", exc_info=True)
                yield CommandReturn(
                    text="设置好感度失败，请稍后重试"
                )
        
        @self.subcommand(
            name="list",
            help="列出所有用户好感度（管理员）",
            usage="affection list",
            aliases=["l"],
        )
        async def list_affection(self, ctx: ExecuteContext) -> AsyncGenerator[CommandReturn, None]:
            """列出所有用户好感度（管理员）"""
            try:
                logger.info("收到列出好感度命令")
                
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
                
                # 由于我们的好感度是按用户ID和场景存储的，这里简化处理
                # 实际实现中，应该从存储中获取所有好感度数据
                yield CommandReturn(
                    text="当前版本暂不支持列出所有用户好感度"
                )
                
            except Exception as e:
                logger.error(f"列出好感度失败: {e}", exc_info=True)
                yield CommandReturn(
                    text="列出好感度失败，请稍后重试"
                )

# 创建命令实例
command = AffectionCommand()
