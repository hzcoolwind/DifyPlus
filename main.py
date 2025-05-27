import io
import json
import re
import subprocess
import tomllib
from typing import Optional, Union, Dict, List, Tuple, Any
import time
from dataclasses import dataclass, field
import asyncio
from collections import defaultdict
import urllib.parse
import mimetypes
import base64
import uuid
import aiohttp
import filetype
from loguru import logger
import speech_recognition as sr
import os
import traceback
import shutil
import xml.etree.ElementTree as ET
import utils
from WechatAPI import WechatAPIClient
from database.XYBotDB import XYBotDB
from plugins.DifyPlus.groupmanager import UserGroupModelManager
from utils.decorators import *
from utils.plugin_base import PluginBase
from PIL import Image
from pathlib import Path

# 添加API代理导入
try:
    from api_manager_integrator import has_api_manager_feature

    has_api_proxy = has_api_manager_feature()
    if has_api_proxy:
        logger.info("API管理中心可用，DifyPlus插件将使用API代理")
    else:
        logger.info("API管理中心不可用，DifyPlus插件将使用直接连接")
except ImportError:
    has_api_proxy = False
    logger.warning("未找到API管理中心集成模块，DifyPlus插件将使用直接连接")

# 常量定义
XYBOT_PREFIX = "\n"
DIFY_ERROR_MESSAGE = "🙅对不起，DifyPlus出现错误！\n"
VOICE_TRANSCRIPTION_FAILED = "\n语音转文字失败"
TEXT_TO_VOICE_FAILED = "\n文本转语音失败"


@dataclass
class ModelConfig:
    api_key: str
    base_url: str
    trigger_words: list[str]
    description: str
    wakeup_words: list[str] = field(default_factory=list)  # 添加唤醒词列表字段


@dataclass
class GroupsConfig:
    command_tip: str
    group_ids: list[str] = field(default_factory=list)  # 群聊id
    group_names: list[str] = field(default_factory=list)  # 群聊简称
    models: list[str] = field(default_factory=list)  # 群组允许的智能体
    csrs: list[str] = field(default_factory=list)  # 人工座席


class DifyPlus(PluginBase):
    description = "DifyPlus插件"
    author = "冷风"
    version = "1.0.3"  # 更新版本号
    is_ai_platform = True  # 标记为 AI 平台插件

    def __init__(self):
        super().__init__()
        self.user_models = {}  # 存储用户当前使用的智能体
        self.processed_messages = {}  # 存储已处理的消息ID，避免重复处理
        self.message_expiry = 60  # 消息处理记录的过期时间（秒）
        self.user_group_manager = UserGroupModelManager()

        try:
            with open("main_config.toml", "rb") as f:
                config = tomllib.load(f)
            self.admins = config["XYBot"]["admins"]
        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            logger.error(f"加载主配置文件失败: {e}")
            raise

        try:
            with open("plugins/DifyPlus/config.toml", "rb") as f:
                config = tomllib.load(f)
            plugin_config = config["Dify"]
            self.enable = plugin_config["enable"]
            self.default_model = plugin_config["default-model"]
            self.command_tip = plugin_config["command-tip"]
            self.commands = plugin_config["commands"]
            self.http_proxy = plugin_config["http-proxy"]
            self.voice_reply_all = plugin_config["voice_reply_all"]
            self.robot_names = plugin_config.get("robot-names", [])
            # 移除单独的 URL 配置，改为动态构建
            self.remember_user_model = plugin_config.get("remember_user_model", True)
            self.support_agent_mode = plugin_config.get("support_agent_mode", True)  # 添加Agent模式支持开关
            self.need_wakeup = plugin_config.get("need-wakeup", True)  # 私聊默认需要唤醒
            self.reply_title = plugin_config.get("reply-title", '')  # 私聊需要唤醒词，则回复内容添加抬头

            # 加载所有智能体配置
            self.models = {}
            for model_name, model_config in plugin_config.get("models", {}).items():
                self.models[model_name] = ModelConfig(
                    api_key=model_config["api-key"],
                    base_url=model_config["base-url"],
                    trigger_words=model_config["trigger-words"],
                    # 如果有唤醒词配置则加载,否则使用空列表
                    wakeup_words=model_config.get("wakeup-words", []),
                    description=model_config.get("description", [])
                )

            # 加载所有群组配置
            self.groups = {}
            for groups_name, groups_config in plugin_config.get("groups", {}).items():
                self.groups[groups_name] = GroupsConfig(
                    # 如果有唤醒词配置则加载,否则使用空列表
                    group_ids=groups_config.get("group-ids", []),
                    group_names=groups_config.get("group-names", []),
                    models=groups_config.get("models", []),
                    csrs=groups_config.get("csrs", []),
                    command_tip=groups_config.get("command-tip", self.command_tip)
                )

            # 设置当前使用的智能体
            self.current_model = self.models[self.default_model]

        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            logger.error(f"加载DifyPlus插件配置文件失败: {e}")
            raise

        self.db = XYBotDB()
        self.image_cache = {}
        self.image_cache_timeout = 120
        # 添加文件缓存
        self.file_cache = {}
        self.file_cache_timeout = 300  # 5分钟文件缓存超时
        # 添加文件存储目录配置
        self.files_dir = "files"
        # 创建文件存储目录
        os.makedirs(self.files_dir, exist_ok=True)
        # 创建临时文件目录
        os.makedirs("temp", exist_ok=True)

        # 添加Agent模式相关属性
        self.current_agent_thoughts = {}  # 存储当前Agent思考过程，格式: {conversation_id: [thought1, thought2, ...]}
        self.agent_files = {}  # 存储Agent生成的文件，格式: {file_id: {url: "", type: "", belongs_to: ""}}

        # 允许多个智能体共享唤醒词
        self.wakeup_word_to_models = defaultdict(list)  # 改为存储智能体列表
        logger.info("开始加载唤醒词配置:")
        for model_name, model_config in self.models.items():
            # logger.info(f"处理智能体 '{model_name}' 的唤醒词列表: {model_config.wakeup_words}")
            for wakeup_word in model_config.wakeup_words:
                self.wakeup_word_to_models[wakeup_word].append(model_config)
                logger.info(f"唤醒词 '{wakeup_word}' 添加到智能体 '{model_name}' 的唤醒词列表")
        # 如果有重复唤醒词，记录日志但不覆盖
        for wakeup_word, models in self.wakeup_word_to_models.items():
            if len(models) > 1:
                model_names = [next((name for name, config in self.models.items()
                                     if config == model), '未知') for model in models]
                logger.warning(f"唤醒词 '{wakeup_word}' 被多个智能体共享: {', '.join(model_names)}")
        logger.info(f"唤醒词映射完成，共加载 {len(self.wakeup_word_to_models)} 个唤醒词")

        # 加载群组配置
        self.groupid_to_groupsconfig = {}
        logger.info("开始加载群组配置:")
        for groups_name, groups_config in self.groups.items():
            # logger.info(f"处理群组 '{groups_name}' 的群聊列表: {groups_config.group_names}")
            for group_id in groups_config.group_ids:
                group_name = groups_config.group_names[groups_config.group_ids.index(group_id)]
                if group_id in self.groupid_to_groupsconfig:
                    old_groups_name = next((name for name, config in self.groups.items()
                                            if config == self.groupid_to_groupsconfig[group_id]), '未知')
                    logger.warning(f"群聊 '{group_name}({group_id})' 已经添加到群组配置 '{old_groups_name}'， "
                                   f"当前的群组配置{groups_name}将忽略该群聊！")
                else:
                    self.groupid_to_groupsconfig[group_id] = groups_config
                    logger.info(f"群聊：'{group_name}({group_id})' 添加到群组配置 '{groups_name}' 的群聊列表")
        logger.info(f"群聊加载完成，共加载 {len(self.groupid_to_groupsconfig)} 个群聊")

        # 加载配置文件
        self.config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        logger.info(f"加载DifyPlus插件配置文件：{self.config_path}")

        # 尝试获取API代理实例
        self.api_proxy = None
        if has_api_proxy:
            try:
                import sys
                # 导入api_proxy实例
                sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
                from admin.server import get_api_proxy
                self.api_proxy = get_api_proxy()
                if self.api_proxy:
                    logger.info("成功获取API代理实例")
                else:
                    logger.warning("API代理实例获取失败，将使用直接连接")
            except Exception as e:
                logger.error(f"获取API代理实例失败: {e}")
                logger.error(traceback.format_exc())

    def get_user_model(self, user_id: str) -> ModelConfig:
        """获取用户当前使用的智能体"""
        if self.remember_user_model and user_id in self.user_models:
            return self.user_models[user_id]
        return self.current_model

    def set_user_model(self, user_id: str, model: ModelConfig):
        """设置用户当前使用的智能体"""
        if self.remember_user_model:
            self.user_models[user_id] = model

    def get_user_group_model(self, user_id: str, group_id: str) -> ModelConfig:
        """获取用户群聊默认智能体"""
        if self.remember_user_model:
            model_config = self.user_group_manager.get_user_group_model(user_id, group_id)
            if model_config is None:
                return self.get_group_default_model(group_id)
            else:
                return model_config
        return self.get_group_default_model(group_id)

    def set_user_group_model(self, user_id: str, group_id: str, model: ModelConfig):
        """设置用户群聊默认智能体"""
        if self.remember_user_model:
            self.user_group_manager.set_user_group_model(user_id, group_id, model)

    # 辅助函数：检查智能体是否可用于当前群组
    def is_model_allowed(self, group_id, model_config: ModelConfig) -> bool:
        # 私聊不受限制
        if group_id is None:
            return True

        groups_config = self.groupid_to_groupsconfig.get(group_id, None)
        if not groups_config:
            return False
        for model_name in groups_config.models:
            if self.models[model_name] == model_config:
                return True
        return False

    # 辅助函数：获取群聊默认智能体（群组配置智能体的第一个）
    def get_group_default_model(self, group_id) -> ModelConfig | None:
        if group_id is None:
            return None
        try:
            groups_config = self.groupid_to_groupsconfig.get(group_id, None)
            if groups_config is None:
                return None
            model_name = groups_config.models[0]
            return self.models[model_name]
        except Exception as e:
            logger.error(f"获取群聊默认智能体: {e}")
            logger.error(traceback.format_exc())
            return None

    def is_message_processed(self, message: dict) -> bool:
        """检查消息是否已经处理过"""
        # 清理过期的消息记录
        current_time = time.time()
        expired_keys = []
        for msg_id, timestamp in self.processed_messages.items():
            if current_time - timestamp > self.message_expiry:
                expired_keys.append(msg_id)

        for key in expired_keys:
            del self.processed_messages[key]

        # 获取消息ID
        msg_id = message.get("MsgId") or message.get("NewMsgId")
        if not msg_id:
            return False  # 如果没有消息ID，视为未处理过

        # 检查消息是否已处理
        return msg_id in self.processed_messages

    def mark_message_processed(self, message: dict):
        """标记消息为已处理"""
        msg_id = message.get("MsgId") or message.get("NewMsgId")
        if msg_id:
            self.processed_messages[msg_id] = time.time()
            logger.debug(f"标记消息 {msg_id} 为已处理")

    # 根据消息内容和群组ID判断使用哪个智能体
    def get_model_from_message(self, content: str, user_id: str, group_id: str = None):
        #    -> tuple[ModelConfig, str, bool, bool] | \
        #       tuple[None, str, bool, bool] | \
        #       tuple[Any, str, bool, bool]:
        """根据消息内容和群组ID判断使用哪个智能体
        Args:
            content: 消息内容
            user_id: 用户ID
            group_id: 群组ID(可选，私聊时为None)
        Returns:
            tuple: (选择的智能体, 处理后的查询内容, 是否是切换命令, 是否检测到唤醒词)
                   (None, original_content, False, False) 如果没有可用智能体

            情况1： 触发词+切换  （切换用户默认智能体为触发词对应智能体           有model，切换TRUE， 唤醒False）
                               （切换不成功，继续检测是否有唤醒词）
            情况2： 唤醒词+内容   (内容送唤醒词对应智能体的dify，             有model，切换FALSE， 唤醒TRUE）
                                (唤醒词对应智能体不可用， 不做处理，继续）
            情况3： 触发词+内容  （内容送触发词对应智能体的dify，              有model，切换FALSE， 唤醒TRUE）
            情况4： 未唤醒内容   （内容返回，返回默认智能体，不唤醒，后续处       model为默认，切换FALSE，唤醒FALSE）
            情况5： 没有可用的智能体（内容返回                               model为None，切换FALSE，唤醒FALSE）
        """
        original_content = content  # 保留原始内容
        content = content.lower()  # 只在检测时使用小写版本

        # 1. 检查是否是切换智能体的命令
        if content.endswith("切换"):
            for model_name, model_config in self.models.items():
                for trigger in model_config.trigger_words:
                    if content.startswith(trigger.lower()):
                        if not self.is_model_allowed(group_id, model_config):
                            continue  # 群组无权限使用此智能体
                        if group_id is None:
                            self.set_user_model(user_id, model_config)
                        else:
                            self.set_user_group_model(user_id, group_id, model_config)
                        logger.info(f"用户 {user_id} 群组{group_id} 切换智能体到 {model_name}")
                        return model_config, original_content, True, False
            # 没有找到可用的切换目标智能体, 可能是非切换命令
            logger.warning(f"用户: {user_id} 群组: {group_id} 尝试切换智能体，但没有该可用智能体，继续检查是否有唤醒词")
            # return None, original_content, True, False

        # 2. 检查是否使用了唤醒词
        logger.debug(f"检查是否包含唤醒词 '{content}' ")
        for wakeup_word, model_configs in self.wakeup_word_to_models.items():
            wakeup_lower = wakeup_word.lower()
            content_lower = content.lower()
            if content_lower.startswith(wakeup_lower) or f" {wakeup_lower}" in content_lower:
                # 过滤出当前群组可用的智能体
                available_models = [
                    config for config in model_configs
                    if self.is_model_allowed(group_id, config)
                ]
                if not available_models:
                    continue  # 没有可用智能体
                # 如果有多个可用智能体，使用第一个（或可以添加优先级逻辑）
                model_config = available_models[0]
                model_name = next((name for name, config in self.models.items()
                                   if config == model_config), '未知')
                logger.info(f"消息中检测到唤醒词 '{wakeup_word}'，使用智能体 '{model_name}'")
                # 精确替换唤醒词（保持原逻辑）
                original_wakeup = None
                if content_lower.startswith(wakeup_lower):
                    original_wakeup = original_content[:len(wakeup_lower)]
                else:
                    wakeup_pos = content_lower.find(f" {wakeup_lower}") + 1
                    if wakeup_pos > 0:
                        original_wakeup = original_content[wakeup_pos:wakeup_pos + len(wakeup_lower)]
                if original_wakeup:
                    query = original_content.replace(original_wakeup, "", 1).strip()
                    logger.debug(f"唤醒词处理后的查询: '{query}'")
                    return model_config, query, False, True

        # 3. 使用了触发词，但不是切换的，类似唤醒词触发
        logger.debug(f"检查是否包含触发词: '{content}'")
        for model_name, model_config in self.models.items():
            for trigger in model_config.trigger_words:
                if trigger.lower() in content:
                    if not self.is_model_allowed(group_id, model_config):
                        continue  # 群组无权限使用此智能体
                    logger.info(f"消息中包含触发词 '{trigger}'，使用智能体 '{model_name}'")
                    query = original_content.replace(trigger, "", 1).strip()
                    logger.debug(f"触发词处理后的查询: '{query}'")
                    return model_config, query, False, True

        # 4. 使用用户当前的智能体，但要检查群组权限
        if group_id is None:
            current_model = self.get_user_model(user_id)
        else:
            current_model = self.get_user_group_model(user_id, group_id)
        if current_model and self.is_model_allowed(group_id, current_model):
            model_name = next((name for name, config in self.models.items() if config == current_model), '默认')
            logger.debug(f"未检测到特定智能体，使用用户 {user_id} 当前默认智能体 '{model_name}'")
            return current_model, original_content, False, False

        # 5. 没有可用智能体的情况
        logger.warning(f"用户 {user_id} 在群组 {group_id} 没有可用智能体")
        return None, original_content, False, False


    async def reset_conversation(self, bot: WechatAPIClient, message: dict, model_config=None):
        """重置与Dify的对话

        Args:
            bot: WechatAPIClient实例
            message: 消息字典
            model_config: 模型配置（可选）

        Returns:
            bool: 是否成功重置对话
        """
        try:
            # 使用传入的model_config，如果没有则使用默认模型
            model = model_config or self.current_model

            # 获取用户ID
            user_id = message["FromWxid"]
            if message.get("IsGroup", False):
                # 群聊消息，使用群聊ID
                user_id = message["FromWxid"]
            else:
                # 私聊消息，使用发送者ID
                user_id = message["SenderWxid"]

            # 从数据库获取会话ID
            conversation_id = self.db.get_llm_thread_id(user_id, "dify")

            if not conversation_id:
                logger.info(f"用户 {user_id} 没有活跃的对话，无需重置")
                return False

            logger.info(f"准备重置用户 {user_id} 的对话，会话ID: {conversation_id}")

            # 构建API请求
            url = f"{model.base_url}/conversations/{conversation_id}"
            headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
            data = {"user": user_id}

            # 发送DELETE请求
            async with aiohttp.ClientSession() as session:
                # 正确的方式是在请求时设置代理，而不是在创建会话时
                proxy = self.http_proxy if self.http_proxy and self.http_proxy.strip() else None
                async with session.delete(url, headers=headers, json=data, proxy=proxy) as resp:
                    if resp.status in (200, 201, 204):
                        if resp.ok:
                            # 重置成功，清除数据库中的会话ID
                            self.db.save_llm_thread_id(user_id, "", "dify")
                            logger.success(f"成功重置用户 {user_id} 的对话")
                            return True
                        else:
                            logger.error(f"重置对话失败")
                    else:
                        logger.error(f"重置对话失败: HTTP {resp.status}")

            return False
        except Exception as e:
            logger.error(f"重置对话时发生错误: {e}")
            logger.error(traceback.format_exc())
            return False


    async def command_message_process(self, bot: WechatAPIClient, message: dict, command):
        logger.info("[command_message_process]")

        is_command = command in self.commands
        if is_command:
            # 如果是命令，处理命令
            if message["IsGroup"]:
                # 群聊处理
                group_id = message["FromWxid"]
                user_wxid = message["SenderWxid"]
                if command in self.commands:
                    if command == '/help' or command == '/帮助':
                        await bot.send_at_message(group_id, self.groupid_to_groupsconfig[group_id].command_tip,
                                                  [user_wxid])
                    if command == '/重置会话':
                        model = self.get_user_group_model(user_wxid, group_id)
                        # 执行重置对话操作
                        success = await self.reset_conversation(bot, message, model)
                        if success:
                            await bot.send_at_message(
                                group_id,
                                "\n对话已重置，我已经忘记了之前的对话内容。",
                                [user_wxid]
                            )
                        else:
                            await bot.send_at_message(
                                group_id,
                                "\n重置对话失败，可能是因为没有活跃的对话或发生了错误。",
                                [user_wxid]
                            )
                    if command == '/list' or command == '/智能体':
                        # 生成输出字符串
                        output_lines = []
                        for model_name in self.groupid_to_groupsconfig[group_id].models:
                            model_data = self.models[model_name]
                            line = (
                                f"智能体: [{model_name}]\n"
                                f"唤醒词：{', '.join(model_data.wakeup_words)}\n"
                                f"触发词：{', '.join(model_data.trigger_words)}\n"
                                f"描述:  {model_data.description}\n"
                            )
                            output_lines.append(line)
                        # 添加编号并合并为最终字符串
                        final_output = ""
                        for i, line in enumerate(output_lines, 1):
                            final_output += f"{i}. {line}\n"
                        default_model = next(name for name, config in self.models.items()
                                             if config == self.get_user_group_model(user_wxid, group_id))
                        final_output += f"输入相应智能体的'触发词 切换'可以切换默认智能体。\n\n"
                        final_output += f"您在当前群默认的智能体：\n[{default_model}]\n"
                        await bot.send_at_message(group_id, final_output, [user_wxid])
            else:
                # 私聊处理
                if command in self.commands:
                    if command == '/help' or command == '/帮助':
                        await bot.send_text_message(message["FromWxid"], self.command_tip)
                    if command == '/重置会话':
                        model = self.get_user_model(message["SenderWxid"])
                        # 执行重置对话操作
                        success = await self.reset_conversation(bot, message, model)
                        if success:
                            await bot.send_text_message(
                                message["FromWxid"],
                                "对话已重置，我已经忘记了之前的对话内容。"
                            )
                        else:
                            await bot.send_text_message(
                                message["FromWxid"],
                                "重置对话失败，可能是因为没有活跃的对话或发生了错误。"
                            )
                    if command == '/list' or command == '/智能体':
                        # 生成输出字符串
                        output_lines = []
                        for model_name in self.models:
                            model_data = self.models[model_name]
                            line = (
                                f"智能体: [{model_name}]\n"
                                f"唤醒词：{', '.join(model_data.wakeup_words)}\n"
                                f"触发词：{', '.join(model_data.trigger_words)}\n"
                                f"描述:  {model_data.description}\n"
                            )
                            output_lines.append(line)
                        # 添加编号并合并为最终字符串
                        final_output = ""
                        for i, line in enumerate(output_lines, 1):
                            final_output += f"{i}. {line}\n"
                        default_model = next(name for name, config in self.models.items()
                                             if config == self.get_user_model(message["FromWxid"]))
                        final_output += f"输入相应智能体的'触发词 切换'可以切换默认智能体。\n\n"
                        final_output += f"您当前默认的智能体：\n[{default_model}]\n"
                        await bot.send_text_message(message["FromWxid"], final_output)
            return False
        return True

    async def at_message_process(self, bot: WechatAPIClient, message: dict, content=None):
        """
        返回  True，Query，   有@，query为清理过的content
             Flase，Content  无@，直接返回content
        """
        logger.info("[at_message_process]")

        if content is None:
            content = message["Content"].strip()

        bot_status = self.get_bot_status()
        bot_wxid = None
        bot_nickname = None
        if bot_status and bot_status['status'] == 'ready':
            bot_wxid = bot_status['wxid']
            if bot_wxid and bot:
                bot_nickname = await bot.get_nickname(bot_wxid)
                logger.debug(f"获取到bot的昵称：{bot_nickname}")

        is_at = self.is_at_message(message, bot_wxid, bot_nickname)

        if is_at:
            query = content
            if content.startswith('@'):
                # 先检查是否是@机器人
                at_bot_prefix = None
                for robot_name in self.robot_names:
                    if content.startswith(f'@{robot_name}'):
                        at_bot_prefix = f'@{robot_name}'
                        break

                # 是不是@了bot的nickname
                if not at_bot_prefix and bot_nickname:
                    at_bot_prefix = f'@{bot_nickname}'

                if at_bot_prefix:
                    # 如果是@机器人，移除@机器人部分
                    query = content[len(at_bot_prefix):].strip()
                    logger.debug(f"移除@{at_bot_prefix}后的查询内容: {query}")
                else:
                    # 如果不是@机器人，则尝试找空格
                    space_index = content.find(' ')
                    if space_index > 0:
                        # 只保留空格后面的内容
                        query = content[space_index + 1:].strip()
                        logger.debug(f"移除@前缀后的查询内容: {query}")
                    else:
                        # 如果没有空格，尝试提取@后面的内容
                        # 找到第一个非空格字符的位置
                        for i in range(1, len(content)):
                            if content[i] != '@' and content[i] != ' ':
                                query = content[i:].strip()
                                logger.debug(f"提取@后面的内容: {query}")
                                break
                        else:
                            # 如果整个内容都是@，将query设为空
                            query = ""
            else:
                # 如果不是以@开头，则尝试移除@机器人名称
                for robot_name in self.robot_names:
                    query = query.replace(f"@{robot_name}", "").strip()

            logger.debug(f"@提取到的 query: {query}")
            return True, query
        return False, content

    def _safe_get_mime_type(self, file_path):
        path = Path(file_path)
        if not path.suffix:  # 无后缀的情况
            return "", "application/octet-stream"
        mime_type, _ = mimetypes.guess_type(file_path)
        return path.suffix, mime_type or "application/octet-stream"

    async def file_message_process(self, bot: WechatAPIClient, message: dict, model, image_md5=None, filename_md5=None):
        logger.info("[file_message_process]")

        files = []
        # 优先检查引用消息中的图片MD5
        if image_md5:
            try:
                logger.info(f"尝试根据MD5查找图片: {image_md5}")
                image_content = await self.find_image_by_md5(image_md5)
                if not image_content:
                    try:
                        logger.info(f"未找到该图片，尝试解析下载图片: {image_md5}")
                        quote_info = message.get("Quote", {})
                        quoted_content = quote_info.get("Content", "")
                        root = ET.fromstring(quoted_content)
                        img_element = root.find('img')
                        if img_element is not None:
                            aeskey = img_element.get('aeskey')
                            cdnmidimgurl = img_element.get('cdnmidimgurl')
                            length = img_element.get('length')
                            md5 = img_element.get('md5')
                            if md5 == image_md5:
                                logger.debug(f"解析图片XML成功: aeskey={aeskey}, length={length}, md5={md5}")
                                b64imagecontent = await bot.download_image(aeskey, cdnmidimgurl)
                                image_content = base64.b64decode(b64imagecontent)

                                # 确保files目录存在
                                files_dir = os.path.join(os.getcwd(), "files")
                                os.makedirs(files_dir, exist_ok=True)

                                # 根据MD5值生成文件名
                                file_extension = self._get_image_extension(image_content)
                                file_name = f"{md5}.{file_extension}"
                                file_path = os.path.join(files_dir, file_name)

                                # 保存图片文件
                                with open(file_path, "wb") as f:
                                    f.write(image_content)
                                logger.info(f"图片已保存到: {file_path}")
                            else:
                                logger.warning(f"解析图片XML中: md5={md5}，请求md5={image_md5}, 不一致。")
                    except Exception as e:
                        logger.error("解析图片消息失败: {}, 内容: {}", e, message["Content"])

                if image_content:
                    logger.info(f"根据MD5找到图片，大小: {len(image_content)} 字节")
                    # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
                    user_id = message["FromWxid"] if message.get("IsGroup", False) else message["SenderWxid"]
                    file_id = await self.upload_file_to_dify(
                        image_content,
                        f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                        "image/jpeg",
                        user_id,
                        model_config=model
                    )
                    if file_id:
                        logger.info(f"引用图片上传成功，文件ID: {file_id}")
                        files = [file_id]
                    else:
                        logger.error("引用图片上传失败")
                else:
                    logger.warning(f"未找到MD5为 {image_md5} 的图片")
            except Exception as e:
                logger.error(f"处理引用图片失败: {e}")

        # 优先检查引用消息中的file MD5
        if filename_md5:
            try:
                logger.info(f"尝试根据MD5查找文件: {filename_md5}")
                file_content = await self.find_file_by_md5(filename_md5)
                if not file_content:
                    try:
                        quote_info = message.get("Quote", {})
                        quoted_msg_type = quote_info.get("MsgType")
                        quoted_xml_type = quote_info.get("XmlType")
                        quoted_title = quote_info.get('Content')
                        quoted_md5 = quote_info.get('md5')
                        qutoed_appattach = quote_info.get("appattach", {})
                        qutoed_attachid = qutoed_appattach.get('attachid')
                        qutoed_totallen = qutoed_appattach.get('totallen')
                        qutoed_appid = qutoed_appattach.get('appid', '')
                        qutoed_fileext = qutoed_appattach.get('fileext', '')
                        logger.info(f"未查找文件: {filename_md5}，尝试解析下载文件: {quoted_title}, 大小: {qutoed_totallen} 字节")
                        download_success, file_data = await self.download_file_process(bot, qutoed_appid, qutoed_attachid,
                                                                                       qutoed_totallen)
                        if download_success:
                            file_content = file_data
                            file_name = f"{quoted_md5}.{qutoed_fileext}"  # 否则添加扩展名
                            await self.save_file_by_md5(file_name, file_data)
                    except Exception as e:
                        logger.error("读文件出错: {}", e)

                if file_content:
                    logger.info(f"根据MD5找到文件，大小: {len(file_content)} 字节")
                    # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
                    user_id = message["FromWxid"] if message.get("IsGroup", False) else message["SenderWxid"]
                    ext, mime_type = self._safe_get_mime_type(filename_md5)
                    file_id = await self.upload_file_to_dify(
                        file_content,
                        f"{int(time.time())}{ext}",  # 生成一个有效的文件名
                        mime_type,
                        user_id,
                        model_config=model
                    )
                    if file_id:
                        logger.info(f"引用文件上传成功，文件ID: {file_id}")
                        files = [file_id]
                    else:
                        logger.error("引用文件上传失败")
                else:
                    logger.warning(f"未找到MD5为 {filename_md5} 的文件")
            except Exception as e:
                logger.error(f"处理引用文件失败: {e}")

        if not files:
            image_content = await self.get_cached_image(message["FromWxid"])
            if image_content:
                try:
                    logger.debug("发现最近的图片，准备上传到 Dify")
                    # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
                    user_id = message["FromWxid"] if message.get("IsGroup", False) else message["SenderWxid"]
                    file_id = await self.upload_file_to_dify(
                        image_content,
                        f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                        "image/jpeg",  # 根据实际图片类型调整
                        user_id,
                        model_config=model  # 传递正确的智能体配置
                    )
                    if file_id:
                        logger.debug(f"图片上传成功，文件ID: {file_id}")
                        files = [file_id]
                        return files
                    else:
                        logger.error("图片上传失败")
                        return files
                except Exception as e:
                    logger.error(f"处理图片失败: {e}")
                    return files
        return files

    # 群聊消息处理
    async def group_message_process(self, bot: WechatAPIClient, message: dict, content=None, image_md5=None,
                                    filename_md5=None):

        if not message["IsGroup"]:
            return True

        logger.info("[group_message_process]")

        if content is None:
            content = message["Content"].strip()

        group_id = message["FromWxid"]
        user_wxid = message["SenderWxid"]

        # 检查该群聊是否有对应的智能体，是否有唤醒词或触发词，是否是切换模型命令
        wakeup_model, processed_wakeup_query, is_switch, wakeup_detected = self.get_model_from_message(
            content,
            user_wxid,
            group_id
        )

        # 群聊如果没有智能体就直接返回
        if wakeup_model is None:
            logger.info(f"该群:{group_id}没有配置智能体，直接返回。")
            return True

        # 如果是切换命令，切换
        if is_switch:
            if wakeup_model:
                model_name = next(name for name, config in self.models.items() if config == wakeup_model)
                await bot.send_at_message(
                    group_id,
                    f"\n已切换到{model_name}智能体，将一直使用该智能体直到下次切换。",
                    [user_wxid]
                )
            return False

        # 处理命令
        command = content.split(" ")[0] if content else ""
        if not await self.command_message_process(bot, message, command):
            return False

        # 检查是否有最近的图片 - 无论聊天室功能是否启用都获取图片
        files = await self.file_message_process(bot, message, wakeup_model, image_md5, filename_md5)

        # 如果检测到唤醒（唤醒词或触发词），处理唤醒请求
        if wakeup_detected and wakeup_model and processed_wakeup_query:
            if wakeup_model.api_key:  # 检查唤醒词对应智能体的API密钥
                logger.info(f"唤醒对应智能体处理请求")
                await self.dify(bot, message, processed_wakeup_query, files=files, specific_model=wakeup_model)
            else:
                model_name = next((name for name, config in self.models.items() if config == wakeup_model), '未知')
                logger.error(f"唤醒对应智能体 '{model_name}' 的API密钥未配置")
                await bot.send_at_message(group_id, f"\n此智能体API密钥未配置，请联系管理员", [user_wxid])
            return False

        # 如果没有触发的且有@机器人情况的，一般会使用默认模型处理
        is_at, query = await self.at_message_process(bot, message, content)
        if is_at:
            logger.info(f"未唤醒且有群聊@的先处理消息再发送智能体处理请求")
            if not query:
                await bot.send_at_message(message["FromWxid"], "\n请输入你的问题或指令。", [message["SenderWxid"]])
                return False

            await self.dify(bot, message, query, files=files, specific_model=wakeup_model)
            return False
        # 都不是，就让别的插件处理吧
        return True

    async def private_message_process(self, bot: WechatAPIClient, message: dict, content=None, image_md5=None,
                                      filename_md5=None):
        if message["IsGroup"]:
            return True

        logger.info("[private_message_process]")

        if content is None:
            content = message["Content"].strip()

        # 先检查唤醒词或触发词，获取对应智能体
        model, processed_query, is_switch, wakeup_detected = self.get_model_from_message(
            content,
            message["FromWxid"],
            None
        )

        # 没有可用智能体
        if model is None:
            logger.info("没有配置智能体，直接返回。")
            return True

        # 如果是切换命令，切换
        if is_switch:
            if model:
                model_name = next(name for name, config in self.models.items() if config == model)
                await bot.send_text_message(
                    message["FromWxid"],
                    f"已切换到{model_name.upper()}智能体，将一直使用该智能体直到下次切换。"
                )
            return False

        # 处理命令
        command = content.split(" ")[0] if content else ""
        if not await self.command_message_process(bot, message, command):
            return False

        # 检查是否有最近的图片
        files = await self.file_message_process(bot, message, model, image_md5, filename_md5)

        if wakeup_detected and model and processed_query:
            if model.api_key:  # 检查唤醒词对应智能体的API密钥
                logger.info(f"使用唤醒对应智能体处理请求")
                await self.dify(bot, message, processed_query, files=files, specific_model=model)
            else:
                model_name = next((name for name, config in self.models.items() if config == model), '未知')
                logger.error(f"唤醒对应智能体 '{model_name}' 的API密钥未配置")
                await bot.send_message(message["FromWxid"], f"\n此智能体API密钥未配置，请联系管理员")
            return False

        if self.need_wakeup:
            logger.debug(f"私聊需要唤醒或触发词，未激活智能体")
        else:
            logger.info(f"私聊不需要唤醒词，激活默认智能体处理请求")
            await self.dify(bot, message, processed_query, files=files, specific_model=model)
            return False

        # 都不是，交给别的插件处理
        return True

    @on_text_message(priority=25)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return True

        logger.info('[handle_text]>>>')
        if not message["IsGroup"]:
            # 私聊
            ret = await self.private_message_process(bot, message)
        else:
            # 群聊
            ret = await self.group_message_process(bot, message)
        logger.info(f'<<<[handle_text] return:{ret}')
        return ret

    @on_at_message(priority=25)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return True

        # 必定是群聊吧？
        logger.info('[handle_at]>>>')

        # 群聊
        ret = await self.group_message_process(bot, message)
        logger.info(f'<<<[handle_at] return:{ret}')
        return False

    @on_quote_message(priority=25)
    async def handle_quote(self, bot: WechatAPIClient, message: dict):
        """处理引用消息"""
        if not self.enable:
            return True

        logger.info('[handle_quote]>>>')

        # 检查消息是否已经处理过
        if self.is_message_processed(message):
            logger.info(f"消息 {message.get('MsgId') or message.get('NewMsgId')} 已经处理过，跳过")
            return False  # 消息已处理，阻止后续插件处理

        # 标记消息为已处理
        self.mark_message_processed(message)

        # 提取引用消息的内容
        content = message["Content"].strip()
        quote_info = message.get("Quote", {})
        quoted_content = quote_info.get("Content", "")
        quoted_sender = quote_info.get("Nickname", "")
        quoted_msg_type = quote_info.get("MsgType")
        qutoed_appattach = quote_info.get("appattach", {})

        logger.info(f"处理引用消息: 内容={content}, 引用内容={quoted_content}, 引用发送者={quoted_sender}")

        # 检查引用的消息是否包含图片
        image_md5 = message.get("ImageMD5")  # 首先检查消息中是否已经有MD5（从XML处理中传递过来的）

        # 如果没有，尝试从引用消息中提取
        if not image_md5 and quote_info.get("MsgType") == 3:  # 图片消息
            try:
                # 尝试从引用的图片消息中提取MD5
                # if "<?xml" in quoted_content and "<img" in quoted_content:
                if "<img" in quoted_content:
                    root = ET.fromstring(quoted_content)
                    img_element = root.find('img')
                    if img_element is not None:
                        image_md5 = img_element.get('md5')
                        logger.info(f"从引用的图片消息中提取到MD5: {image_md5}")
            except Exception as e:
                logger.error(f"解析引用图片消息XML失败: {e}")

        if image_md5:
            logger.info(f"引用消息处理: 找到图片MD5: {image_md5}")

        filename_md5 = None
        if not filename_md5 and quote_info.get("MsgType") == 49:  # 文件消息
            try:
                filename_md5 = quote_info.get("md5", None)
                if filename_md5:
                    filename_md5 += '.' + qutoed_appattach.get("fileext")
                logger.info(f"从引用的文件消息中提取到MD5:{filename_md5}")
            except Exception as e:
                logger.error(f"解析引用图片消息XML失败: {e}")

        if filename_md5:
            logger.info(f"引用消息处理: 找到文件MD5: {filename_md5}")

        # 处理群聊和私聊的情况
        if message["IsGroup"]:
            is_at, query = await self.at_message_process(bot, message)
            # 如果没有内容，则使用引用的内容
            if not query or query == '':
                query = f"请回复这条消息: '{quoted_content}'"
                if image_md5 and quoted_msg_type == 3:
                    query = f"请分析下这个图片"
                if filename_md5 and quoted_msg_type == 49:
                    query = f"请分析下这个文件"
            else:
                if (not (image_md5 and quoted_msg_type == 3) and
                        not (filename_md5 and quoted_msg_type == 49)):
                    query = f"{query} (引用消息: '{quoted_content}')"
            ret = await self.group_message_process(bot, message, query, image_md5, filename_md5)
        else:
            is_at, query = await self.at_message_process(bot, message)
            # 如果没有内容，则使用引用的内容
            if not query or query == '':
                query = f"请回复这条消息: '{quoted_content}'"
                if image_md5 and quoted_msg_type == 3:
                    query = f"请分析下这个图片"
                if filename_md5 and quoted_msg_type == 49:
                    query = f"请分析下这个文件"
            else:
                if (not (image_md5 and quoted_msg_type == 3) and
                        not (filename_md5 and quoted_msg_type == 49)):
                    query = f"{query} (引用消息: '{quoted_content}')"
            ret = await self.private_message_process(bot, message, query, image_md5, filename_md5)
        logger.info(f'<<<[handle_quote] return:{ret}')
        return ret

    @on_voice_message(priority=25)
    async def handle_voice(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        logger.info('[handle_voice]>>>')
        if message["IsGroup"]:
            return True

        if not self.current_model.api_key:
            await bot.send_text_message(message["FromWxid"], "你还没配置Dify API密钥！")
            return False

        query = await self.audio_to_text(bot, message)
        if not query:
            await bot.send_text_message(message["FromWxid"], VOICE_TRANSCRIPTION_FAILED)
            return False

        logger.debug(f"语音转文字结果: {query}")
        ret = await self.private_message_process(bot, message, query)
        logger.info(f'<<<[handle_voice] return:{ret}')
        return ret

    def get_bot_status(self):
        """获取机器人状态"""
        status_file = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "../bot_status.json"
        admin_status_file = Path(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "../admin/bot_status.json"
        if status_file.exists():
            try:
                with open(status_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取状态文件失败: {e}")
        if admin_status_file.exists():
            try:
                with open(admin_status_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取状态文件失败: {e}")
        # 无法获取状态
        return None

    def is_at_message(self, message: dict, bot_wxid=None, bot_nickname=None) -> bool:
        """检查消息是否@了机器人

        支持检测普通消息和引用消息中的@
        """
        if not message["IsGroup"]:
            return False

        # 获取消息内容
        content = message["Content"]

        # 记录原始消息信息便于调试
        logger.debug(f"检查消息是否@机器人: {content[:50]}...")

        # 检查消息类型
        msg_type = message.get("MsgType")
        logger.debug(f"消息类型: {msg_type}, 是否有Quote字段: {'Quote' in message}")

        # 增强对XML引用消息的处理
        if "Quote" in message:
            logger.info(f"详细检查引用消息是否@机器人: {content[:50]}...")

            # 直接检查消息内容中是否包含@机器人
            for robot_name in self.robot_names:
                # 检查格式: "@小球子 xxx"
                if f"@{robot_name}" in content:
                    logger.info(f"在引用消息内容中发现@{robot_name}")
                    return True

                # 检查格式: "@小球子"（消息开头）
                if content.startswith(f'@{robot_name}'):
                    logger.info(f"引用消息内容以@{robot_name}开头")
                    return True

                # 特殊处理：检查是否是@小球子这样的格式（忽略大小写）
                if content.lower().startswith(f'@{robot_name.lower()}'):
                    logger.info(f"引用消息内容以@{robot_name}开头（忽略大小写）")
                    return True

                # 检查格式: "@小球子"（消息中间）
                at_pattern = re.compile(f'@{robot_name}\\b')
                if at_pattern.search(content):
                    logger.info(f"在引用消息内容中发现@{robot_name}（正则匹配）")
                    return True

            # 检查消息内容是否以@开头，后面跟着空格和其他内容
            if content.startswith('@'):
                # 提取@后面的名称部分
                space_index = content.find(' ')
                if space_index > 0:
                    at_name = content[1:space_index].strip()
                    logger.info(f"提取到@名称: {at_name}")

                    # 检查提取的名称是否是机器人名称
                    for robot_name in self.robot_names:
                        if at_name == robot_name or at_name.lower() == robot_name.lower():
                            logger.info(f"@名称匹配机器人名称: {robot_name}")
                            return True

                        # 检查名称是否部分匹配（例如@小球 可能是@小球子的简写）
                        if robot_name.startswith(at_name) or robot_name.lower().startswith(at_name.lower()):
                            logger.info(f"@名称部分匹配机器人名称: {at_name} -> {robot_name}")
                            return True

        # 如果消息内容以@开头，这是一个强烈的信号，表明用户@了某人
        if content.startswith('@'):
            logger.debug(f"消息内容以@开头: {content[:20]}")
            # 检查@的是否是机器人
            for robot_name in self.robot_names:
                if content.startswith(f'@{robot_name}'):
                    logger.debug(f"消息内容以@{robot_name}开头")
                    return True

                # 特殊处理：检查是否是@小小x这样的格式（可能有空格）
                if content.lower().startswith(f'@{robot_name.lower()}'):
                    logger.debug(f"消息内容以@{robot_name}开头（忽略大小写）")
                    return True
            # 如果@的不是机器人，继续检查其他条件

        # 检查普通消息中的@
        for robot_name in self.robot_names:
            if f"@{robot_name}" in content:
                logger.debug(f"在消息内容中发现@{robot_name}")
                return True

        # 如果是引用消息，检查消息类型
        if msg_type == 49 or msg_type == 57 or "Quote" in message:  # 引用消息类型
            logger.debug(f"检测到引用消息: {msg_type}, Quote字段: {'Quote' in message}")

            # 特殊处理：如果消息内容以@开头，这是一个强烈的信号，表明用户@了某人
            if content.startswith('@'):
                for robot_name in self.robot_names:
                    if content.startswith(f'@{robot_name}'):
                        logger.debug(f"引用消息内容以@{robot_name}开头")
                        return True

                    # 特殊处理：检查是否是@小小x这样的格式（可能有空格）
                    if content.lower().startswith(f'@{robot_name.lower()}'):
                        logger.debug(f"引用消息内容以@{robot_name}开头（忽略大小写）")
                        return True

            # 如果有Quote字段，检查引用的消息内容
            if "Quote" in message:
                quote_info = message.get("Quote", {})
                quote_from = quote_info.get("Nickname", "")

                # 检查被引用的消息是否来自机器人
                for robot_name in self.robot_names:
                    if robot_name == quote_from:
                        logger.debug(f"引用了机器人 '{robot_name}' 的消息")
                        return True

                # 检查引用消息的内容中是否有@机器人
                quote_content = quote_info.get("Content", "")
                for robot_name in self.robot_names:
                    if f"@{robot_name}" in quote_content:
                        logger.debug(f"在引用的消息内容中发现@{robot_name}")
                        return True

            # 如果有OriginalContent，尝试解析XML
            if "OriginalContent" in message:
                try:
                    root = ET.fromstring(message.get("OriginalContent", ""))
                    title = root.find("appmsg/title")
                    if title is not None and title.text:
                        # 检查引用消息的标题中是否包含@机器人
                        for robot_name in self.robot_names:
                            if f"@{robot_name}" in title.text:
                                logger.debug(f"在引用消息标题中发现@{robot_name}")
                                return True
                except Exception as e:
                    logger.debug(f"解析引用消息 XML 失败: {e}")

            # 特殊处理：如果消息内容中包含机器人名称（不带@符号）
            for robot_name in self.robot_names:
                if robot_name in content:
                    logger.debug(f"在引用消息内容中发现机器人名称: {robot_name}")
                    return True

        # 检查消息的Ats字段，这是一个直接的@标记
        if "Ats" in message and message["Ats"] and bot_wxid:
            logger.debug(f"消息包含Ats字段: {message['Ats']}")
            # 如果机器人的wxid在Ats列表中，则返回True
            if bot_wxid in message["Ats"]:
                logger.debug(f"在Ats字段中发现机器人的wxid: {bot_wxid}")
                return True

        if bot_wxid and bot_nickname:
            if f"@{bot_nickname}" in content:
                logger.debug(f"在消息中发现@了bot的NickName: @{bot_nickname}")
                return True

        return False

    async def dify(self, bot: WechatAPIClient, message: dict, query: str, files=None, specific_model=None):
        """发送消息到Dify API"""
        if files is None:
            files = []

        # 如果提供了specific_model，直接使用；否则根据消息内容选择智能体
        if specific_model:
            model = specific_model
            processed_query = query
            is_switch = False
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.info(f"使用指定的智能体 '{model_name}'")
        else:
            # 根据消息内容选择智能体
            # model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
            model, processed_query, is_switch, wakeup_detected = self.get_model_from_message(
                query,
                message["SenderWxid"],
                message["FromWxid"] if message["IsGroup"] else None
            )
            # 如果是切换智能体的命令
            if is_switch:
                model_name = next(name for name, config in self.models.items() if config == model)
                await bot.send_text_message(
                    message["FromWxid"],
                    f"已切换到{model_name.upper()}智能体，将一直使用该智能体直到下次切换。"
                )
                return
            if model is None:
                return
            model_name = next((name for name, config in self.models.items() if config == model), '默认')
            logger.info(f"从消息内容选择智能体 '{model_name}'")

        # 记录将要使用的智能体配置
        logger.info(f"智能体API密钥: {model.api_key[:5]}...{model.api_key[-5:] if len(model.api_key) > 10 else ''}")
        logger.info(f"智能体API端点: {model.base_url}")

        # 处理文件上传
        formatted_files = []
        for file_info in files:
            if isinstance(file_info, dict) and "id" in file_info and "type" in file_info:
                # 新格式，已包含类型信息
                formatted_files.append({
                    "type": file_info["type"],
                    "transfer_method": "local_file",
                    "upload_file_id": file_info["id"]
                })
            else:
                # 兼容旧格式，假设是图片ID
                formatted_files.append({
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": file_info
                })

        # 检查是否有缓存的文件
        cached_file = await self.get_cached_file(message["SenderWxid"])
        if cached_file:
            file_content, file_name, mime_type = cached_file
            logger.info(f"发现缓存文件，准备上传到 Dify: {file_name}, 大小: {len(file_content)} 字节")

            # 上传文件到 Dify
            file_info = await self.upload_file_to_dify(file_content, file_name, mime_type, message["SenderWxid"],
                                                       model_config=model)
            if file_info:
                logger.info(f"成功上传缓存文件到 Dify，文件ID: {file_info['id']}, 类型: {file_info['type']}")
                formatted_files.append({
                    "type": file_info["type"],
                    "transfer_method": "local_file",
                    "upload_file_id": file_info["id"]
                })

        try:
            logger.debug(f"开始调用 Dify API - 用户消息: {processed_query}")
            logger.debug(f"文件列表: {formatted_files}")

            # 获取会话ID
            user_wxid = message["SenderWxid"]
            from_wxid = message["FromWxid"]

            # 对于群聊消息，可以选择使用群聊ID或发送者ID作为会话ID的键
            if message["IsGroup"]:
                # 检查配置，决定使用群聊ID还是发送者ID
                # 默认使用群聊ID作为会话ID的键，这与原始行为一致
                use_group_id = True

                if use_group_id:
                    # 使用群聊ID作为会话ID的键
                    logger.debug(f"群聊消息，使用群聊ID '{from_wxid}' 获取会话ID")
                    conversation_id = self.db.get_llm_thread_id(from_wxid, namespace="dify")
                else:
                    # 使用发送者的wxid作为会话ID的键
                    logger.debug(f"群聊消息，使用发送者wxid '{user_wxid}' 获取会话ID")
                    conversation_id = self.db.get_llm_thread_id(user_wxid, namespace="dify")
            else:
                # 私聊消息，使用原来的FromWxid
                conversation_id = self.db.get_llm_thread_id(from_wxid, namespace="dify")

            try:
                user_username = await bot.get_nickname(user_wxid) or "未知用户"
            except:
                user_username = "未知用户"

            inputs = {
                "user_wxid": user_wxid,
                "user_username": user_username,
                "from_wxid": from_wxid
            }

            # 根据是否支持Agent模式，设置不同的请求参数
            # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
            user_id = from_wxid if message["IsGroup"] else user_wxid

            payload = {
                "inputs": inputs,
                "query": processed_query,
                "response_mode": "streaming",  # 始终使用流式响应
                "conversation_id": conversation_id,
                "user": user_id,  # 对于群聊使用群聊ID，对于私聊使用发送者的wxid
                "files": formatted_files,
                "auto_generate_name": False,
            }

            # 决定是使用API代理还是直接连接
            use_api_proxy = self.api_proxy is not None and has_api_proxy
            logger.debug(
                f"发送请求到 Dify（智能体：{model_name}） - URL: {model.base_url}/chat-messages, Payload: {json.dumps(payload)}")

            if use_api_proxy:
                # 使用API代理调用
                logger.info(f"通过API代理调用Dify")
                try:
                    # 检查是否有对应的注册API
                    base_url_without_v1 = model.base_url.rstrip("/v1")
                    endpoint = model.base_url.replace(base_url_without_v1, "")
                    endpoint = endpoint + "/chat-messages"

                    # 准备请求
                    api_response = await self.api_proxy.call_api(
                        api_type="dify",
                        endpoint=endpoint,
                        data=payload,
                        method="POST",
                        headers={"Authorization": f"Bearer {model.api_key}"}
                    )

                    if api_response.get("success") is False:
                        logger.error(f"API代理调用失败: {api_response.get('error')}")
                        # 失败时回退到直接调用
                        use_api_proxy = False
                    else:
                        # API代理不支持流式响应，处理非流式返回的结果
                        ai_resp = api_response.get("data", {}).get("answer", "")
                        new_con_id = api_response.get("data", {}).get("conversation_id", "")
                        # 根据消息类型选择正确的ID来保存会话ID
                        if message["IsGroup"]:
                            # 群聊消息，使用群聊ID
                            self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")
                            logger.debug(f"群聊消息，保存会话ID到群聊ID: {message['FromWxid']}")
                        else:
                            # 私聊消息，使用原来的FromWxid
                            self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")

                        # 过滤掉思考标签
                        think_pattern = r'<think>.*?</think>'
                        ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                        logger.debug(f"API代理返回(过滤思考标签后): {ai_resp[:100]}...")

                        if ai_resp:
                            # 获取消息ID，如果有的话
                            message_id = api_response.get("data", {}).get("message_id")
                            if message_id:
                                logger.debug(f"API代理返回消息ID: {message_id}")
                                await self.dify_handle_text(bot, message, ai_resp, model, message_id=message_id)
                            else:
                                await self.dify_handle_text(bot, message, ai_resp, model)
                        else:
                            logger.warning("API代理未返回有效响应")
                            # 回退到直接调用
                            use_api_proxy = False
                except Exception as e:
                    logger.error(f"API代理调用异常: {e}")
                    logger.error(traceback.format_exc())
                    # 出错时回退到直接调用
                    use_api_proxy = False

            # 如果API代理不可用或调用失败，使用直接连接
            if not use_api_proxy:
                headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
                ai_resp = ""
                async with aiohttp.ClientSession() as session:
                    # 正确的方式是在请求时设置代理，而不是在创建会话时
                    proxy = self.http_proxy if self.http_proxy else None
                    async with session.post(url=f"{model.base_url}/chat-messages", headers=headers,
                                            data=json.dumps(payload), proxy=proxy) as resp:
                        if resp.status in (200, 201):
                            async for line in resp.content:
                                line = line.decode("utf-8").strip()
                                if not line or line == "event: ping":
                                    continue
                                elif line.startswith("data: "):
                                    line = line[6:]
                                try:
                                    resp_json = json.loads(line)
                                except json.JSONDecodeError:
                                    logger.error(f"Dify返回的JSON解析错误: {line}")
                                    continue

                                event = resp_json.get("event", "")
                                if event == "message":
                                    ai_resp += resp_json.get("answer", "")
                                elif event == "message_replace":
                                    ai_resp = resp_json.get("answer", "")
                                elif event == "message_end":
                                    # 在消息结束时过滤掉思考标签
                                    think_pattern = r'<think>.*?</think>'
                                    ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                                    logger.debug(f"消息结束时过滤思考标签")
                                elif event == "message_file":
                                    file_url = resp_json.get("url", "")
                                    file_id = resp_json.get("id", "")
                                    file_type = resp_json.get("type", "image")
                                    belongs_to = resp_json.get("belongs_to", "assistant")

                                    # 存储文件信息
                                    self.agent_files[file_id] = {
                                        "url": file_url,
                                        "type": file_type,
                                        "belongs_to": belongs_to
                                    }

                                    # 处理文件
                                    if file_type == "image":
                                        await self.dify_handle_image(bot, message, file_url, model_config=model)
                                    else:
                                        logger.info(f"收到非图片类型文件: {file_type}, ID: {file_id}, URL: {file_url}")
                                elif event == "agent_thought":
                                    # 处理Agent思考过程
                                    if self.support_agent_mode:
                                        thought_id = resp_json.get("id", "")
                                        message_id = resp_json.get("message_id", "")
                                        conversation_id = resp_json.get("conversation_id", "")
                                        position = resp_json.get("position", 0)
                                        thought = resp_json.get("thought", "")
                                        observation = resp_json.get("observation", "")
                                        tool = resp_json.get("tool", "")
                                        tool_input = resp_json.get("tool_input", "")
                                        message_files = resp_json.get("message_files", [])

                                        # 记录思考过程
                                        if conversation_id not in self.current_agent_thoughts:
                                            self.current_agent_thoughts[conversation_id] = []

                                        self.current_agent_thoughts[conversation_id].append({
                                            "id": thought_id,
                                            "message_id": message_id,
                                            "position": position,
                                            "thought": thought,
                                            "observation": observation,
                                            "tool": tool,
                                            "tool_input": tool_input,
                                            "files": message_files
                                        })

                                        logger.debug(f"Agent思考: {thought[:100]}...")
                                        if tool:
                                            logger.debug(f"使用工具: {tool}, 输入: {tool_input}")
                                        if observation:
                                            logger.debug(f"观察结果: {observation[:100]}...")
                                elif event == "agent_message":
                                    # 处理Agent消息
                                    if self.support_agent_mode:
                                        answer = resp_json.get("answer", "")
                                        ai_resp += answer
                                        logger.debug(f"Agent消息: {answer}")
                                elif event == "error":
                                    await self.dify_handle_error(bot, message,
                                                                 resp_json.get("task_id", ""),
                                                                 resp_json.get("message_id", ""),
                                                                 resp_json.get("status", ""),
                                                                 resp_json.get("code", ""),
                                                                 resp_json.get("message", ""))

                            new_con_id = resp_json.get("conversation_id", "")
                            if new_con_id and new_con_id != conversation_id:
                                # 根据消息类型选择正确的ID来保存会话ID
                                if message["IsGroup"]:
                                    # 群聊消息，使用群聊ID
                                    self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")
                                    logger.debug(f"群聊消息，保存会话ID到群聊ID: {message['FromWxid']}")
                                else:
                                    # 私聊消息，使用原来的FromWxid
                                    self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")
                            ai_resp = ai_resp.rstrip()

                            # 最后再次过滤思考标签，确保完全移除
                            think_pattern = r'<think>.*?</think>'
                            ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                            logger.debug(f"Dify响应(过滤思考标签后): {ai_resp[:100]}...")
                        elif resp.status == 404:
                            logger.warning("会话ID不存在，重置会话ID并重试")
                            # 根据消息类型选择正确的ID来重置会话ID
                            if message["IsGroup"]:
                                # 群聊消息，使用群聊ID
                                self.db.save_llm_thread_id(message["FromWxid"], "", "dify")
                                logger.debug(f"群聊消息，重置会话ID，群聊ID: {message['FromWxid']}")
                            else:
                                # 私聊消息，使用原来的FromWxid
                                self.db.save_llm_thread_id(message["FromWxid"], "", "dify")
                            # 重要：在递归调用时必须传递原始智能体，不要重新选择
                            return await self.dify(bot, message, processed_query, files=formatted_files,
                                                   specific_model=model)
                        elif resp.status == 400:
                            # 先获取错误内容
                            error_text = await resp.content.read()
                            error_text_str = error_text.decode('utf-8')

                            logger.warning(f"收到{resp.status}错误，完整错误信息: {error_text_str}")

                            # 强制重置会话ID，无论错误类型如何
                            # 这是一个更激进的解决方案，但可以确保会话ID被重置
                            logger.warning(f"收到{resp.status}错误，强制重置会话ID")

                            # 重置会话ID
                            # 根据消息类型选择正确的ID来重置会话ID
                            if message.get("IsGroup", False):
                                # 群聊消息，使用群聊ID
                                from_wxid = message.get("FromWxid", "")
                                if from_wxid:
                                    # 确保完全清除会话ID
                                    self.db.save_llm_thread_id(from_wxid, "", "dify")
                                    logger.info(f"已重置群聊 {from_wxid} 的会话ID")
                            else:
                                # 私聊消息，使用原来的FromWxid
                                from_wxid = message.get("FromWxid", "")
                                if from_wxid:
                                    # 确保完全清除会话ID
                                    self.db.save_llm_thread_id(from_wxid, "", "dify")
                                    logger.info(f"已重置私聊用户 {from_wxid} 的会话ID")

                            # 通知用户
                            await bot.send_text_message(
                                message["FromWxid"],
                                f"{XYBOT_PREFIX}检测到对话异常，已重置对话。正在重新处理您的问题..."
                            )

                            # 等待一小段时间，确保数据库操作完成
                            await asyncio.sleep(1)

                            # 创建一个新的会话ID
                            new_conversation_id = str(uuid.uuid4())
                            logger.info(f"生成新的会话ID: {new_conversation_id}")

                            # 保存新的会话ID
                            if message.get("IsGroup", False):
                                # 群聊消息，使用群聊ID
                                self.db.save_llm_thread_id(message.get("FromWxid", ""), new_conversation_id, "dify")
                            else:
                                # 私聊消息，使用原来的FromWxid
                                self.db.save_llm_thread_id(message.get("FromWxid", ""), new_conversation_id, "dify")

                            # 修改payload，使用新的会话ID
                            payload["conversation_id"] = new_conversation_id
                            logger.info(f"更新payload中的会话ID为: {new_conversation_id}")

                            # 重新发送请求，使用新的会话ID
                            logger.info("使用新会话ID重新发送请求")

                            # 重新构建请求
                            headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
                            ai_resp = ""

                            # 重新发送请求
                            logger.debug(
                                f"重新发送请求到 Dify - URL: {model.base_url}/chat-messages, 新会话ID: {new_conversation_id}")
                            async with aiohttp.ClientSession() as new_session:
                                # 正确的方式是在请求时设置代理，而不是在创建会话时
                                proxy = self.http_proxy if self.http_proxy else None
                                async with new_session.post(url=f"{model.base_url}/chat-messages", headers=headers,
                                                            data=json.dumps(payload), proxy=proxy) as new_resp:
                                    if new_resp.status in (200, 201):
                                        # 处理成功响应
                                        logger.info("使用新会话ID的请求成功")
                                        # 读取响应内容
                                        async for line in new_resp.content:
                                            line = line.decode("utf-8").strip()
                                            if not line or line == "event: ping":
                                                continue
                                            elif line.startswith("data: "):
                                                line = line[6:]
                                            try:
                                                resp_json = json.loads(line)
                                                event = resp_json.get("event", "")
                                                if event == "message":
                                                    ai_resp += resp_json.get("answer", "")
                                                elif event == "message_end":
                                                    # 处理消息结束事件
                                                    think_pattern = r'<think>.*?</think>'
                                                    ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                                            except json.JSONDecodeError:
                                                logger.error(f"重试请求返回的JSON解析错误: {line}")
                                                continue

                                        # 处理响应
                                        if ai_resp:
                                            await self.dify_handle_text(bot, message, ai_resp, model)
                                            return
                                        else:
                                            logger.warning("重试请求未返回有效响应")
                                    else:
                                        # 如果重试仍然失败，放弃并通知用户
                                        error_msg = await new_resp.text()
                                        logger.error(f"重试请求失败: HTTP {new_resp.status} - {error_msg}")
                                        await bot.send_text_message(
                                            message["FromWxid"],
                                            f"{XYBOT_PREFIX}重试请求失败，请稍后再试。"
                                        )
                                        return

                            # 如果执行到这里，说明重试失败，回退到原始方法
                            return await self.dify(bot, message, processed_query, files=files, specific_model=model)
                        elif resp.status == 500:
                            return await self.handle_500(bot, message)
                        else:
                            return await self.handle_other_status(bot, message, resp)

                if ai_resp:
                    # 获取消息ID，如果有的话
                    message_id = resp_json.get("message_id")
                    if message_id:
                        logger.debug(f"Dify API返回消息ID: {message_id}")
                        await self.dify_handle_text(bot, message, ai_resp, model, message_id=message_id)
                    else:
                        await self.dify_handle_text(bot, message, ai_resp, model)
                else:
                    logger.warning("Dify未返回有效响应")
        except Exception as e:
            logger.error(f"Dify API 调用失败: {e}")
            await self.handle_exceptions(bot, message, model_config=model)

    async def download_file(self, url: str) -> bytes:
        """
        下载文件并返回文件内容
        """
        try:
            logger.info(f"开始下载文件: {url}")
            async with aiohttp.ClientSession() as session:
                # 正确的方式是在请求时设置代理，而不是在创建会话时
                proxy = self.http_proxy if self.http_proxy else None
                async with session.get(url, proxy=proxy) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        logger.info(f"文件下载成功，大小: {len(content)} 字节")
                        return content
                    else:
                        logger.error(f"文件下载失败: HTTP {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"下载文件时发生错误: {e}")
            logger.error(traceback.format_exc())
            return None

    async def upload_file_to_dify(self, file_content: bytes, file_name: str, mime_type: str, user: str,
                                  model_config=None) -> Optional[dict]:
        """
        上传文件到Dify并返回文件信息
        返回格式: {"id": "uuid", "type": "image|document|audio|video"}
        """
        logger.info(
            f"开始上传文件到Dify, 用户: {user}, 文件名: {file_name}, 文件大小: {len(file_content)} 字节, MIME类型: {mime_type}")

        if not file_content or len(file_content) == 0:
            logger.error("文件内容为空，无法上传")
            return None

        try:
            # 判断文件类型
            file_extension = os.path.splitext(file_name)[1].lower().lstrip('.')
            if not file_extension:
                # 如果文件名没有扩展名，尝试从 MIME 类型推断
                file_extension = mime_type.split('/')[-1].lower()

            # 确定文件类型
            # 根据 Dify 文档，支持的文件类型如下：
            # document: 'TXT', 'MD', 'MARKDOWN', 'PDF', 'HTML', 'XLSX', 'XLS', 'DOCX', 'CSV', 'EML', 'MSG', 'PPTX', 'PPT', 'XML', 'EPUB'
            # image: 'JPG', 'JPEG', 'PNG', 'GIF', 'WEBP', 'SVG'
            # audio: 'MP3', 'M4A', 'WAV', 'WEBM', 'AMR'
            # video: 'MP4', 'MOV', 'MPEG', 'MPGA'
            # custom: 其他文件类型

            # 文档类型列表 - 根据 Dify 文档
            document_extensions = ['txt', 'md', 'markdown', 'pdf', 'html', 'xlsx', 'xls', 'docx', 'csv', 'eml', 'msg',
                                   'pptx', 'ppt', 'xml', 'epub']
            # 根据文档，Dify 确实支持 'ppt' 格式
            # 图片类型列表
            image_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg']
            # 音频类型列表
            audio_extensions = ['mp3', 'm4a', 'wav', 'webm', 'amr']
            # 视频类型列表
            video_extensions = ['mp4', 'mov', 'mpeg', 'mpga']

            # 默认使用 custom 类型
            file_type = "custom"

            # 根据文件扩展名判断类型
            if file_extension in document_extensions or mime_type.startswith('application/') or mime_type.startswith(
                    'text/'):
                file_type = "document"
                # 特殊处理 PPT 文件
                if file_extension == 'ppt' or file_name.lower().endswith(
                        '.ppt') or mime_type == 'application/vnd.ms-powerpoint':
                    logger.info(f"检测到 PPT 文件，使用 document 类型上传")
            elif file_extension in image_extensions or mime_type.startswith('image/'):
                file_type = "image"
                # 处理图片文件
                try:
                    # 尝试打开图片数据
                    # 特别处理截断的图片文件
                    from PIL import ImageFile
                    ImageFile.LOAD_TRUNCATED_IMAGES = True  # 允许加载截断的图片

                    # 使用BytesIO确保完整读取图片数据
                    image_io = io.BytesIO(file_content)
                    image = Image.open(image_io)
                    logger.debug(f"原始图片格式: {image.format}, 大小: {image.size}, 模式: {image.mode}")

                    # 转换为RGB模式(去除alpha通道)
                    if image.mode in ('RGBA', 'LA'):
                        logger.debug(f"图片包含alpha通道，转换为RGB模式")
                        background = Image.new('RGB', image.size, (255, 255, 255))
                        background.paste(image, mask=image.split()[-1])
                        image = background

                    # 检查图片大小，如果太大则调整大小
                    max_dimension = 1600  # 最大尺寸限制
                    max_file_size = 1024 * 1024 * 2  # 2MB大小限制

                    # 调整图片尺寸
                    width, height = image.size
                    if width > max_dimension or height > max_dimension:
                        # 计算缩放比例
                        ratio = min(max_dimension / width, max_dimension / height)
                        new_width = int(width * ratio)
                        new_height = int(height * ratio)
                        logger.info(f"图片尺寸过大，调整大小从 {width}x{height} 到 {new_width}x{new_height}")
                        image = image.resize((new_width, new_height), Image.LANCZOS)

                    # 保存为JPEG，尝试不同的质量级别以满足大小限制
                    quality = 95
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=quality, optimize=True)
                    output.seek(0)
                    resized_content = output.getvalue()

                    # 如果文件仍然太大，逐步降低质量
                    while len(resized_content) > max_file_size and quality > 50:
                        quality -= 10
                        output = io.BytesIO()
                        image.save(output, format='JPEG', quality=quality, optimize=True)
                        output.seek(0)
                        resized_content = output.getvalue()
                        logger.debug(f"降低图片质量到 {quality}，新大小: {len(resized_content)} 字节")

                    file_content = resized_content
                    mime_type = 'image/jpeg'
                    file_extension = 'jpg'
                    logger.info(f"图片处理成功，质量: {quality}，新大小: {len(file_content)} 字节")

                    # 验证处理后的图片
                    try:
                        Image.open(io.BytesIO(file_content))
                        logger.debug("处理后的图片验证成功")
                    except Exception as e:
                        logger.error(f"处理后的图片验证失败: {e}")
                        # 如果处理后的图片无效，尝试使用原始图片数据
                        file_content = image_io.getvalue()
                        logger.warning(f"使用原始图片数据上传，大小: {len(file_content)} 字节")
                except Exception as e:
                    logger.error(f"图片格式转换失败: {e}")
                    logger.error(traceback.format_exc())
                    # 尝试使用原始数据上传，但先验证原始数据是否为有效图片
                    try:
                        Image.open(io.BytesIO(file_content))
                        logger.warning("原始图片数据有效，将直接使用原始数据上传")
                    except Exception as img_error:
                        logger.error(f"原始图片数据无效: {img_error}")
                        # 如果原始数据也无效，返回None
                        return None
            elif file_extension in audio_extensions or mime_type.startswith('audio/'):
                file_type = "audio"
            elif file_extension in video_extensions or mime_type.startswith('video/'):
                file_type = "video"

            logger.info(f"文件类型判断: {file_type}, 扩展名: {file_extension}")

            # 使用传入的model_config，如果没有则使用默认智能体
            model = model_config or self.current_model
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.debug(f"使用智能体 '{model_name}' 上传文件")

            # 检查API密钥
            if not model.api_key:
                logger.error(f"智能体 '{model_name}' 的API密钥未配置，无法上传文件")
                return None

            # 决定是使用API代理还是直接连接
            use_api_proxy = self.api_proxy is not None and has_api_proxy and False  # 文件上传暂不使用API代理

            if use_api_proxy:
                # API代理目前不支持文件上传，使用直接连接
                logger.info("文件上传目前不支持API代理，使用直接连接")
                use_api_proxy = False

            # 处理文件名，确保有正确的扩展名
            if file_type == "image" and not file_name.lower().endswith(
                    ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg')):
                processed_file_name = f"image_{int(time.time())}.jpg"
                logger.info(f"更新图片文件名为: {processed_file_name}")
            else:
                # 处理文件名，避免重复的扩展名
                processed_file_name = file_name
                file_extension = os.path.splitext(file_name)[1].lower().lstrip('.')
                base_name = os.path.splitext(file_name)[0]

                # 检查基本名称是否已经包含扩展名
                if base_name.lower().endswith(f".{file_extension}"):
                    # 如果基本名称已经包含扩展名，则去除重复的扩展名
                    processed_file_name = f"{base_name}.{file_extension}"
                    logger.info(f"去除重复的文件扩展名，处理后的文件名: {processed_file_name}")

            # 确保MIME类型与文件类型匹配
            if file_type == "image" and not mime_type.startswith('image/'):
                mime_type = 'image/jpeg'
                logger.info(f"更新MIME类型为: {mime_type}")

            # 使用直接连接上传文件
            headers = {"Authorization": f"Bearer {model.api_key}"}
            formdata = aiohttp.FormData()
            # 使用处理后的文件名
            formdata.add_field("file", file_content,
                               filename=processed_file_name,
                               content_type=mime_type)
            # 确保使用正确的用户ID
            # 如果user是群聊ID（包含@chatroom），则使用它
            # 否则，使用发送者的wxid
            formdata.add_field("user", user)

            url = f"{model.base_url}/files/upload"
            logger.debug(f"开始请求Dify文件上传API: {url}")

            # 设置较长的超时时间
            timeout = aiohttp.ClientTimeout(total=60)  # 60秒超时

            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    # 正确的方式是在请求时设置代理，而不是在创建会话时
                    proxy = self.http_proxy if self.http_proxy else None
                    async with session.post(url, headers=headers, data=formdata, proxy=proxy) as resp:
                        if resp.status in (200, 201):
                            result = await resp.json()
                            file_id = result.get("id")
                            if file_id:
                                logger.info(f"文件上传成功，文件ID: {file_id}, 类型: {file_type}")
                                # 上传成功后删除缓存
                                if user in self.file_cache:
                                    del self.file_cache[user]
                                    logger.debug(f"已清除用户 {user} 的文件缓存")
                                # 清除图片缓存
                                if file_type == "image" and user in self.image_cache:
                                    del self.image_cache[user]
                                    logger.debug(f"已清除用户 {user} 的图片缓存")
                                return {
                                    "id": file_id,
                                    "type": file_type
                                }
                            else:
                                logger.error(f"文件上传成功但未返回文件ID: {result}")
                        else:
                            error_text = await resp.text()
                            logger.error(f"文件上传失败: HTTP {resp.status} - {error_text}")
                            return None
            except aiohttp.ClientError as e:
                logger.error(f"HTTP请求失败: {e}")
                return None
        except Exception as e:
            logger.error(f"上传文件时发生错误: {e}")
            logger.error(traceback.format_exc())
            return None

    async def dify_handle_text(self, bot: WechatAPIClient, message: dict, text: str, model_config=None,
                               message_id=None):
        """
        处理Dify返回的文本消息

        Args:
            bot: WechatAPIClient实例
            message: 消息字典
            text: 要处理的文本内容
            model_config: 智能体配置（可选）
            message_id: Dify生成的消息ID（可选，用于文本转语音）
        """
        # 使用传入的model_config，如果没有则使用默认智能体
        model = model_config or self.current_model

        # 先过滤掉<think>...</think>标签中的内容
        think_pattern = r'<think>.*?</think>'
        text = re.sub(think_pattern, '', text, flags=re.DOTALL)
        logger.debug(f"过滤思考标签后的文本: {text[:100]}...")

        # 获取会话ID，用于查找Agent思考过程
        # 根据消息类型选择正确的ID来获取会话ID
        if message["IsGroup"]:
            # 群聊消息，使用群聊ID
            conversation_id = self.db.get_llm_thread_id(message["FromWxid"], namespace="dify")
            logger.debug(f"群聊消息，从群聊ID获取会话ID: {message['FromWxid']}")
        else:
            # 私聊消息，使用原来的FromWxid
            conversation_id = self.db.get_llm_thread_id(message["FromWxid"], namespace="dify")

        # 如果启用了Agent模式且有思考过程，可以在这里处理
        if self.support_agent_mode and conversation_id in self.current_agent_thoughts:
            thoughts = self.current_agent_thoughts[conversation_id]
            if thoughts:
                logger.debug(f"发现Agent思考过程，共{len(thoughts)}条")
                # 这里可以根据需要处理思考过程，例如添加到消息中
                # 例如：添加使用的工具信息
                tools_used = []
                for thought in thoughts:
                    if thought.get("tool") and thought["tool"] not in tools_used:
                        tools_used.append(thought["tool"])

                if tools_used:
                    logger.info(f"Agent使用了以下工具: {', '.join(tools_used)}")

                # 清除已处理的思考过程
                self.current_agent_thoughts[conversation_id] = []

        # 匹配Dify返回的Markdown链接格式 [文件名](URL) 和 ![文件名](URL)
        link_pattern = r'!?\[(.*?)\]\((.*?)\)'
        matches = re.findall(link_pattern, text)

        # 记录所有找到的链接
        if matches:
            logger.info(f"[文件处理] 在回复中找到 {len(matches)} 个文件链接")
            for i, (filename, url) in enumerate(matches):
                logger.info(f"[文件处理] 链接 {i + 1}: 文件名='{filename}', URL='{url}'")
        # 移除所有链接文本，以免重复显示
        text = re.sub(link_pattern, '', text)

        # 移除行首字符为标点符号后面跟换行的
        pattern = r'^([\u2000-\u206F\u2E00-\u2E7F\'!"#$%&()*+,\-./:;<=>?@[\]^_`{|}~]+)(\r\n|\n|\r)'
        while re.search(pattern, text, re.MULTILINE):
            text = re.sub(pattern, '', text, flags=re.MULTILINE)

        # 移除粗体、斜体、代码块、链接、图片等基础格式
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)  # 粗体
        text = re.sub(r'\*(.*?)\*', r'\1', text)  # 斜体
        text = re.sub(r'`(.*?)`', r'\1', text)  # 行内代码
        text = re.sub(r'\!$$(.*?)$$$.*?$', r'\1', text)  # 图片
        text = re.sub(r'$$(.*?)$$$.*?$', r'\1', text)  # 链接
        # 移除标题
        text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
        # 移除列表标记
        text = re.sub(r'^[\*\-\+] ', '', text, flags=re.MULTILINE)
        # 移除代码块
        text = re.sub(r'```.*?\n.*?```', '', text, flags=re.DOTALL)
        # 移除引用
        text = re.sub(r'^> ', '', text, flags=re.MULTILINE)
        # 移除水平线
        text = re.sub(r'^[-*_]{3,}', '', text, flags=re.MULTILINE)

        # 先发送文字内容
        if text:
            # 检查是否需要发送语音消息
            if message["MsgType"] == 34 or self.voice_reply_all:
                # 获取消息ID，如果有的话
                agent_message_id = None
                if self.support_agent_mode and conversation_id in self.current_agent_thoughts:
                    thoughts = self.current_agent_thoughts[conversation_id]
                    if thoughts and thoughts[-1].get("message_id"):
                        agent_message_id = thoughts[-1].get("message_id")
                        logger.debug(f"找到Agent消息ID: {agent_message_id}，将用于文本转语音")

                # 使用message_id或text调用文本转语音
                await self.text_to_voice_message(bot, message, text=text, message_id=agent_message_id)
            else:
                # 使用 //n 作为分隔符进行分段发送
                paragraphs = text.split("//n")
                logger.info(f"检测到 //n 分隔符，将消息分为 {len(paragraphs)} 段发送")

                should_quote = False
                quoted_msg_id = message.get("MsgId", "")
                quoted_wxid = message.get("SenderWxid", "")
                quoted_content = message.get("Content", "")

                # 尝试获取引用消息的发送者昵称
                try:
                    quoted_nickname = await bot.get_nickname(quoted_wxid) or "未知用户"
                except:
                    quoted_nickname = "未知用户"

                # 如果有消息ID且内容不是太长，使用引用回复
                if quoted_msg_id and quoted_wxid and quoted_content and len(quoted_content) <= 100:
                    should_quote = True
                    logger.info(f"将使用普通消息引用回复，引用MsgId={quoted_msg_id}")

                for i, paragraph in enumerate(paragraphs):
                    if paragraph.strip():
                        logger.debug(f"发送第 {i + 1}/{len(paragraphs)} 段消息，长度: {len(paragraph.strip())} 字符")

                        # 群聊判断如果CSRS标记，则@人工座席微信
                        if message["IsGroup"]:
                            group_id = message["FromWxid"]
                            if '@@@CSRS@@@' in paragraph:
                                logger.debug(f'发现@CSRS标记，正在获取csrs')
                                paragraph = paragraph.replace('@@@CSRS@@@', '')
                                # 只对第一段使用引用回复
                                if should_quote and i == 0:
                                    await self.send_quote_message(
                                        bot,
                                        message["FromWxid"],
                                        paragraph.strip(),
                                        quoted_msg_id,
                                        quoted_wxid,
                                        quoted_nickname,
                                        quoted_content[:100]  # 截断过长的引用内容
                                    )
                                else:
                                    await bot.send_at_message(message["FromWxid"], paragraph.strip(),
                                                              [message["SenderWxid"]])
                                groups_config = self.groupid_to_groupsconfig.get(group_id, {})
                                members = await bot.get_chatroom_member_list(group_id)
                                csrs = [member['UserName'] for member in members
                                        if member.get('UserName') in groups_config.csrs]
                                if len(csrs) > 0:
                                    logger.debug(f'找到csrs: {csrs}，并@csrs。')
                                    await bot.send_at_message(message["FromWxid"], '', csrs)
                            else:
                                # 只对第一段使用引用回复
                                if should_quote and i == 0:
                                    await self.send_quote_message(
                                        bot,
                                        message["FromWxid"],
                                        paragraph.strip(),
                                        quoted_msg_id,
                                        quoted_wxid,
                                        quoted_nickname,
                                        quoted_content[:100]  # 截断过长的引用内容
                                    )
                                else:
                                    await bot.send_at_message(message["FromWxid"], paragraph.strip(),
                                                              [message["SenderWxid"]])

                        else:
                            if '@@@CSRS@@@' in paragraph:
                                paragraph = paragraph.replace('@@@CSRS@@@', '')
                            send_message = paragraph.strip()
                            send_message = self.reply_title + send_message
                            # 只对第一段使用引用回复
                            if should_quote and i == 0:
                                await self.send_quote_message(
                                    bot,
                                    message["FromWxid"],
                                    send_message,
                                    quoted_msg_id,
                                    quoted_wxid,
                                    quoted_nickname,
                                    quoted_content[:100]  # 截断过长的引用内容
                                )
                            else:
                                await bot.send_text_message(message["FromWxid"], send_message)

                        if i < len(paragraphs) - 1:  # 如果不是最后一段
                            await asyncio.sleep(0.5)  # 添加0.5秒延迟

        # 处理所有找到的链接
        for filename, url in matches:
            try:
                # 如果URL是相对路径,添加base_url
                if url.startswith('/files') or url.startswith('./files'):
                    # 移除base_url中可能的v1路径
                    base_url = model.base_url.replace('/v1', '')
                    if url.startswith('./'):
                        url = url[1:]  # 移除开头的点
                    url = f"{base_url}{url}"

                logger.info(f"[文件处理] 开始下载文件: {filename}, URL: {url}")

                # 设置请求头
                headers = {"Authorization": f"Bearer {model.api_key}"}

                # 下载文件
                async with aiohttp.ClientSession() as session:
                    # 正确的方式是在请求时设置代理，而不是在创建会话时
                    proxy = self.http_proxy if self.http_proxy else None
                    async with session.get(url, headers=headers, proxy=proxy) as resp:
                        if resp.status == 200:
                            # 获取内容类型
                            content_type = resp.headers.get('Content-Type', '')
                            logger.info(f"[文件处理] 下载成功: 状态码={resp.status}, 内容类型={content_type}")

                            # 读取文件内容
                            file_content = await resp.read()
                            logger.info(f"[文件处理] 文件大小: {len(file_content)} 字节")

                            # 保存一份用于调试
                            # debug_file = f"debug_file_{int(time.time())}_{os.path.basename(url)}"
                            # try:
                            #     with open(debug_file, "wb") as f:
                            #         f.write(file_content)
                            #     logger.info(f"[文件处理] 已保存调试文件: {debug_file}")
                            # except Exception as save_error:
                            #     logger.error(f"[文件处理] 保存调试文件失败: {save_error}")

                            # 根据内容类型或文件扩展名确定文件类型
                            file_type = None

                            # 首先尝试使用文件内容检测类型
                            kind = filetype.guess(file_content)
                            if kind:
                                file_type = kind.mime
                                ext = kind.extension
                                logger.info(f"[文件处理] 通过内容检测到文件类型: {file_type}, 扩展名: {ext}")
                            else:
                                # 尝试从Content-Type头获取
                                if content_type and content_type != 'application/octet-stream':
                                    file_type = content_type
                                    ext = mimetypes.guess_extension(content_type)
                                    if ext:
                                        ext = ext.lstrip('.')
                                    else:
                                        ext = ""
                                    logger.info(f"[文件处理] 从Content-Type获取文件类型: {file_type}, 扩展名: {ext}")
                                else:
                                    # 尝试从文件名获取扩展名
                                    ext = os.path.splitext(filename)[1].lower().lstrip('.')
                                    if not ext and '.' in url:
                                        ext = os.path.splitext(url)[1].lower().lstrip('.')

                                    if ext:
                                        file_type = mimetypes.guess_type(f"file.{ext}")[0]
                                        logger.info(f"[文件处理] 从文件名获取类型: {file_type}, 扩展名: {ext}")
                                    else:
                                        # 无法确定类型
                                        file_type = 'application/octet-stream'
                                        ext = 'bin'
                                        logger.warning(f"[文件处理] 无法确定文件类型，使用默认值: {file_type}")

                            # 创建临时目录用于处理文件
                            temp_dir = os.path.join(os.getcwd(), "temp")
                            os.makedirs(temp_dir, exist_ok=True)
                            temp_filename = os.path.join(temp_dir, f"{int(time.time())}_{filename}")

                            try:
                                # 保存临时文件
                                with open(temp_filename, "wb") as f:
                                    f.write(file_content)
                                logger.debug(f"[文件处理] 已保存临时文件: {temp_filename}")

                                # 根据文件类型发送不同类型的消息
                                if file_type and (
                                        file_type.startswith('audio/') or ext in ('wav', 'mp3', 'ogg', 'm4a', 'amr')):
                                    # 音频文件
                                    logger.info(f"[文件处理] 检测到音频文件，发送语音消息")

                                    # 对于音频文件，可能需要转换格式
                                    try:
                                        # 检查是否有ffmpeg
                                        if shutil.which("ffmpeg"):
                                            # 转换为mp3格式，这是微信支持较好的格式
                                            mp3_file = f"{temp_filename}.mp3"
                                            command = f'ffmpeg -y -i "{temp_filename}" -acodec libmp3lame -ar 44100 -ab 192k "{mp3_file}"'
                                            logger.debug(f"[文件处理] 执行音频转换命令: {command}")

                                            process = subprocess.run(command, shell=True, check=True,
                                                                     capture_output=True, text=True)
                                            if process.returncode == 0:
                                                logger.info(f"[文件处理] 音频转换成功: {mp3_file}")

                                                # 读取转换后的文件
                                                with open(mp3_file, "rb") as f:
                                                    converted_audio = f.read()

                                                # 发送转换后的音频
                                                await bot.send_voice_message(message["FromWxid"], voice=converted_audio,
                                                                             format="mp3")
                                                logger.info(f"[文件处理] 发送转换后的语音消息成功")

                                                # 删除转换后的文件
                                                try:
                                                    os.remove(mp3_file)
                                                    logger.debug(f"[文件处理] 已删除转换后的音频文件: {mp3_file}")
                                                except Exception as del_error:
                                                    logger.debug(f"[文件处理] 删除转换后的音频文件失败: {del_error}")
                                            else:
                                                logger.warning(f"[文件处理] 音频转换失败: {process.stderr}")
                                                # 尝试直接发送原始音频
                                                await bot.send_voice_message(message["FromWxid"], voice=file_content,
                                                                             format=ext or 'mp3')
                                                logger.info(f"[文件处理] 发送原始语音消息成功")
                                        else:
                                            logger.warning("[文件处理] 未找到ffmpeg，直接发送原始音频")
                                            await bot.send_voice_message(message["FromWxid"], voice=file_content,
                                                                         format=ext or 'mp3')
                                            logger.info(f"[文件处理] 发送原始语音消息成功")
                                    except Exception as audio_error:
                                        logger.error(f"[文件处理] 处理音频文件失败: {audio_error}")
                                        logger.error(traceback.format_exc())
                                        # 尝试直接发送原始音频
                                        await bot.send_voice_message(message["FromWxid"], voice=file_content,
                                                                     format=ext or 'mp3')
                                        logger.info(f"[文件处理] 尝试直接发送原始语音消息")

                                elif file_type and (file_type.startswith('image/') or ext in (
                                        'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg')):
                                    # 图片文件
                                    logger.info(f"[文件处理] 检测到图片文件，发送图片消息")
                                    await bot.send_image_message(message["FromWxid"], file_content)
                                    logger.info(f"[文件处理] 发送图片消息成功")

                                elif file_type and (file_type.startswith('video/') or ext in (
                                        'mp4', 'avi', 'mov', 'mkv', 'flv', 'webm')):
                                    # 视频文件
                                    logger.info(f"[文件处理] 检测到视频文件，发送视频消息")
                                    await bot.send_video_message(message["FromWxid"], video=file_content, image="None")
                                    logger.info(f"[文件处理] 发送视频消息成功")

                                else:
                                    # 其他类型文件，855,Mac暂不做处理
                                    logger.info(f"[文件处理] 检测到其他类型文件: {file_type}，855/Mac暂不处理")
                                    # logger.info(f"[文件处理] 检测到其他类型文件: {file_type}，发送文件中")
                                    # file_info = await bot.upload_file(file_content)
                                    # logger.debug(f"文件上传成功: {file_info}")
                                    # media_id = file_info.get('mediaId')
                                    # total_len = file_info.get('totalLen', len(file_content))
                                    # file_extension = os.path.splitext(filename)[1][1:]
                                    # logger.info(f"文件信息: mediaId={media_id}, totalLen={total_len}")
                                    #
                                    # xml = f"""<appmsg appid="" sdkver="0">
                                    #     <title>{filename}</title>
                                    #     <des></des>
                                    #     <action></action>
                                    #     <type>6</type>
                                    #     <showtype>0</showtype>
                                    #     <content></content>
                                    #     <url></url>
                                    #     <appattach>
                                    #         <totallen>{total_len}</totallen>
                                    #         <attachid>{media_id}</attachid>
                                    #         <fileext>{file_extension}</fileext>
                                    #     </appattach>
                                    #     <md5></md5>
                                    # </appmsg>"""
                                    #
                                    # # 发送文件消息
                                    # logger.debug(f"开始发送文件消息: {filename}")
                                    # result = await bot.send_cdn_file_msg(message["FromWxid"], xml)
                                    # logger.debug(f"文件消息发送结果: {result}")

                            except Exception as e:
                                logger.error(f"[文件处理] 处理文件失败: {e}")
                                logger.error(traceback.format_exc())

                            finally:
                                # 无论成功与否，都删除临时文件
                                try:
                                    if os.path.exists(temp_filename):
                                        os.remove(temp_filename)
                                        logger.debug(f"[文件处理] 已删除临时文件: {temp_filename}")
                                except Exception as del_error:
                                    logger.debug(f"[文件处理] 删除临时文件失败: {del_error}")
                        else:
                            error_text = await resp.text()
                            logger.error(f"[文件处理] 下载失败: 状态码={resp.status}, 错误={error_text}")
            except Exception as e:
                logger.error(f"[文件处理] 处理文件链接失败: {e}")
                logger.error(traceback.format_exc())

        # 处理可能的其他格式链接 - 由于我们已经处理了标准格式的链接，这部分可以简化
        other_pattern = r'!?\]\((https?:\/\/[^\s\)]+)\)'
        other_links = re.findall(other_pattern, text)
        if other_links:
            logger.debug(f"[文件处理] 发现其他格式链接: {other_links}")
            # 不再处理这些链接，因为主要的链接已经在前面处理过了

        # 识别普通文件链接 - 简化处理
        file_pattern = r'https?://[^\s<>"]+?/[^\s<>"]+\.(?:pdf|doc|docx|xls|xlsx|txt|zip|rar|7z|tar|gz)'
        file_links = re.findall(file_pattern, text)
        if file_links:
            logger.debug(f"[文件处理] 发现普通文件链接: {file_links}")
            # 不再处理这些链接，因为主要的链接已经在前面处理过了

        pattern = r'\$\$[^$$]+\]\$\$https?:\/\/[^\s$$]+\)'
        text = re.sub(pattern, '', text)

    async def dify_handle_image(self, bot: WechatAPIClient, message: dict, image: Union[str, bytes], model_config=None):
        try:
            image_content = None

            if isinstance(image, str) and image.startswith("http"):
                try:
                    logger.info(f"从URL下载图片: {image}")
                    async with aiohttp.ClientSession() as session:
                        # 正确的方式是在请求时设置代理，而不是在创建会话时
                        proxy = self.http_proxy if self.http_proxy and self.http_proxy.strip() else None
                        async with session.get(image, proxy=proxy) as resp:
                            if resp.status == 200:
                                image_content = await resp.read()
                                logger.info(f"成功从URL下载图片，大小: {len(image_content)} 字节")

                                # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
                                user_id = message["FromWxid"] if message.get("IsGroup", False) else message[
                                    "SenderWxid"]

                                # 上传到 Dify
                                file_info = await self.upload_file_to_dify(
                                    image_content,
                                    f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                                    "image/jpeg",  # 根据实际图片类型调整
                                    user_id,
                                    model_config=model_config  # 传递智能体配置
                                )
                                if file_info:
                                    logger.info(f"图片上传成功，文件ID: {file_info['id']}, 类型: {file_info['type']}")
                            else:
                                logger.error(f"下载图片失败: HTTP {resp.status}")
                                await bot.send_text_message(message["FromWxid"], f"下载图片失败: HTTP {resp.status}")
                                return
                except Exception as e:
                    logger.error(f"下载图片 {image} 失败: {e}")
                    logger.error(traceback.format_exc())
                    await bot.send_text_message(message["FromWxid"], f"下载图片 {image} 失败: {str(e)}")
                    return
            elif isinstance(image, bytes):
                logger.info(f"处理二进制图片数据，大小: {len(image)} 字节")
                image_content = image

                # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
                user_id = message["FromWxid"] if message.get("IsGroup", False) else message["SenderWxid"]

                # 上传到 Dify
                file_info = await self.upload_file_to_dify(
                    image_content,
                    f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                    "image/jpeg",  # 根据实际图片类型调整
                    user_id,  # 使用正确的用户ID
                    model_config=model_config  # 传递模型配置
                )
                if file_info:
                    logger.info(f"图片上传成功，文件ID: {file_info['id']}, 类型: {file_info['type']}")
            else:
                logger.error(f"不支持的图片类型: {type(image)}")
                await bot.send_text_message(message["FromWxid"], f"不支持的图片类型: {type(image)}")
                return

            # 确保我们有图片内容
            if not image_content:
                logger.error("图片内容为空，无法发送")
                await bot.send_text_message(message["FromWxid"], "图片内容为空，无法发送")
                return

            # 验证图片数据
            try:
                # 允许加载截断的图片
                from PIL import ImageFile
                ImageFile.LOAD_TRUNCATED_IMAGES = True

                # 验证图片数据
                img = Image.open(io.BytesIO(image_content))
                logger.info(f"图片验证成功，格式: {img.format}, 大小: {img.size}, 模式: {img.mode}")

                # 检查图片大小，如果太大则调整大小
                width, height = img.size
                max_dimension = 1600  # 最大尺寸限制

                if width > max_dimension or height > max_dimension:
                    # 计算缩放比例
                    ratio = min(max_dimension / width, max_dimension / height)
                    new_width = int(width * ratio)
                    new_height = int(height * ratio)
                    logger.info(f"图片尺寸过大，调整大小从 {width}x{height} 到 {new_width}x{new_height}")
                    img = img.resize((new_width, new_height), Image.LANCZOS)

                    # 转换为RGB模式(去除alpha通道)
                    if img.mode in ('RGBA', 'LA'):
                        logger.debug(f"图片包含alpha通道，转换为RGB模式")
                        background = Image.new('RGB', img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[-1])
                        img = background

                    # 保存为JPEG
                    output = io.BytesIO()
                    img.save(output, format='JPEG', quality=95, optimize=True)
                    output.seek(0)
                    image_content = output.getvalue()
                    logger.info(f"图片处理成功，新大小: {len(image_content)} 字节")
            except Exception as e:
                logger.error(f"图片验证或处理失败: {e}")
                logger.error(traceback.format_exc())
                # 继续使用原始图片数据

            # 直接发送图片数据，不进行base64转换
            logger.info(f"发送图片给用户，大小: {len(image_content)} 字节")
            await bot.send_image_message(message["FromWxid"], image_content)
            logger.info("图片发送成功")
        except Exception as e:
            logger.error(f"处理图片失败: {e}")
            logger.error(traceback.format_exc())
            await bot.send_text_message(message["FromWxid"], f"处理图片失败: {str(e)}")

    @staticmethod
    async def dify_handle_error(bot: WechatAPIClient, message: dict, task_id: str, message_id: str, status: str,
                                code: int, err_message: str):
        output = (XYBOT_PREFIX +
                  DIFY_ERROR_MESSAGE +
                  f"任务 ID：{task_id}\n"
                  f"消息唯一 ID：{message_id}\n"
                  f"HTTP 状态码：{status}\n"
                  f"错误码：{code}\n"
                  f"错误信息：{err_message}")
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_500(bot: WechatAPIClient, message: dict):
        output = XYBOT_PREFIX + "🙅检测到服务异常，请稍后再试。"
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_other_status(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        ai_resp = (XYBOT_PREFIX +
                   f"🙅对不起，出现错误！\n"
                   f"状态码：{resp.status}\n"
                   f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_text_message(message["FromWxid"], ai_resp)

    @staticmethod
    async def handle_exceptions(bot: WechatAPIClient, message: dict, model_config=None):
        output = (XYBOT_PREFIX +
                  "🙅对不起，出现错误！\n"
                  f"错误信息：\n"
                  f"{traceback.format_exc()}")
        await bot.send_text_message(message["FromWxid"], output)

    async def audio_to_text(self, bot: WechatAPIClient, message: dict) -> str:
        if not shutil.which("ffmpeg"):
            logger.error("未找到ffmpeg，请安装并配置到环境变量")
            await bot.send_text_message(message["FromWxid"], "服务器缺少ffmpeg，无法处理语音")
            return ""

        silk_file = "temp_audio.silk"
        mp3_file = "temp_audio.mp3"
        try:
            with open(silk_file, "wb") as f:
                f.write(message["Content"])

            command = f"ffmpeg -y -i {silk_file} -ar 16000 -ac 1 -f mp3 {mp3_file}"
            process = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            if process.returncode != 0:
                logger.error(f"ffmpeg 执行失败: {process.stderr}")
                return ""

            # 使用当前智能体的 base-url 构建音频转文本 URL
            model = self.get_user_model(message["SenderWxid"])
            audio_to_text_url = f"{model.base_url}/audio-to-text"
            logger.debug(f"使用音频转文本 URL: {audio_to_text_url}")

            headers = {"Authorization": f"Bearer {model.api_key}"}
            formdata = aiohttp.FormData()
            with open(mp3_file, "rb") as f:
                mp3_data = f.read()
            formdata.add_field("file", mp3_data, filename="audio.mp3", content_type="audio/mp3")
            # 对于群聊消息，使用群聊ID作为user参数，这样对话会与群聊关联，而不是与个人关联
            user_id = message["FromWxid"] if message.get("IsGroup", False) else message["SenderWxid"]
            formdata.add_field("user", user_id)
            async with aiohttp.ClientSession() as session:
                # 正确的方式是在请求时设置代理，而不是在创建会话时
                proxy = self.http_proxy if self.http_proxy and self.http_proxy.strip() else None
                async with session.post(audio_to_text_url, headers=headers, data=formdata, proxy=proxy) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        text = result.get("text", "")
                        if "failed" in text.lower() or "code" in text.lower():
                            logger.error(f"Dify API 返回错误: {text}")
                        else:
                            logger.info(f"语音转文字结果 (Dify API): {text}")
                            return text
                    else:
                        logger.error(f"audio-to-text 接口调用失败: {resp.status} - {await resp.text()})")

            command = f"ffmpeg -y -i {mp3_file} {silk_file.replace('.silk', '.wav')}"
            process = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            if process.returncode != 0:
                logger.error(f"ffmpeg 转为 WAV 失败: {process.stderr}")
                return ""

            r = sr.Recognizer()
            with sr.AudioFile(silk_file.replace('.silk', '.wav')) as source:
                audio = r.record(source)
            text = r.recognize_google(audio, language="zh-CN")
            logger.info(f"语音转文字结果 (Google): {text}")
            return text
        except Exception as e:
            logger.error(f"语音处理失败: {e}")
            return ""
        finally:
            for temp_file in [silk_file, mp3_file, silk_file.replace('.silk', '.wav')]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)

    async def text_to_voice_message(self, bot: WechatAPIClient, message: dict, text: str = None,
                                    message_id: str = None):
        """
        将文本转换为语音消息并发送

        Args:
            bot: WechatAPIClient实例
            message: 消息字典
            text: 要转换为语音的文本内容（可选，如果提供message_id则可为None）
            message_id: Dify生成的消息ID（可选，优先级高于text）
        """
        try:
            # 使用当前智能体的 base-url 构建文本转音频 URL
            model = self.get_user_model(message["SenderWxid"])
            text_to_audio_url = f"{model.base_url}/text-to-audio"
            logger.debug(f"使用文本转音频 URL: {text_to_audio_url}")

            headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
            # 构建请求数据，支持message_id参数
            data = {"user": message["SenderWxid"]}

            # 优先使用message_id，如果没有则使用text
            if message_id:
                data["message_id"] = message_id
                logger.debug(f"使用message_id: {message_id}进行文本转语音")
            elif text:
                data["text"] = text.replace('@@@CSRS@@@', '')
                logger.debug(
                    f"使用text进行文本转语音: {text[:50]}..." if len(text) > 50 else f"使用text进行文本转语音: {text}")
            else:
                logger.error("文本转语音失败: 未提供text或message_id参数")
                await bot.send_text_message(message["FromWxid"], f"{TEXT_TO_VOICE_FAILED}: 未提供文本内容或消息ID")
                return

            async with aiohttp.ClientSession() as session:
                # 正确的方式是在请求时设置代理，而不是在创建会话时
                proxy = self.http_proxy if self.http_proxy else None
                async with session.post(text_to_audio_url, headers=headers, json=data, proxy=proxy) as resp:
                    if resp.status == 200:
                        audio = await resp.read()
                        await bot.send_voice_message(message["FromWxid"], voice=audio, format="mp3")
                        logger.info(f"文本转语音成功，{'使用message_id' if message_id else '使用text'}")
                    else:
                        error_text = await resp.text()
                        logger.error(f"text-to-audio 接口调用失败: {resp.status} - {error_text}")
                        await bot.send_text_message(message["FromWxid"],
                                                    f"{TEXT_TO_VOICE_FAILED}: 状态码 {resp.status}")
        except Exception as e:
            logger.error(f"text-to-audio 接口调用异常: {e}")
            logger.error(traceback.format_exc())
            await bot.send_text_message(message["FromWxid"], f"{TEXT_TO_VOICE_FAILED}: {str(e)}")

    @on_image_message(priority=25)
    async def handle_image(self, bot: WechatAPIClient, message: dict):
        """处理图片消息"""
        if not self.enable:
            return

        logger.info('[handle_image]>>>')

        try:
            # 获取图片消息的关键信息
            msg_id = message.get("MsgId")
            from_wxid = message.get("FromWxid")
            sender_wxid = message.get("SenderWxid")

            logger.info(f"收到图片消息: MsgId={msg_id}, FromWxid={from_wxid}, SenderWxid={sender_wxid}")

            image_info = message.get("ImageInfo")
            if image_info:
                aeskey = image_info.get("aeskey")
                cdnmidimgurl = image_info.get("cdnmidimgurl")
                try:
                    message["Content"] = await bot.download_image(aeskey, cdnmidimgurl)
                    logger.info("download_image下载图片成功")
                except Exception as e2:
                    logger.error(f"download_image下载图片失败: {e2}")

            # 直接从消息中获取图片内容
            image_content = None
            xml_content = message.get("Content")

            # 如果是二进制数据，直接使用
            if isinstance(xml_content, bytes):
                logger.debug("图片内容是二进制数据，尝试直接处理")
                try:
                    # 验证是否为有效的图片数据
                    Image.open(io.BytesIO(xml_content))
                    image_content = xml_content
                    logger.info(f"二进制图片数据验证成功，大小: {len(xml_content)} 字节")
                except Exception as e:
                    logger.error(f"二进制图片数据无效: {e}")

            # 如果是字符串，尝试解析XML或处理base64图片数据
            elif isinstance(xml_content, str):
                # 检查是否是base64编码的图片数据
                if xml_content.startswith('/9j/') or xml_content.startswith('iVBOR'):
                    logger.debug("检测到base64编码的图片数据，直接解码")
                    try:
                        import base64
                        # 处理可能的填充字符
                        xml_content = xml_content.strip()
                        # 处理可能的换行符
                        xml_content = xml_content.replace('\n', '').replace('\r', '')

                        try:
                            # 先尝试直接解码
                            image_data = base64.b64decode(xml_content)
                        except Exception as base64_error:
                            logger.warning(f"直接解码失败: {base64_error}")
                            # 尝试修复可能的base64编码问题
                            try:
                                # 添加可能缺失的填充
                                padding_needed = len(xml_content) % 4
                                if padding_needed:
                                    xml_content += '=' * (4 - padding_needed)
                                image_data = base64.b64decode(xml_content)
                                logger.debug("添加填充后成功解码base64数据")
                            except Exception as padding_error:
                                logger.error(f"添加填充后仍然无法解码: {padding_error}")
                                # 尝试使用更宽松的解码方式
                                try:
                                    image_data = base64.b64decode(xml_content + '==', validate=False)
                                    logger.debug("使用宽松模式成功解码base64数据")
                                except Exception as e:
                                    logger.error(f"所有base64解码方法均失败: {e}")
                                    return

                        # 验证图片数据
                        try:
                            # 允许加载截断的图片
                            from PIL import ImageFile
                            ImageFile.LOAD_TRUNCATED_IMAGES = True

                            Image.open(io.BytesIO(image_data))
                            image_content = image_data
                            logger.info(f"base64图片数据解码成功，大小: {len(image_data)} 字节")
                        except Exception as img_error:
                            logger.error(f"base64图片数据无效: {img_error}")
                    except Exception as base64_error:
                        logger.error(f"base64解码失败: {base64_error}")
                        logger.debug(f"base64数据前100字符: {xml_content[:100]}")
                else:
                    # 尝试解析XML
                    logger.debug("图片内容是字符串，尝试解析XML")
                    try:
                        # 尝试解析XML获取图片信息
                        root = ET.fromstring(xml_content)
                        img_element = root.find('img')

                        if img_element is not None:
                            # 提取图片元数据
                            md5 = img_element.get('md5')
                            aeskey = img_element.get('aeskey')
                            length = img_element.get('length')
                            # 获取图片URL，但不使用这些变量，避免IDE警告
                            # cdnmidimgurl = img_element.get('cdnmidimgurl')
                            # cdnthumburl = img_element.get('cdnthumburl')

                            logger.info(f"从XML解析到图片信息: md5={md5}, aeskey={aeskey}, length={length}")

                            # 尝试使用PAD API下载图片
                            try:
                                # 从 XML 中提取图片大小
                                img_length = int(length) if length and length.isdigit() else 0

                                # 使用消息 ID 下载图片 - 实现分段下载
                                logger.debug(f"尝试使用消息 ID {msg_id} 下载图片，图片大小: {img_length}")

                                # 创建一个字节数组来存储完整的图片数据
                                full_image_data = bytearray()

                                # 分段下载大图片
                                chunk_size = 64 * 1024  # 64KB
                                chunks = (img_length + chunk_size - 1) // chunk_size  # 向上取整

                                logger.info(f"开始分段下载图片，总大小: {img_length} 字节，分 {chunks} 段下载")

                                download_success = True
                                for i in range(chunks):
                                    try:
                                        # 下载当前段
                                        chunk_data = await bot.get_msg_image(msg_id, from_wxid, img_length,
                                                                             start_pos=i * chunk_size)
                                        if chunk_data and len(chunk_data) > 0:
                                            full_image_data.extend(chunk_data)
                                            logger.debug(f"第 {i + 1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                        else:
                                            logger.error(f"第 {i + 1}/{chunks} 段下载失败，数据为空")
                                            download_success = False
                                            break
                                    except Exception as e:
                                        logger.error(f"下载第 {i + 1}/{chunks} 段时出错: {e}")
                                        download_success = False
                                        break

                                if download_success and len(full_image_data) > 0:
                                    # 验证图片数据
                                    try:
                                        image_data = bytes(full_image_data)
                                        Image.open(io.BytesIO(image_data))
                                        image_content = image_data
                                        logger.info(f"使用消息 ID下载图片成功，总大小: {len(image_data)} 字节")
                                    except Exception as img_error:
                                        logger.error(f"下载的图片数据无效: {img_error}")
                                else:
                                    logger.error(f"图片分段下载失败，已下载: {len(full_image_data)}/{img_length} 字节")
                            except Exception as download_error:
                                logger.error(f"使用消息 ID下载图片失败: {download_error}")
                                logger.error(traceback.format_exc())
                    except Exception as xml_error:
                        logger.error(f"XML解析失败: {xml_error}")
                        logger.debug(f"XML内容前100字符: {xml_content[:100]}")
            else:
                logger.error(f"图片消息内容格式未知: {type(xml_content)}")

            # 如果成功获取图片内容，则缓存
            if image_content:
                # 缓存图片到发送者和收件人的ID
                self.image_cache[sender_wxid] = {
                    "content": image_content,
                    "timestamp": time.time()
                }
                logger.info(f"已缓存用户 {sender_wxid} 的图片")

                # 如果是私聊，也缓存到聊天对象的ID
                if from_wxid != sender_wxid:
                    self.image_cache[from_wxid] = {
                        "content": image_content,
                        "timestamp": time.time()
                    }
                    logger.info(f"已缓存聊天对象 {from_wxid} 的图片")
            else:
                logger.warning(f"未能获取图片内容，无法缓存")
            logger.info('<<<[handle_image]')

        except Exception as e:
            logger.error(f"处理图片消息失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")

    async def get_cached_image(self, user_wxid: str) -> Optional[bytes]:
        """获取用户最近的图片"""
        logger.debug(f"尝试获取用户 {user_wxid} 的缓存图片")
        if user_wxid in self.image_cache:
            cache_data = self.image_cache[user_wxid]
            current_time = time.time()
            cache_age = current_time - cache_data["timestamp"]
            logger.info(f"找到缓存图片，年龄: {cache_age:.2f}秒, 超时时间: {self.image_cache_timeout}秒")

            if cache_age <= self.image_cache_timeout:
                try:
                    # 确保我们有有效的二进制数据
                    image_content = cache_data["content"]
                    if not isinstance(image_content, bytes):
                        logger.error("缓存的图片内容不是二进制格式")
                        del self.image_cache[user_wxid]
                        return None

                    # 尝试验证图片数据
                    try:
                        img = Image.open(io.BytesIO(image_content))
                        logger.debug(f"缓存图片验证成功，格式: {img.format}, 大小: {len(image_content)} 字节")
                    except Exception as e:
                        logger.error(f"缓存的图片数据无效: {e}")
                        del self.image_cache[user_wxid]
                        return None

                    # 不再删除缓存，而是在上传成功后删除
                    # 更新时间戳，避免过早超时
                    self.image_cache[user_wxid]["timestamp"] = current_time
                    logger.info(f"成功获取用户 {user_wxid} 的缓存图片")
                    return image_content
                except Exception as e:
                    logger.error(f"处理缓存图片失败: {e}")
                    del self.image_cache[user_wxid]
                    return None
            else:
                # 超时清除
                logger.info(f"缓存图片超时，已清除")
                del self.image_cache[user_wxid]
        else:
            logger.debug(f"未找到用户 {user_wxid} 的缓存图片")
        return None

    def _get_image_extension(self, image_data):
        """根据图片数据判断文件扩展名"""
        try:
            from PIL import Image
            import io

            # 尝试打开图片并获取格式
            img = Image.open(io.BytesIO(image_data))
            fmt = img.format.lower() if img.format else "jpg"
            return fmt
        except Exception as e:
            logger.error(f"获取图片格式失败: {e}")
            return "jpg"  # 默认返回jpg

    async def find_image_by_md5(self, md5: str) -> Optional[bytes]:
        """根据MD5查找图片文件"""
        if not md5:
            logger.warning("MD5为空，无法查找图片")
            return None

        # 检查files目录是否存在
        files_dir = os.path.join(os.getcwd(), "files")
        if not os.path.exists(files_dir):
            logger.warning(f"files目录不存在: {files_dir}")
            return None

        # 尝试查找不同扩展名的图片文件
        for ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
            file_path = os.path.join(files_dir, f"{md5}.{ext}")
            if os.path.exists(file_path):
                try:
                    # 读取图片文件
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    logger.info(f"根据MD5找到图片文件: {file_path}, 大小: {len(image_data)} 字节")
                    return image_data
                except Exception as e:
                    logger.error(f"读取图片文件失败: {e}")

        logger.warning(f"未找到MD5为 {md5} 的图片文件")
        return None

    async def get_cached_file(self, user_wxid: str) -> Optional[tuple[bytes, str, str]]:
        """获取用户最近的文件，返回 (文件内容, 文件名, MIME类型)"""
        logger.debug(f"尝试获取用户 {user_wxid} 的缓存文件")
        if user_wxid in self.file_cache:
            cache_data = self.file_cache[user_wxid]
            current_time = time.time()
            cache_age = current_time - cache_data["timestamp"]
            logger.debug(f"找到缓存文件，年龄: {cache_age:.2f}秒, 超时时间: {self.file_cache_timeout}秒")

            if cache_age <= self.file_cache_timeout:
                try:
                    # 确保我们有有效的二进制数据
                    file_content = cache_data["content"]
                    file_name = cache_data["name"]
                    mime_type = cache_data["mime_type"]

                    # 处理不同类型的文件内容
                    if isinstance(file_content, bytearray):
                        # 将 bytearray 转换为 bytes
                        file_content = bytes(file_content)
                        logger.info(f"将 bytearray 转换为 bytes，大小: {len(file_content)} 字节")
                    elif isinstance(file_content, str):
                        # 尝试将字符串解析为 base64
                        try:
                            file_content = base64.b64decode(file_content)
                            logger.info(f"将 base64 字符串转换为 bytes，大小: {len(file_content)} 字节")
                        except Exception as e:
                            logger.error(f"Base64 解码失败: {e}")
                            file_content = file_content.encode('utf-8')
                            logger.info(f"将普通字符串转换为 bytes，大小: {len(file_content)} 字节")
                    elif not isinstance(file_content, bytes):
                        logger.error(f"缓存的文件内容不是支持的格式: {type(file_content)}")
                        del self.file_cache[user_wxid]
                        return None

                    # 更新缓存中的文件内容
                    self.file_cache[user_wxid]["content"] = file_content

                    # 更新时间戳，避免过早超时
                    self.file_cache[user_wxid]["timestamp"] = current_time
                    logger.info(f"成功获取用户 {user_wxid} 的缓存文件: {file_name}, 大小: {len(file_content)} 字节")
                    return (file_content, file_name, mime_type)
                except Exception as e:
                    logger.error(f"处理缓存文件失败: {e}")
                    del self.file_cache[user_wxid]
                    return None
            else:
                # 超时清除
                logger.debug(f"缓存文件超时，已清除")
                del self.file_cache[user_wxid]
        else:
            logger.debug(f"未找到用户 {user_wxid} 的缓存文件")
        return None

    def cache_file(self, user_wxid: str, file_content: bytes, file_name: str, mime_type: str) -> None:
        """缓存用户文件"""
        self.file_cache[user_wxid] = {
            "content": file_content,
            "name": file_name,
            "mime_type": mime_type,
            "timestamp": time.time()
        }
        logger.info(f"已缓存用户 {user_wxid} 的文件: {file_name}, 大小: {len(file_content)} 字节")

    async def save_file_by_md5(self, md5filename: str, file_data: bytes):
        try:
            # 确保files目录存在
            files_dir = os.path.join(os.getcwd(), "files")
            os.makedirs(files_dir, exist_ok=True)

            # 根据MD5值生成文件名
            file_path = os.path.join(files_dir, md5filename)

            # 保存文件
            with open(file_path, "wb") as f:
                f.write(file_data)
            logger.info(f"文件已保存到: {file_path}")
        except Exception as save_error:
            logger.error(f"保存文件失败: {save_error}")

    async def find_file_by_md5(self, filename_md5: str) -> Optional[bytes]:
        """根据MD5查找图片文件"""
        if not filename_md5:
            logger.warning("MD5 file为空，无法查找文件")
            return None

        # 检查files目录是否存在
        files_dir = os.path.join(os.getcwd(), "files")
        if not os.path.exists(files_dir):
            logger.warning(f"files目录不存在: {files_dir}")
            return None

        # 尝试查找不同扩展名的图片文件
        file_path = os.path.join(files_dir, f"{filename_md5}")
        if os.path.exists(file_path):
            try:
                # 读取文件
                with open(file_path, "rb") as f:
                    file_data = f.read()
                logger.info(f"根据MD5 filename找到文件: {file_path}, 大小: {len(file_data)} 字节")
                return file_data
            except Exception as e:
                logger.error(f"读取文件失败: {e}")

        logger.warning(f"未找到MD5为 {filename_md5} 的文件")
        return None

    async def download_and_send_file(self, bot: WechatAPIClient, message: dict, url: str):
        """下载并发送文件"""
        try:
            # 从URL中获取文件名
            parsed_url = urllib.parse.urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                filename = f"downloaded_file_{int(time.time())}"

            logger.info(f"开始下载文件: {url}")

            # 使用改进后的download_file方法
            content = await self.download_file(url)
            if not content:
                await bot.send_text_message(message["FromWxid"], f"下载文件失败: {url}")
                return

            # 检测文件类型
            kind = filetype.guess(content)
            if kind is None:
                # 如果无法检测文件类型,尝试从URL获取
                ext = os.path.splitext(filename)[1].lower()
                if not ext:
                    # 如果没有扩展名，使用默认扩展名
                    ext = ".txt"
                    logger.warning(f"无法识别文件类型，使用默认扩展名: {ext}")
            else:
                ext = f".{kind.extension}"
                logger.info(f"检测到文件类型: {kind.mime}, 扩展名: {ext}")

            # 确保文件名有扩展名
            if not os.path.splitext(filename)[1]:
                filename = f"{filename}{ext}"

            # 根据文件类型发送不同类型的消息
            if ext.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg']:
                await bot.send_image_message(message["FromWxid"], content)
                logger.info(f"发送图片消息成功，文件名: {filename}, 大小: {len(content)} 字节")
            elif ext.lower() in ['.mp3', '.wav', '.ogg', '.m4a']:
                await bot.send_voice_message(message["FromWxid"], voice=content, format=ext[1:])
                logger.info(f"发送语音消息成功，文件名: {filename}, 大小: {len(content)} 字节")
            elif ext.lower() in ['.mp4', '.avi', '.mov', '.mkv', '.flv']:
                await bot.send_video_message(message["FromWxid"], video=content, image="None")
                logger.info(f"发送视频消息成功，文件名: {filename}, 大小: {len(content)} 字节")
            else:
                # 其他类型文件，发送文件信息
                await bot.send_text_message(message["FromWxid"],
                                            f"文件名: {filename}\n类型: {ext[1:]}\n大小: {len(content) / 1024:.2f} KB")
                logger.info(f"发送文件信息成功，文件名: {filename}, 大小: {len(content)} 字节")

            # 缓存文件，便于后续使用
            mime_type = kind.mime if kind else f"application/{ext[1:]}"
            self.cache_file(message["SenderWxid"], content, filename, mime_type)
            logger.info(f"文件已缓存，用户: {message['SenderWxid']}, 文件名: {filename}")

            # 如果是私聊，也缓存到聊天对象的ID
            if message["FromWxid"] != message.get("SenderWxid", message["FromWxid"]):
                self.cache_file(message["FromWxid"], content, filename, mime_type)
                logger.info(f"文件已缓存到聊天对象: {message['FromWxid']}, 文件名: {filename}")

        except Exception as e:
            logger.error(f"下载或发送文件失败: {e}")
            logger.error(traceback.format_exc())

    # 添加一个专门处理引用消息的方法
    @on_xml_message(priority=99)  # 使用最高优先级确保最先处理
    async def handle_xml_quote(self, bot: WechatAPIClient, message: dict):
        """专门处理XML格式的引用消息"""
        if not self.enable:
            return True

        logger.info('[handle_xml_file]')
        # 检查消息是否已经处理过
        if self.is_message_processed(message):
            logger.info(f"XML消息 {message.get('MsgId') or message.get('NewMsgId')} 已经处理过，跳过")
            return True  # 消息已处理，允许其他插件处理

        # 检查是否是引用消息
        if message.get("Quote"):
            logger.info("检测到XML引用消息，直接处理")

            # 提取引用消息的详细信息
            quote_info = message.get("Quote", {})
            quoted_msg_id = quote_info.get("MsgId", "") or quote_info.get("NewMsgId", "")
            quoted_wxid = quote_info.get("FromWxid", "")
            quoted_content = quote_info.get("Content", "")
            quoted_nickname = quote_info.get("Nickname", "")
            quoted_msg_type = quote_info.get("MsgType")

            logger.info(
                f"引用消息详情: MsgId={quoted_msg_id}, 发送者={quoted_nickname}, 类型={quoted_msg_type}, 内容={quoted_content[:30]}...")

            # 检查引用的消息是否包含图片
            image_md5 = None
            if quoted_msg_type == 3:  # 图片消息
                try:
                    # 尝试从引用的图片消息中提取MD5
                    if "<?xml" in quoted_content and "<img" in quoted_content:
                        root = ET.fromstring(quoted_content)
                        img_element = root.find('img')
                        if img_element is not None:
                            image_md5 = img_element.get('md5')
                            logger.info(f"从XML引用的图片消息中提取到MD5: {image_md5}")
                except Exception as e:
                    logger.error(f"解析XML引用图片消息XML失败: {e}")

            # 获取消息内容
            content = message.get("Content", "")
            logger.info(f"XML引用消息内容: {content[:50]}...")

            # 直接检查消息内容中是否包含@机器人
            is_at_bot = False
            for robot_name in self.robot_names:
                if f"@{robot_name}" in content:
                    logger.info(f"XML引用消息内容中直接发现@{robot_name}")
                    is_at_bot = True
                    break

                # 检查格式: "@小球子"（消息开头）
                if content.startswith(f'@{robot_name}'):
                    logger.info(f"XML引用消息内容以@{robot_name}开头")
                    is_at_bot = True
                    break

            # 如果直接检查没有发现@，使用增强的is_at_message方法
            if not is_at_bot:
                is_at_bot = self.is_at_message(message)

            if is_at_bot:
                logger.info("Dify: XML引用消息中@了机器人，处理该消息")

                # 如果有图片MD5，添加到消息中
                if image_md5:
                    message["ImageMD5"] = image_md5
                    logger.info(f"将图片MD5 {image_md5} 添加到消息中")

                # 标记消息为已处理
                self.mark_message_processed(message)

                # 处理群聊和私聊的情况
                if message["IsGroup"]:
                    is_at, query = await self.at_message_process(bot, message)
                    # 如果没有内容，则使用引用的内容
                    if not query:
                        if image_md5 or quoted_msg_type == 3:
                            query = f"请分析这张图片"
                        else:
                            query = f"请回复这条消息: '{quoted_content}'"
                    else:
                        query = f"{query} (引用消息: '{quoted_content}')"
                    ret = await self.group_message_process(bot, message, query, image_md5)
                else:
                    # 如果没有内容，则使用引用的内容
                    if not content:
                        if image_md5 or quoted_msg_type == 3:
                            query = f"请分析这张图片"
                        else:
                            query = f"请回复这条消息: '{quoted_content}'"
                    else:
                        query = f"{content} (引用消息: '{quoted_content}')"
                    ret = await self.private_message_process(bot, message, query, image_md5)
                logger.info(f'<<<[handle_quote] return:{ret}')
                return ret
            else:
                logger.info("Dify: XML引用消息中没有@机器人，忽略该消息")
                return True

        # 不是引用消息，交给下一个处理器处理
        return True

    async def download_file_process(self, bot: WechatAPIClient, app_id, attach_id, total_len):
        # 使用 /Tools/DownloadFile API 下载文件
        logger.info("[download_file_process]")

        logger.info("开始下载文件...")
        # 分段下载大文件
        # 每次下载 64KB
        chunk_size = 64 * 1024  # 64KB

        # 创建一个字节数组来存储完整的文件数据
        file_data = bytearray()

        # 计算需要下载的分段数量
        chunks = (total_len + chunk_size - 1) // chunk_size  # 向上取整

        logger.info(f"开始分段下载文件，总大小: {total_len} 字节，分 {chunks} 段下载")

        # 尝试两个不同的API端点
        urls = [
            f'http://127.0.0.1:9011/api/Tools/DownloadFile',
            f'http://127.0.0.1:9011/VXAPI/Tools/DownloadFile'
        ]

        download_success = False

        for url in urls:
            if download_success:
                break

            file_data.clear()  # 清空之前的数据
            logger.info(f"尝试使用 {url} 下载文件")

            # 分段下载
            for i in range(chunks):
                start_pos = i * chunk_size
                # 最后一段可能不足 chunk_size
                current_chunk_size = min(chunk_size, total_len - start_pos)

                logger.info(
                    f"下载第 {i + 1}/{chunks} 段，起始位置: {start_pos}，大小: {current_chunk_size} 字节")

                async with aiohttp.ClientSession() as session:
                    # 设置较长的超时时间
                    timeout = aiohttp.ClientTimeout(total=60)  # 1分钟

                    # 构造请求参数
                    json_param = {
                        "AppID": app_id,
                        "AttachId": attach_id,
                        "DataLen": total_len,
                        "Section": {
                            "DataLen": current_chunk_size,
                            "StartPos": start_pos
                        },
                        "UserName": "",  # 可选参数
                        "Wxid": bot.wxid
                    }

                    logger.info(
                        f"调用下载文件API: AttachId={attach_id}, 起始位置: {start_pos}, 大小: {current_chunk_size}")
                    response = await session.post(
                        url,
                        json=json_param,
                        timeout=timeout
                    )

                    # 处理响应
                    try:
                        json_resp = await response.json()

                        if json_resp.get("Success"):
                            data = json_resp.get("Data")

                            # 尝试从不同的响应格式中获取文件数据
                            chunk_data = None
                            if isinstance(data, dict):
                                if "buffer" in data:
                                    chunk_data = base64.b64decode(data["buffer"])
                                elif "data" in data and isinstance(data["data"], dict) and "buffer" in data[
                                    "data"]:
                                    chunk_data = base64.b64decode(data["data"]["buffer"])
                                else:
                                    try:
                                        chunk_data = base64.b64decode(str(data))
                                    except:
                                        logger.error(f"无法解析文件数据: {data}")
                            elif isinstance(data, str):
                                try:
                                    chunk_data = base64.b64decode(data)
                                except:
                                    logger.error(f"无法解析文件数据字符串")

                            if chunk_data:
                                # 将分段数据添加到完整文件中
                                file_data.extend(chunk_data)
                                logger.info(
                                    f"第 {i + 1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                            else:
                                logger.warning(f"第 {i + 1}/{chunks} 段数据为空")
                                break
                        else:
                            error_msg = json_resp.get("Message", "Unknown error")
                            logger.error(f"第 {i + 1}/{chunks} 段下载失败: {error_msg}")
                            break
                    except Exception as e:
                        logger.error(f"解析第 {i + 1}/{chunks} 段响应失败: {e}")
                        break

            # 检查文件是否下载完整
            if len(file_data) > 0:
                logger.info(f"文件下载成功: AttachId={attach_id}, 实际大小: {len(file_data)} 字节")
                download_success = True
                break
            else:
                logger.warning("文件数据为空，尝试下一个API端点")

        return download_success, file_data

    @on_xml_message(priority=98)  # 使用高优先级确保先处理
    async def handle_xml_file(self, bot: WechatAPIClient, message: dict):
        """处理XML格式的文件消息"""
        if not self.enable:
            return True

        logger.info('[handle_xml_file]>>>')
        try:
            # 检查消息内容是否是XML格式
            content = message.get("Content", "")
            if not content or not isinstance(content, str) or not content.strip().startswith("<"):
                logger.warning(f"消息内容不是XML格式: {content[:100]}")
                return True

            # 解析XML内容
            root = ET.fromstring(message["Content"])
            appmsg = root.find("appmsg")
            if appmsg is None:
                return True

            type_element = appmsg.find("type")
            if type_element is None:
                return True

            type_value = int(type_element.text)
            logger.info(f"XML消息类型: {type_value}")

            # 检测是否是文件消息（类型6）
            if type_value == 6:
                logger.info("检测到文件消息")

                # 提取文件信息
                title = appmsg.find("title").text
                appattach = appmsg.find("appattach")
                md5 = appmsg.find("md5").text
                attach_id = appattach.find("attachid").text
                file_extend = appattach.find("fileext").text
                total_len = int(appattach.find("totallen").text)
                app_id = appmsg.get("appid", "")

                logger.info(f"文件名: {title}")
                logger.info(f"文件扩展名: {file_extend}")
                logger.info(f"附件ID: {attach_id}")
                logger.info(f"文件大小: {total_len}")

                # 不发送下载提示
                logger.info(f"开始下载文件: {title}, 大小: {total_len} 字节")
                download_success, file_data = await self.download_file_process(bot, app_id, attach_id, total_len)

                # 如果文件下载成功
                if download_success:
                    # 确定文件类型
                    mime_type = mimetypes.guess_type(f"{title}.{file_extend}")[0] or "application/octet-stream"

                    # 确保文件数据是二进制格式
                    if isinstance(file_data, str):
                        try:
                            binary_file_data = base64.b64decode(file_data)
                            logger.info(f"将base64字符串转换为二进制数据，大小: {len(binary_file_data)} 字节")
                        except Exception as e:
                            logger.error(f"Base64解码失败: {e}")
                            binary_file_data = file_data.encode('utf-8')
                    elif isinstance(file_data, bytearray):
                        binary_file_data = bytes(file_data)
                        logger.info(f"将bytearray转换为二进制数据，大小: {len(binary_file_data)} 字节")
                    else:
                        binary_file_data = file_data

                    # 处理文件名，避免重复的扩展名
                    if title.lower().endswith(f".{file_extend.lower()}"):
                        file_name = title  # 如果标题已经包含扩展名，直接使用
                    else:
                        file_name = f"{title}.{file_extend}"  # 否则添加扩展名
                    logger.info(f"处理后的文件名: {file_name}")

                    md5_filename = f"{md5}.{file_extend.lower()}"
                    await self.save_file_by_md5(md5_filename, binary_file_data)

                    # 缓存文件
                    from_wxid = message["FromWxid"]
                    sender_wxid = message.get("SenderWxid", from_wxid)
                    self.cache_file(sender_wxid, binary_file_data, file_name, mime_type)

                    # 如果是私聊，也缓存到聊天对象的ID
                    if from_wxid != sender_wxid:
                        self.cache_file(from_wxid, binary_file_data, file_name, mime_type)

                    logger.info(f"文件下载成功并已缓存: {file_name}, 大小: {len(binary_file_data) / 1024:.2f} KB")
                else:
                    logger.warning("所有API端点尝试失败")

                logger.info('<<<[handle_xml_file]')
        except Exception as e:
            logger.error(f"处理XML消息时发生错误: {str(e)}")
            logger.error(traceback.format_exc())

        return True  # 允许后续插件处理

    @on_file_message(priority=20)
    async def handle_file(self, bot: WechatAPIClient, message: dict):
        """处理文件消息"""
        if not self.enable:
            return

        logger.info('[handle_file]>>>')
        try:
            # 获取文件消息的关键信息
            msg_id = message.get("MsgId")
            from_wxid = message.get("FromWxid")
            sender_wxid = message.get("SenderWxid")
            file_content = message.get("Content")

            logger.info(f"收到文件消息: MsgId={msg_id}, FromWxid={from_wxid}, SenderWxid={sender_wxid}")

            # 如果Content是二进制数据，直接使用
            if isinstance(file_content, bytes) and len(file_content) > 0:
                logger.info(f"文件内容是二进制数据，大小: {len(file_content)} 字节")

                # 获取文件名和类型
                file_name = message.get("FileName", f"file_{int(time.time())}")

                # 检测文件类型
                mime_type = "application/octet-stream"  # 默认类型
                try:
                    kind = filetype.guess(file_content)
                    if kind is not None:
                        mime_type = kind.mime
                        # 如果文件名没有后缀，添加正确的后缀
                        if not os.path.splitext(file_name)[1]:
                            file_name = f"{file_name}.{kind.extension}"
                except Exception as e:
                    logger.error(f"检测文件类型失败: {e}")

            # 如果Content是XML字符串，解析并下载文件
            elif isinstance(file_content, str) and ("<appmsg" in file_content or "<msg>" in file_content):
                logger.info("文件内容是XML格式，尝试解析并下载文件")
                try:
                    # 解析XML
                    import xml.etree.ElementTree as ET
                    import mimetypes
                    import base64

                    # 处理可能的XML格式差异
                    if "<msg>" in file_content and "<appmsg" in file_content:
                        # 提取<appmsg>部分
                        start = file_content.find("<appmsg")
                        end = file_content.find("</appmsg>") + 9
                        appmsg_xml = file_content[start:end]
                        root = ET.fromstring(f"<root>{appmsg_xml}</root>")
                        appmsg = root.find('appmsg')
                    else:
                        root = ET.fromstring(file_content)
                        appmsg = root.find('.//appmsg')

                    if appmsg is not None:
                        # 获取文件名
                        title = appmsg.find('.//title')
                        file_name = title.text if title is not None and title.text else f"file_{int(time.time())}"

                        # 获取文件类型
                        fileext = appmsg.find('.//fileext')
                        if fileext is not None and fileext.text:
                            ext = fileext.text.lower()
                            if not file_name.lower().endswith(f".{ext}"):
                                file_name = f"{file_name}.{ext}"
                            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
                        else:
                            mime_type = "application/octet-stream"

                        # 获取下载所需信息
                        appattach = appmsg.find('.//appattach')
                        if appattach is not None:
                            attachid = appattach.find('.//attachid')
                            aeskey = appattach.find('.//aeskey')
                            totallen = appattach.find('.//totallen')

                            # 获取文件大小
                            total_len = int(
                                totallen.text) if totallen is not None and totallen.text and totallen.text.isdigit() else 0

                            # 获取附件ID和其他下载所需信息
                            attach_id = None
                            cdn_url = None
                            aes_key = None

                            if attachid is not None and attachid.text:
                                attach_id = attachid.text.strip()
                                logger.info(f"找到附件ID: {attach_id}")

                            # 获取CDN URL和AES密钥（用于方法3）
                            cdnattachurl = appattach.find('.//cdnattachurl')
                            if cdnattachurl is not None and cdnattachurl.text:
                                cdn_url = cdnattachurl.text.strip()
                                logger.info(f"找到CDN URL: {cdn_url}")

                            if aeskey is not None and aeskey.text:
                                aes_key = aeskey.text.strip()
                                logger.info(f"找到AES密钥: {aes_key}")

                                # 开始下载文件
                                logger.info(f"开始下载文件: {file_name}, 大小: {total_len} 字节")

                                # 尝试不同的下载方法
                                try:
                                    file_data = None

                                    # 方法1: 如果有附件ID，使用download_attach方法
                                    if attach_id:
                                        logger.debug(f"方法1: 尝试使用download_attach方法下载文件，附件ID: {attach_id}")
                                        file_data = await bot.download_attach(attach_id)

                                    # 方法3: 如果有CDN URL和AES密钥，使用download_image方法
                                    if not file_data and cdn_url and aes_key:
                                        logger.debug(f"方法3: 尝试使用download_image方法下载文件，CDN URL: {cdn_url}")
                                        try:
                                            image_data = await bot.download_image(aes_key, cdn_url)
                                            if image_data:
                                                if isinstance(image_data, str):
                                                    try:
                                                        file_data = base64.b64decode(image_data)
                                                        logger.info(
                                                            f"使用download_image成功下载文件，大小: {len(file_data)} 字节")
                                                    except Exception as e:
                                                        logger.error(f"Base64解码失败: {e}")
                                        except Exception as e:
                                            logger.error(f"download_image方法失败: {e}")
                                    if not file_data:
                                        # 方法2: 使用Tools/DownloadFile API分段下载文件
                                        logger.debug(f"尝试使用Tools/DownloadFile API分段下载文件")

                                        # 分段下载大文件
                                        chunk_size = 64 * 1024  # 64KB
                                        chunks = (total_len + chunk_size - 1) // chunk_size  # 向上取整
                                        file_data_bytes = bytearray()
                                        download_success = False

                                        # 尝试两个不同的API端点
                                        urls = [
                                            f'http://{bot.ip}:{bot.port}/api/Tools/DownloadFile',
                                            f'http://{bot.ip}:{bot.port}/VXAPI/Tools/DownloadFile'
                                        ]

                                        # 尝试每个API端点
                                        for url in urls:
                                            if download_success:
                                                break

                                            logger.info(
                                                f"尝试使用 {url} 分段下载文件，总大小: {total_len} 字节，分 {chunks} 段下载")
                                            file_data_bytes.clear()  # 清空之前的数据

                                            try:
                                                async with aiohttp.ClientSession() as session:
                                                    # 分段下载
                                                    for i in range(chunks):
                                                        start_pos = i * chunk_size
                                                        # 最后一段可能不足 chunk_size
                                                        current_chunk_size = min(chunk_size, total_len - start_pos)

                                                        logger.debug(
                                                            f"下载第 {i + 1}/{chunks} 段，起始位置: {start_pos}，大小: {current_chunk_size} 字节")

                                                        # 构造请求参数
                                                        json_param = {
                                                            "AppID": "",  # 可选参数
                                                            "AttachId": attach_id,
                                                            "DataLen": total_len,
                                                            "Section": {
                                                                "DataLen": current_chunk_size,
                                                                "StartPos": start_pos
                                                            },
                                                            "UserName": "",  # 可选参数
                                                            "Wxid": bot.wxid
                                                        }

                                                        # 设置较长的超时时间
                                                        timeout = aiohttp.ClientTimeout(total=60)  # 1分钟

                                                        # 发送请求
                                                        try:
                                                            async with session.post(url, json=json_param,
                                                                                    timeout=timeout) as resp:
                                                                if resp.status == 200:
                                                                    resp_json = await resp.json()
                                                                    if resp_json.get("Success"):
                                                                        data = resp_json.get("Data")
                                                                        if isinstance(data, str):
                                                                            try:
                                                                                chunk_data = base64.b64decode(data)
                                                                                file_data_bytes.extend(chunk_data)
                                                                                logger.debug(
                                                                                    f"第 {i + 1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                                                            except Exception as e:
                                                                                logger.error(f"Base64解码失败: {e}")
                                                                                break
                                                                        elif isinstance(data,
                                                                                        dict) and "buffer" in data:
                                                                            try:
                                                                                chunk_data = base64.b64decode(
                                                                                    data["buffer"])
                                                                                file_data_bytes.extend(chunk_data)
                                                                                logger.debug(
                                                                                    f"第 {i + 1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                                                            except Exception as e:
                                                                                logger.error(
                                                                                    f"Buffer Base64解码失败: {e}")
                                                                                break
                                                                        else:
                                                                            logger.warning(f"无法解析响应数据: {data}")
                                                                            break
                                                                    else:
                                                                        logger.warning(f"API返回错误: {resp_json}")
                                                                        break
                                                                else:
                                                                    logger.warning(f"API请求失败: {resp.status}")
                                                                    break
                                                        except Exception as e:
                                                            logger.error(f"下载第 {i + 1}/{chunks} 段时出错: {e}")
                                                            break

                                                    # 检查文件是否下载完整
                                                    if len(file_data_bytes) > 0:
                                                        logger.info(
                                                            f"文件分段下载成功，实际大小: {len(file_data_bytes)} 字节")
                                                        file_data = base64.b64encode(file_data_bytes).decode('utf-8')
                                                        download_success = True
                                                        break
                                                    else:
                                                        logger.warning(f"文件下载失败，数据为空")
                                            except Exception as e:
                                                logger.error(f"尝试使用 {url} 分段下载文件时出错: {e}")
                                                logger.error(traceback.format_exc())

                                        # 如果所有尝试都失败
                                        if not download_success:
                                            logger.error("所有API端点尝试失败")
                                except Exception as e:
                                    logger.error(f"下载文件异常: {e}")
                                    logger.error(traceback.format_exc())
                                    file_data = None

                                if file_data:
                                    # 如果返回的是base64字符串，解码为二进制
                                    if isinstance(file_data, str):
                                        try:
                                            file_content = base64.b64decode(file_data)
                                        except Exception as e:
                                            logger.error(f"Base64解码失败: {e}")
                                            file_content = file_data.encode('utf-8')
                                    elif isinstance(file_data, dict) and "buffer" in file_data:
                                        try:
                                            file_content = base64.b64decode(file_data["buffer"])
                                        except Exception as e:
                                            logger.error(f"Buffer Base64解码失败: {e}")
                                            file_content = str(file_data).encode('utf-8')
                                    else:
                                        file_content = str(file_data).encode('utf-8')

                                    logger.info(f"文件下载成功，大小: {len(file_content)} 字节")
                                else:
                                    logger.error("文件下载失败或内容为空")
                                    await bot.send_text_message(from_wxid, "文件下载失败，请重新发送。")
                                    return
                            else:
                                logger.error("XML中缺少必要的附件ID")
                                await bot.send_text_message(from_wxid, "无法解析文件信息，请重新发送。")
                                return
                        else:
                            logger.error("XML中缺少appattach节点")
                            await bot.send_text_message(from_wxid, "无法解析文件信息，请重新发送。")
                            return
                    else:
                        logger.error("XML格式不正确，无法解析appmsg节点")
                        await bot.send_text_message(from_wxid, "无法解析文件信息，请重新发送。")
                        return
                except Exception as e:
                    logger.error(f"解析XML或下载文件失败: {e}")
                    logger.error(traceback.format_exc())
                    await bot.send_text_message(from_wxid, f"处理文件失败: {str(e)}")
                    return
            else:
                logger.warning(f"文件内容格式不支持: {type(file_content)}")
                await bot.send_text_message(from_wxid, "不支持的文件格式，请重新发送。")
                return

            # 缓存文件
            self.cache_file(sender_wxid, file_content, file_name, mime_type)

            # 如果是私聊，也缓存到聊天对象的ID
            if from_wxid != sender_wxid:
                self.cache_file(from_wxid, file_content, file_name, mime_type)

            logger.info(f"文件已缓存: {file_name}, 大小: {len(file_content) / 1024:.2f} KB, 类型: {mime_type}")
            logger.info('<<<[handle_file]')
        except Exception as e:
            logger.error(f"处理文件消息失败: {e}")
            logger.error(traceback.format_exc())

    async def send_quote_message(self, bot: WechatAPIClient, to_wxid: str, content: str, quoted_msg_id: str,
                                 quoted_wxid: str, quoted_nickname: str, quoted_content: str):
        """
        发送引用消息

        参数:
            bot: WechatAPIClient实例
            to_wxid: 消息接收人的wxid
            content: 要发送的新消息内容
            quoted_msg_id: 被引用消息的newMsgId
            quoted_wxid: 被引用消息发送者的wxid
            quoted_nickname: 被引用消息发送者的昵称
            quoted_content: 被引用的消息内容
        """
        # 构建引用消息的XML
        quote_xml = f'''<appmsg appid="" sdkver="0">
            <title>{content}</title>
            <des></des>
            <action></action>
            <type>57</type>
            <showtype>0</showtype>
            <soundtype>0</soundtype>
            <mediatagname></mediatagname>
            <messageext></messageext>
            <messageaction></messageaction>
            <content></content>
            <contentattr>0</contentattr>
            <url></url>
            <lowurl></lowurl>
            <dataurl></dataurl>
            <lowdataurl></lowdataurl>
            <songalbumurl></songalbumurl>
            <songlyric></songlyric>
            <appattach>
                <totallen>0</totallen>
                <attachid></attachid>
                <emoticonmd5></emoticonmd5>
                <fileext></fileext>
                <cdnthumbaeskey></cdnthumbaeskey>
                <aeskey></aeskey>
            </appattach>
            <extinfo></extinfo>
            <sourceusername></sourceusername>
            <sourcedisplayname></sourcedisplayname>
            <thumburl></thumburl>
            <md5></md5>
            <statextstr></statextstr>
            <directshare>0</directshare>
            <refermsg>
                <type>1</type>
                <svrid>{quoted_msg_id}</svrid>
                <fromusr>{quoted_wxid}</fromusr>
                <chatusr>{quoted_wxid}</chatusr>
                <displayname>{quoted_nickname}</displayname>
                <content>{quoted_content}</content>
            </refermsg>
        </appmsg>'''

        # 压缩XML结构为单行（去除XML结构中的换行和多余空格，但保留消息内容中的换行）
        # 注意：不要使用简单的replace('\n', '')，这会导致消息内容中的换行也被移除
        # 只压缩XML标签之间的空白，保留<title>和<content>标签内的格式
        import re
        # 先保存原始内容
        title_match = re.search(r'<title>(.*?)</title>', quote_xml, re.DOTALL)
        content_match = re.search(r'<content>(.*?)</content>', quote_xml, re.DOTALL)

        # 提取需要保留格式的内容
        title_content = title_match.group(1) if title_match else ""
        ref_content = content_match.group(1) if content_match else ""

        # 压缩整个XML
        quote_xml = quote_xml.replace('\n', '').replace('    ', '')

        # 如果原始内容中有换行，恢复它们
        if title_match and '\n' in title_content:
            # 在压缩后的XML中找到title标签内容并替换回原始内容
            quote_xml = re.sub(r'<title>(.*?)</title>', f'<title>{title_content}</title>', quote_xml)

        if content_match and '\n' in ref_content:
            # 在压缩后的XML中找到content标签内容并替换回原始内容
            quote_xml = re.sub(r'<content>(.*?)</content>', f'<content>{ref_content}</content>', quote_xml)

        # 使用send_app_message发送引用消息
        # type=57表示这是一个引用消息
        try:
            logger.info(f"发送引用消息: 引用MsgId={quoted_msg_id}, 引用人={quoted_nickname}")
            result = await bot.send_app_message(to_wxid, quote_xml, 57)
            return result
        except Exception as e:
            logger.error(f"发送引用消息失败: {e}")
            logger.error(traceback.format_exc())
            # 如果发送引用消息失败，回退到普通消息
            logger.info("回退到发送普通文本消息")
            return await bot.send_text_message(to_wxid, content)
