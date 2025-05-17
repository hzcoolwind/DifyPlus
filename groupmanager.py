import pickle
from typing import Dict, Optional
from loguru import logger

# 假设 ModelConfig 是一个类或 TypedDict
class ModelConfig:
    pass  # 根据你的实际配置类替换


class UserGroupModelManager:
    def __init__(self):
        # 三级字典结构：user_id -> group_id -> ModelConfig
        self._user_group_models: Dict[str, Dict[str, ModelConfig]] = {}

    def set_user_group_model(self, user_id: str, group_id: str, model: ModelConfig) -> None:
        """
        设置用户在某群组的模型配置
        :param user_id: 用户ID
        :param group_id: 群组ID
        :param model: 模型配置对象
        """
        if group_id is None: group_id = "0"
        if user_id not in self._user_group_models:
            self._user_group_models[user_id] = {}

        self._user_group_models[user_id][group_id] = model
        logger.debug(f"已为用户 {user_id} 在群组 {group_id} 设置默认模型配置")

    def get_user_group_model(self, user_id: str, group_id: str) -> Optional[ModelConfig]:
        """
        获取用户在某群组的模型配置
        :param user_id: 用户ID
        :param group_id: 群组ID
        :return: 模型配置对象，如果不存在则返回 None
        """
        if group_id is None: group_id = "0"
        user_configs = self._user_group_models.get(user_id, {})
        model = user_configs.get(group_id)

        # if model is None:
        #     logger.debug(f"DifyEher | 未找到用户 {user_id} 在群组 {group_id} 的默认模型配置")
        return model

    def clear_user_group_model(self, user_id: str, group_id: str) -> bool:
        """
        清除用户在某群组的模型配置
        :return: 是否成功清除
        """
        if group_id is None: group_id = "0"
        if user_id in self._user_group_models and group_id in self._user_group_models[user_id]:
            del self._user_group_models[user_id][group_id]
            print(f"已清除用户 {user_id} 在群组 {group_id} 的默认模型配置")
            return True
        return False

    def save_to_file(self, path: str):
        with open(path, 'wb') as f:
            pickle.dump(self._user_group_models, f)

    def load_from_file(self, path: str):
        with open(path, 'rb') as f:
            self._user_group_models = pickle.load(f)