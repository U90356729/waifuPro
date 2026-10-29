import yaml
import logging
import os
import glob
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

# 角色卡 yaml 的解析结果缓存，key 带 mtime，改了文件会自动失效
_YAML_CACHE = {}

class ConfigManager:
    def __init__(self, plugin):
        self.plugin = plugin
        self.data = {}
        self.character_config_path = ""
        self.template_path = ""
        
        # 获取当前文件所在目录（cells/config.py）
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        logger.info(f"当前文件目录: {self.current_dir}")

    async def load_config(self, character: str = "cute_neko", launcher_type: str = "person", completion: bool = True):
        """
        加载配置文件
        :param character: 角色卡名称
        :param launcher_type: 启动类型（person/group）
        :param completion: 是否完成配置
        """
        try:
            # 获取插件配置
            plugin_config = self.plugin.get_config().copy()
            
            # 移除不需要的配置项
            if 'templates' in plugin_config:
                del plugin_config['templates']
            if 'character_cards' in plugin_config:
                del plugin_config['character_cards']
            if 'langbot_group_rule' in plugin_config:
                del plugin_config['langbot_group_rule']
            
            # 只加载角色卡配置，不再使用模板文件
            character_data = {}
            if character != "off":
                character_file = os.path.join(self.current_dir, "..", "config", "cards", f"{character}.yaml")
                logger.info(f"角色卡文件路径: {character_file}")
                character_data = self._load_local_config_file(character_file)
            
            # 合并配置：角色卡配置优先于插件配置
            self.data = {**plugin_config, **character_data}
            
            # 如果需要完成配置，进行额外处理
            if completion:
                await self._complete_config()
            
            safe_keys = [k for k in self.data.keys() if k != "api_key"]
            logger.info(f"配置加载完成: keys={safe_keys}")
            return self.data
            
        except Exception as e:
            logger.error(f"加载配置失败: {e}", exc_info=True)
            raise
    
    def _load_local_config_file(self, file_path: str) -> Dict[str, Any]:
        """
        加载本地配置文件
        :param file_path: 文件路径
        :return: 配置数据
        """
        # 检查文件是否存在
        if not os.path.exists(file_path):
            logger.error(f"文件不存在: {file_path}")
            # 打印当前目录的文件结构
            logger.info(f"当前目录结构:")
            for item in os.listdir(self.current_dir):
                logger.info(f"  {item}")
            # 打印上级目录的文件结构
            parent_dir = os.path.dirname(self.current_dir)
            logger.info(f"上级目录结构:")
            for item in os.listdir(parent_dir):
                logger.info(f"  {item}")
            raise FileNotFoundError(f"配置文件不存在: {file_path}")

        # 按 mtime + 大小做缓存，每条消息都要读角色卡，但不该每次都落盘
        try:
            stat = os.stat(file_path)
            cache_key = (file_path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            cache_key = None

        if cache_key and cache_key in _YAML_CACHE:
            return _YAML_CACHE[cache_key]

        with open(file_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
        logger.info(f"本地配置文件 {file_path} 加载成功")

        if cache_key:
            _YAML_CACHE[cache_key] = config_data
        return config_data
    
    async def _complete_config(self):
        """
        完成配置，处理默认值和依赖关系
        """
        # 设置默认值
        defaults = {
            "short_term_memory_size": 2000,
            "retrieve_top_n": 3,
            "summary_max_tags": 30,
            "repeat_trigger": 2,
            "bracket_rate": [0.1, 0.1],
            "display_value": False,
            "enable_thinking_analysis": False,
            "max_prompt_chars": 3000,
            "intervals": [],
            "proactive_target_user_id": "off",
            "proactive_greeting_enabled": False,
            "proactive_greeting_probability": 50,
            "proactive_min_inactive_minutes": 180,
            "proactive_do_not_disturb_start": "23:00",
            "proactive_do_not_disturb_end": "08:00",
            "loop_time": 60,
            "admin_ids": []
        }
        
        # 合并默认值
        for key, value in defaults.items():
            if key not in self.data:
                self.data[key] = value
    
    def get_config(self) -> Dict[str, Any]:
        """
        获取配置内容
        """
        return self.data
    
    def update_config(self, key: str, value: Any):
        """
        更新配置内容
        """
        self.data[key] = value
        logger.info(f"配置已更新: {key} = {value}")
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项，支持默认值
        """
        return self.data.get(key, default)
    
    async def get_all_character_cards(self) -> List[str]:
        """
        获取所有可用的角色卡名称（直接扫 config/cards 目录）
        """
        try:
            cards_dir = os.path.join(self.current_dir, "..", "config", "cards")
            names = sorted(
                os.path.splitext(os.path.basename(path))[0]
                for path in glob.glob(os.path.join(cards_dir, "*.yaml"))
            )
            if names:
                logger.info(f"可用的角色卡: {names}")
                return names
        except Exception as e:
            logger.error(f"获取角色卡列表失败: {e}")

        return ["cute_neko", "lively_assistant"]
