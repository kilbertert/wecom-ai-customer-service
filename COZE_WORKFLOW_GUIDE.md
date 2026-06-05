# 🎯 Coze工作流配置指南 - 三种消息类型处理

## 📋 功能概述

本服务专门处理微信客服的三种消息类型：
- **文本消息** (`text`)
- **图片消息** (`image`)
- **语音消息** (`voice`)

## 🔄 工作流设计

### 推荐工作流结构

```
开始节点 → 消息类型分支 → 内容处理 → Coze智能体 → 输出格式化 → 结束节点
```

## 🎨 详细配置步骤

### 1. 开始节点配置

**节点名称**: `输入处理`
**输入变量**:
```json
{
  "user_id": "string",
  "message_type": "string",
  "content": "object",
  "metadata": "object",
  "context": "object"
}
```

**Schema定义**:
```json
{
  "type": "object",
  "properties": {
    "user_id": {"type": "string"},
    "message_type": {"type": "string", "enum": ["text", "image", "voice"]},
    "content": {
      "type": "object",
      "properties": {
        "text": {"type": "string"},
        "media_id": {"type": "string"},
        "url": {"type": "string"},
        "format": {"type": "string"}
      }
    },
    "metadata": {"type": "object"},
    "context": {"type": "object"}
  }
}
```

### 2. 消息类型分支节点

创建条件分支，处理三种消息类型：

#### 分支1: 文本消息
```
条件: {{input_data.message_type == 'text'}}
动作: 直接进入智能体处理
```

#### 分支2: 图片消息
```
条件: {{input_data.message_type == 'image'}}
动作: 使用OCR插件识别图片内容
```

#### 分支3: 语音消息
```
条件: {{input_data.message_type == 'voice'}}
动作: 使用ASR插件转换语音为文字
```

### 3. 图片处理子流程

#### OCR识别节点
```
输入: {{input_data.content.url}}
提示词: "请详细描述图片中的内容，包括文字、场景、物体等信息"
输出: image_description
```

#### 内容合并
```
输入: {{image_description}}
逻辑: 将图片描述与用户可能附加的文字描述合并
```

### 4. 语音处理子流程

#### ASR转换节点
```
输入: {{input_data.content.url}}
输出: voice_text
```

#### 文本提取
```
输入: {{voice_text}}
逻辑: 提取语音转换后的文字内容
```

### 5. 智能体处理节点

#### System Prompt 示例
```
你是一个专业的智能客服助手，负责处理用户的咨询。

用户可能通过以下方式发送信息：
1. 直接发送文字描述问题
2. 发送图片询问相关问题
3. 发送语音描述问题

请根据用户提供的文字、图片或语音内容，智能地回答用户的问题。

回答要求：
- 语气友好、专业
- 回答准确、简洁
- 如果信息不完整，主动询问补充信息
- 支持多轮对话
```

#### 用户问题构建
```
基于输入数据构建用户问题：

{{input_data.content.text}} 或
图片描述：{{image_description}} 或
语音内容：{{voice_text}}

用户ID: {{input_data.user_id}}
消息时间: {{input_data.metadata.timestamp}}
```

### 6. 输出节点配置

**节点名称**: `回复格式化`

**输出格式**:
```json
{
  "action": "reply",
  "reply_content": {
    "msgtype": "text",
    "text": {
      "content": "你的智能回复内容"
    }
  },
  "metadata": {
    "intent_type": "售前问题|售后问题|功能咨询|投诉|闲聊问候|其他",
    "confidence": 0.95,
    "processed_type": "text|image|voice"
  }
}
```

## 🛠️ 插件配置

### OCR插件 (图片识别)
```
插件: 图像文字识别
配置:
- 语言: 中文+英文
- 识别模式: 全面识别
- 输出格式: 结构化文本
```

### ASR插件 (语音识别)
```
插件: 语音转文字
配置:
- 语言: 中文
- 音频格式: AMR (微信语音格式)
- 降噪处理: 开启
- 输出格式: 纯文本
```

## 📝 测试用例

### 文本消息测试
```json
输入:
{
  "message_type": "text",
  "content": {"text": "你们的退货政策是什么？"}
}

期望输出:
{
  "action": "reply",
  "reply_content": {
    "msgtype": "text",
    "text": {"content": "我们的退货政策是..."}
  }
}
```

### 图片消息测试
```json
输入:
{
  "message_type": "image",
  "content": {
    "media_id": "media_123",
    "url": "https://example.com/image.jpg"
  }
}

期望输出:
{
  "action": "reply",
  "reply_content": {
    "msgtype": "text",
    "text": {"content": "根据图片显示的问题，我的回答是..."}
  }
}
```

### 语音消息测试
```json
输入:
{
  "message_type": "voice",
  "content": {
    "media_id": "voice_123",
    "url": "https://example.com/voice.amr",
    "format": "amr"
  }
}

期望输出:
{
  "action": "reply",
  "reply_content": {
    "msgtype": "text",
    "text": {"content": "根据您的语音描述，我的回答是..."}
  }
}
```

## 🔍 调试技巧

### 1. 查看输入数据
在工作流中添加调试节点，输出：
```
输入数据: {{input_data}}
消息类型: {{input_data.message_type}}
内容: {{input_data.content}}
```

### 2. 测试各分支
分别发送三种类型的消息，确认各分支正常工作

### 3. 检查输出格式
确保输出格式完全符合服务期望的格式

## ⚠️ 注意事项

1. **图片处理**: 确保OCR插件能处理微信图片格式
2. **语音处理**: 确认ASR插件支持AMR格式
3. **输出格式**: 必须严格按照指定的JSON格式输出
4. **错误处理**: 为每种消息类型添加异常处理逻辑

## 🎯 快速开始

1. 在Coze平台创建新工作流
2. 按照上述配置创建各节点
3. 添加必要的插件
4. 发布工作流并获取Workflow ID
5. 在环境变量中配置`COZE_WORKFLOW_ID`
6. 测试三种消息类型的处理

现在你可以专注于创建智能的回复逻辑，剩下的消息处理和格式转换都由服务自动完成！🚀