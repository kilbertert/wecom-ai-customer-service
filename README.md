# 微信客服接入Coze智能体服务

这是一个完整的微信客服接入Coze智能体的解决方案，基于FastAPI实现。

## 架构概述

```
微信客服服务端 → 中间适配层 → Coze智能体工作流 → 用户微信客户端
```

## 主要功能

- ✅ 微信回调验证和消息处理
- ✅ 消息标准化转换
- ✅ Coze工作流集成
- ✅ 媒体文件处理
- ✅ 监控和健康检查
- ✅ 异步任务处理

## 🎯 运行模式: 三种消息类型处理

**专门处理微信客服的文本、图片、语音三种消息类型**

### ✨ 核心功能
- 📝 **文本消息**: 直接处理文字咨询和问题
- 🖼️ **图片消息**: OCR识别图片内容，智能回答
- 🎤 **语音消息**: ASR转换语音为文字，智能回复
- 🤖 **Coze集成**: 调用智能工作流进行深度处理
- 📤 **自动回复**: 智能回复直接发送回微信客服

### 📋 支持的消息类型
| 消息类型 | 处理方式 | 输出格式 |
|---------|---------|---------|
| 文本消息 | 直接传递 | 智能回复文本 |
| 图片消息 | OCR识别内容 | 基于图片内容的回复 |
| 语音消息 | ASR转换文字 | 基于语音内容的回复 |

### ⚡ 优势特点
- 🚀 **轻量高效**: 只处理必要的消息类型
- 🎯 **精准处理**: 针对性优化每种消息类型
- 🤖 **智能集成**: 与Coze工作流无缝对接
- 📊 **易于监控**: 清晰的消息处理日志


## 快速开始

### 🚀 推荐：安全Docker部署

```bash
# 1. 复制环境变量模板
cp env.template .env

# 2. 编辑配置文件（必需）
# 填入真实的API密钥和配置信息
vim .env  # 或使用其他编辑器

# 3. 运行部署脚本
./deploy.sh

# 4. 验证部署
./deploy.sh verify
```

**安全提醒**: 生产环境请务必设置文件权限 `chmod 600 .env`

### 开发环境部署

#### 1. 环境准备

```bash
# 进入项目目录
cd weixin_coze_service

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

#### 2. 微信回调测试

```bash
# 运行测试菜单，选择测试模式
python run_test.py

# 或直接运行独立测试服务器
python test_wechat_callback.py

# 或运行集成业务测试
python test_wechat_simple.py
```

#### 3. 配置环境变量

复制环境变量模板并填写配置：

```bash
cp env.template .env
```

编辑 `.env` 文件，填入以下配置：

```env
# 微信配置
WECHAT_CORP_ID=your_actual_corp_id
WECHAT_CORP_SECRET=your_actual_corp_secret
WECHAT_KF_TOKEN=your_generated_token
WECHAT_ENCODING_AES_KEY=your_43_char_aes_key
WECHAT_CALLBACK_BASE_URL=https://your-production-domain.com

# 可选：只处理指定客服的消息
WECHAT_ALLOWED_OPEN_KFID=wk2m-vCwAAkKIHsr8jtoekF84d5m2qeQ

# Coze配置
COZE_API_TOKEN=your_actual_api_token
COZE_BOT_ID=your_actual_bot_id

# 应用配置
APP_SECRET_KEY=your_generated_secret_key

# Redis配置 (当前版本暂不需要)
# REDIS_HOST=localhost
# REDIS_PORT=6379
```

**重要**: 不要使用 `env.example` 文件作为生产配置，它包含占位符值。

**客服限制**: 如果设置了 `WECHAT_ALLOWED_OPEN_KFID`，系统将只处理来自指定客服的消息，其他客服的消息会被忽略。

#### 4. 启动服务

##### 使用Python直接启动
```bash
python run.py
```

##### 使用Docker启动
```bash
docker-compose up -d
```

### 4. 验证服务

访问以下端点验证服务状态：

- **API文档**: http://localhost:8000/docs
- **健康检查**: http://localhost:8000/monitoring/health
- **详细健康检查**: http://localhost:8000/monitoring/health/detailed
- **指标监控**: http://localhost:8000/monitoring/metrics

### 🔒 安全部署说明

#### 敏感信息保护

本项目已实施多项安全措施保护敏感信息：

- ✅ **移除代码默认值**: 所有敏感信息不再有代码级别的默认值
- ✅ **环境变量隔离**: 敏感信息通过环境变量传递，不存储在代码中
- ✅ **文件权限控制**: 环境变量文件建议设置为600权限
- ✅ **版本控制排除**: 敏感文件已加入`.gitignore`

#### 生产环境建议

1. **文件权限设置**:
   ```bash
   chmod 600 .env
   ```

2. **定期轮换密钥**:
   - 每3-6个月轮换API Token
   - 定期更新应用密钥

3. **监控和审计**:
   - 启用日志审计
   - 监控异常访问
   - 设置告警机制

#### 部署脚本使用

项目提供便捷的部署脚本：

```bash
# 部署服务
./deploy.sh

# 查看状态
./deploy.sh status

# 查看日志
./deploy.sh logs

# 重启服务
./deploy.sh restart

# 停止服务
./deploy.sh stop

# 清理资源
./deploy.sh clean
```

## 配置说明

### 微信配置

| 参数 | 说明 | 示例 |
|------|------|------|
| WECHAT_CORP_ID | 企业微信CorpID | ww1234567890 |
| WECHAT_CORP_SECRET | 企业微信Secret | your_secret_here |
| WECHAT_KF_TOKEN | 微信客服Token | your_token_here |
| WECHAT_ENCODING_AES_KEY | 消息加解密Key | your_aes_key_here |
| WECHAT_ALLOWED_OPEN_KFID | 只处理指定客服的消息（可选） | wk2m-vCwAAkKIHsr8jtoekF84d5m2qeQ |

### Coze配置

| 参数 | 说明 | 示例 |
|------|------|------|
| COZE_API_TOKEN | Coze API Token | pat_1234567890 |
| COZE_BOT_ID | Bot ID (工作流ID) | 7595036480042729498 |
| COZE_APP_ID | App ID (用于stream_run API，直接获取完整结果) | 7599719407673868324 |

### 系统配置

**单轮对话模式已移除Redis依赖**，无需配置Redis相关参数。

## API端点

### 微信回调
- `GET /wechat/kf/callback` - 微信回调验证
- `POST /wechat/kf/callback` - 微信消息处理

### 监控接口
- `GET /monitoring/health` - 健康检查
- `GET /monitoring/health/detailed` - 详细健康检查
- `GET /monitoring/metrics` - 指标数据
- `GET /monitoring/stats` - 统计信息

## 部署说明

### 生产环境部署

1. **配置HTTPS**: 微信要求回调URL必须使用HTTPS
2. **设置反向代理**: 使用Nginx作为反向代理
3. **配置监控**: 集成Prometheus/Grafana监控
4. **日志管理**: 配置ELK日志收集

### Docker部署

```bash
# 构建镜像
docker build -t weixin-coze-service .

# 运行容器
docker run -d \
  --name weixin-coze \
  -p 8000:8000 \
  -v $(pwd)/.env:/app/.env:ro \
  -v $(pwd)/temp_media:/app/temp_media \
  weixin-coze-service
```

### 扩展部署

```yaml
# 使用docker-compose扩展部署
version: '3.8'
services:
  weixin-coze-service:
    image: weixin-coze-service
    deploy:
      replicas: 3
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
```

## 开发指南

### 项目结构

```
weixin_coze_service/
├── app/
│   ├── core/           # 核心配置和异常处理
│   ├── models/         # 数据模型
│   ├── routes/         # API路由
│   ├── services/       # 业务服务层
│   └── tasks/          # 异步任务
├── tests/              # 单元测试
├── requirements.txt    # Python依赖
├── Dockerfile          # Docker镜像
├── docker-compose.yml  # Docker编排
├── run.py              # 主启动脚本
├── run_test.py         # 测试菜单脚本
├── test_wechat_callback.py    # 独立回调测试
├── test_wechat_simple.py      # 集成业务测试
├── WECHAT_TEST_GUIDE.md       # 测试指南
├── COZE_WORKFLOW_GUIDE.md     # Coze配置指南
├── verify_simple.py           # 代码验证脚本
└── README.md          # 项目文档
```

### 添加新功能

1. 在 `models/` 中定义数据模型
2. 在 `services/` 中实现业务逻辑
3. 在 `routes/` 中添加API端点
4. 在 `main.py` 中注册路由

### 测试

```bash
# 运行单元测试
pytest

# 运行带覆盖率的测试
pytest --cov=app --cov-report=html
```

## 监控和日志

### 指标监控

服务提供以下指标：

- 活跃会话数量
- 消息处理统计
- 系统资源使用率
- API调用状态

### 日志配置

使用结构化日志，支持JSON格式输出，便于日志收集和分析。

## 故障排除

### 常见问题

1. **微信回调验证失败**
   - 检查Token配置是否正确
   - 确认回调URL格式

2. **Coze工作流调用失败**
   - 验证API Token是否有效
   - 检查工作流ID是否正确


### 调试模式

启用调试模式获取更多日志信息：

```env
APP_DEBUG=true
APP_LOG_LEVEL=DEBUG
```

## 许可证

本项目采用MIT许可证。

## 贡献

欢迎提交Issue和Pull Request来改进项目。

## 联系方式

如有问题，请通过以下方式联系：

- 提交GitHub Issue
- 发送邮件至开发团队