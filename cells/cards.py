import logging
from typing import Dict, List, Any
import sys
import os

# 添加插件根目录到Python路径
sys.path.append(os.path.dirname(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))))

logger = logging.getLogger(__name__)

class Cards:
    def __init__(self, plugin):
        self.plugin = plugin
        self._user_name = "user"
        self._assistant_name = "assistant"
        self._language = ""
        self._profile = []
        self._skills = []
        self._background = []
        self._output_format = []
        self._rules = []
        self._manner = ""
        self._memories = []
        self._prologue = ""
        self._additional_keys = {}
        self._has_preset = True

    async def load_config(self, character: str, launcher_type: str):
        """
        加载角色卡配置
        :param character: 角色名称
        :param launcher_type: 启动类型（person/group）
        """
        if character == "off":
            self._has_preset = False
            return
        self._has_preset = True

        from cells.config import ConfigManager
        config = ConfigManager(self.plugin)
        try:
            character_config = await config.load_config(
                character=character,
                launcher_type=launcher_type,
                completion=False
            )
        except Exception as e:
            # 角色卡写错或文件缺失时不要连带消息一起挂掉，退回无预设模式继续回复
            logger.error(f"角色卡 {character} 加载失败，改用无预设模式: {e}")
            self._has_preset = False
            return
        
        self._user_name = character_config.get("user_name", "用户")
        self._assistant_name = character_config.get("assistant_name", "助手")
        self._language = character_config.get("language", "简体中文")
        self._profile = character_config.get("Profile", [])
        if isinstance(self._profile, list) and self._assistant_name != "助手":
            self._profile = [f"你是{self._assistant_name}。"] + self._profile
        self._skills = character_config.get("Skills", [])
        self._background = character_config.get("Background", [])
        if isinstance(self._background, list) and launcher_type == "person" and self._assistant_name != "助手" and self._user_name != "用户":
            self._background = self._background + [f"你是{self._assistant_name}，用户是{self._user_name}。"]
        self._rules = character_config.get("Rules", [])
        self._rules_group = character_config.get("Rules_group", [])
        self._background_group = character_config.get("Background_group", [])
        self._profile_group = character_config.get("Profile_group", [])
        self._prologue = character_config.get("Prologue", "")

        # 收集额外的配置项
        predefined_keys = {
            "user_name", "assistant_name", "language", "Profile", "Skills", "Background", "Rules", "Prologue",
            "Profile_group", "Background_group", "Rules_group", "max_manner_change", "value_descriptions"
        }
        self._additional_keys = {key: value for key, value in character_config.items() if key not in predefined_keys}
        
        logger.info(f"角色卡 {character} 加载完成")

    def get_skills(self) -> List[str]:
        """
        获取角色技能
        """
        return self._skills

    def get_profile(self, mode="person") -> List[str]:
        """
        获取角色简介
        """
        if mode == "group" and hasattr(self, '_profile_group') and self._profile_group:
            return self._profile_group
        return self._profile

    def get_background(self, mode="person") -> List[str]:
        """
        获取角色背景
        """
        if mode == "group" and hasattr(self, '_background_group') and self._background_group:
            return self._background_group
        return self._background

    def get_rules(self, mode="person") -> List[str]:
        """
        获取角色规则
        """
        if mode == "group" and hasattr(self, '_rules_group') and self._rules_group:
            return self._rules_group
        return self._rules

    def get_prologue(self) -> str:
        """
        获取开场场景
        """
        return self._prologue

    def get_user_name(self) -> str:
        """
        获取用户名
        """
        return self._user_name

    def get_assistant_name(self) -> str:
        """
        获取助手名
        """
        return self._assistant_name

    def get_language(self) -> str:
        """
        获取语言
        """
        return self._language

    def get_additional_keys(self) -> Dict[str, Any]:
        """
        获取额外的配置项
        """
        return self._additional_keys

    def has_preset(self) -> bool:
        """
        是否有预设角色
        """
        return self._has_preset
