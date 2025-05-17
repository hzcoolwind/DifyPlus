# DifyPlus 插件 🤖

## 简介

DifyPlus 插件是为 XYBotV2 机器人框架设计的一个插件，它允许机器人与 Dify (一个 LLM 应用开发平台) 进行交互。通过这个插件，你可以让你的微信机器人具备强大的自然语言处理能力，例如文本生成、对话、语音处理和文件处理等。🚀
该插件源于“老夏的金库的Dify插件”，去除了积分相关功能，增加了群聊组支持。不同群聊组可以配置不同的可用模型（智能体），每个群聊组可以有多个群聊。 没有在群聊组中配置的群聊，不会响应。

## 特性

*   **多消息类型支持:** 支持文本、@消息、语音、图片、视频和文件消息的处理。💬
*   **Dify 集成:** 无缝对接 Dify 平台，利用其强大的 LLM 能力。🔗
*   **灵活的配置:** 允许配置 API 密钥、基础 URL、命令、提示语、价格、代理等。⚙️
*   **流式响应:** 使用 Dify 的流式响应模式，逐步返回结果，提升用户体验。✨
*   **语音合成 (TTS) 支持:** 可选的 TTS 功能，将文本回复转换为语音消息。🗣️
*   **文件上传:** 支持上传语音、图片、视频和文件到 Dify 进行处理。📤
*   **媒体文件处理:** 自动识别并发送回复中的链接指向的媒体文件（语音、图片、视频）。🖼️
*   **错误处理:** 完善的错误处理机制，当 Dify 返回错误时，能向用户提供清晰的错误信息。⚠️
*   **支持群聊组** 不同群聊组可以配置不同的模型（智能体），每个群聊组可以配置多个群聊。💬
*   **私聊唤醒** 支持私聊唤醒词唤醒，不唤醒不回复。AI回复带Title，以便区分真人和机器人。⚠️

## 安装

1.  确保你已经安装了 XYBotV2 机器人框架。 ✅
2.  将 `Dify` 插件文件夹复制到 XYBotV2 的 `plugins` 目录下。 📁

## 配置

1.  编辑 `main_config.toml` 文件，配置管理员列表：

    ```toml
    [XYBot]
    admins = ["your_wxid"] # 你的微信ID
    ```

2.  编辑 `plugins/DifyPlus/config.toml` 文件，配置 Dify 插件：
 
    ```toml
    [Dify]
    enable = true                           # 是否启用插件
    default-model = "客服"                   # 私聊默认使用的智能体，私聊可以用全部智能体
    need-wakeup = true                      # 私聊是否需要唤醒词
    reply-title = '[AI回复]'                 # 私聊回复内容添加抬头, 不需要就清空''
    support_agent_mode = true               # 是否支持Agent模式：
    http-proxy = ""                         # HTTP代理配置，格式为"http://代理地址:端口"，不需要则留空
    voice_reply_all = false                 # 是否总是使用语音回复，设为true则所有回复都转为语音消息
    robot-names = ["机器人", "智能助手"]      # @机器人类似@登录的微信
    commands = ["/help", "/帮助", "/list", "/智能体"]    # 可以用来显示command-tip，智能体列表
    command-tip = """
        💬AI聊天指令：
    
        1. 切换默认智能体
           （将会一直保持到下次切换）：
          - @客服 切换
            （切换到客服智能体）
          - @合同 切换
            （切换到合同智能体）
    
       2. 使用唤醒词激活智能体聊天：
          - 小禾 消息内容
            （使用客服智能体聊天）
          - 小谷 消息内容
            （使用谷歌智能体聊天）
          - 小手 消息内容
            （使用快手智能体聊天）
    
       3. 使用@机器人激活默认智能体进行聊天：
          - @（智能客服的微信id）内容
            （@群里的智能客服微信聊天）
          - @机器人 内容
            （@robot-name 进行聊天）
          - @智能助手 内容
            （@robot-name 进行聊天）
    
       4. 使用触发词激活相应的智能体进行聊天
          - 分析下这个合同@合同
           （激活合同智能体进行聊天）
          - 这可能是啥问题？@客服
           （激活客服智能体进行聊天）
       
       5. 使用/list命令显示可用智能体和默认智能体
        """
    
    [Dify.models]
    # 智能体配置，不同的智能体接入不同的dify chatflow、agent
    # 触发词用来切换智能体或唤醒智能体
    # 唤醒词用来唤醒对应智能体，如果可用智能体中唤醒词有相同的，唤醒第一个可用智能体
    
    [Dify.models."合同"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@合同"]
    wakeup-words = ["小同"]
    description = "合同审核智能体，上传合同文档，然后要求分析合同或关注的重点。"
    
    [Dify.models."快手"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@快手"]
    wakeup-words = ["小手"]
    description = "快手绘画智能体，给出绘画要求生成4张图片。"
    
    [Dify.models."谷歌"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@谷歌"]
    wakeup-words = ["小谷"]
    description = "谷歌绘画智能体，给出绘画要求生成图片然后可以对该图片对话修改。"
    
    [Dify.models."FLUX"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@小F"]
    wakeup-words = ["小F"]
    description = "FLUX绘画智能体，给出绘画要求生成4张图片。"
    
    [Dify.models."智谱"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@智谱"]
    wakeup-words = ["小谱"]
    description = "智谱绘画智能体，给出绘画要求生成1张图片或6秒视频。"
    
    [Dify.models."换脸"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@换脸"]
    wakeup-words = ["小脸"]
    description = "换脸智能体，先发送一张需要换脸的源图片，然后再上传一张目标脸的照片。"
    
    [Dify.models."证券"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@证券"]
    wakeup-words = ["小券"]
    description = "证券分析智能体"
    
    [Dify.models."客服甲"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@小甲"]
    wakeup-words = ["小甲"]
    description = "客服甲智能体"
    
    [Dify.models."客服乙"]
    api-key = "app-xxx"
    base-url = "https://api.dify.ai/v1"
    trigger-words = ["@小乙"]
    wakeup-words = ["小乙"]
    description = "客服乙智能体"
    
    [Dify.groups]
    # 群组设置，相同类型的群聊放在一个群组下，允许使用相同的智能体组
    # 智能体组中第一个智能体，为该群组默认智能体。 @‘群组中的智能客服微信’使用默认智能体
    # 通过智能体切换命令用户可在可用智能体间切换默认智能体
    # 例如：
    #     @客服 切换   （切换默认智能体到客服智能体）
    #     @合同 切换   （切换默认智能体到合同智能体）
    # 唤醒词用来唤醒对应智能体， 如果可用智能体中唤醒词有相同的，唤醒第一个可用智能体。
    # group-names与group-ids 一一对应， 为群聊简称，用来标注改群聊id的群聊简称。
    # group-id 可以通过管理端-通讯录-群聊查询。
    # csrs 表示人工座席，返回信息中包含@@@CSRS@@@标记将发送@信息给人工座席。
    #### 注意：group-id 不可重复，不能在不同群组中同时出现 ####
    
    [Dify.groups."客服甲群"]
    group-names = ['客服甲群']
    group-ids = ['575407093@chatroom']
    models = ["客服甲"]
    csrs = ['wxid_f3b19']
    command-tip = """
    💬AI聊天指令：
    
    1. 使用唤醒词激活对应智能体聊天：
       - 小甲 消息内容
    
       2. 使用@机器人激活默认智能体聊天：
          - @（智能客服的微信id）内容
          - @机器人 内容
          - @智能助手 内容
    
       3. 使用触发词激活对应智能体聊天
          - 内容 @小甲
          - @小甲 内容
          - 内容 @小甲 内容
    
       4. 显示可用智能体
          - /list
          - /智能体
       """
    
    [Dify.groups."客服测试群"]
    group-names = ['客服测试群']
    group-ids = ['522366361@chatroom']
    models = ["客服甲", "合同", "快手", "谷歌"]
    csrs = ['wxid_f3b19']
    command-tip = """
    💬AI聊天指令：
    
    1. 使用唤醒词激活智能体聊天：
       - 小甲 消息内容
    
       2. 使用@机器人进行聊天：
          - @（智能客服的微信id）内容
          - @机器人 内容
          - @智能助手 内容
    
       3. 使用触发词激活智能体聊天
          - 内容 @客服
          - @客服 内容
          - 内容 @客服 内容
       """
    
    [Dify.groups."客服乙群"]
    group-names = ['客服乙群']
    group-ids = ['5707161@chatroom']
    models = ["客服乙"]
    
    [Dify.groups."公司群"]
    group-names = ['公司群']
    group-ids = ['564988@chatroom']
    models = ["客服", "合同", "快手", "谷歌"]
    csrs = ['wxid_80mooh3b19']
    
    #[Dify.groups."公司销售群"]
    #group-names = []
    #group-ids = []
    #models = []
    #
    #[Dify.groups."公司研发群"]
    #group-names = []
    #group-ids = []
    #models = []
    
    
    ```

## 使用方法

1.  在微信中向机器人发送命令，例如 `@客服 你好` 或者`@机器人 你好` (在群聊中)。💬
2.  机器人会将你的消息发送到 Dify，并将 Dify 的回复返回给你。 🤖
3.  如果启用了 TTS，机器人会将文本回复转换为语音消息。 🗣️
4.  参考配置文件说明。

## 消息类型支持

*   **文本消息:**  直接发送文本消息给机器人。 📝
*   **@消息:**    在群聊中 @机器人 并发送消息。 📢
*   **语音消息:**  发送语音消息给机器人。 🎤
*   **图片消息:**  发送图片消息给机器人。 🖼️
*   **视频消息:**  发送视频消息给机器人。 🎬
*   **文件消息:**  发送文件消息给机器人。 📄
*   **文字引用:**  发送文字引用消息给机器人。
*   **图片引用:**  发送图片引用消息给机器人。
*   **文件引用:**  发送文件引用消息给机器人。

## 依赖

*   XYBotV2 机器人框架
*   `aiohttp`
*   `filetype`
*   `loguru`
*   `tomllib` (Python 3.11+)  or `toml` (Python < 3.11)
*   `WechatAPI`
*   `database.XYBotDB`
*   `utils.decorators`
*   `utils.plugin_base`

## Change Log

*   **1.0.0**  初始版本 🐣

## 注意事项

*   请确保你的 Dify API 密钥和基础 URL 配置正确。🔑
*   语音合成功能依赖于第三方 API，请确保 API 可用。 🌐
*   如果遇到问题，请查看 XYBotV2 的日志文件 `logs/xybot.log`。 🔍

## 作者

*   冷风 👨‍💻

**基于老夏的金库，感谢老夏** 😊

**可以随意修改使用，欢迎持续共享**

## License

MIT 📜