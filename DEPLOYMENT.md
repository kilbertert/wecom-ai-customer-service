# 微信Coze服务部署指南

## 部署前检查

### 1. 环境要求
- Python 3.11+
- Redis 7.0+
- 至少 1GB RAM
- 网络连接正常

### 2. 依赖检查
```bash
# 运行验证脚本
python verify_core_flow.py

# 检查结果应该是: 验证结果: 5/5 项通过
```

### 3. 配置文件准备
```bash
# 复制环境变量模板
cp env.example .env

# 编辑配置文件 (必需)
vim .env
```

## 必需配置项

### 微信配置
```env
WECHAT_CORP_ID=your_corp_id_here
WECHAT_CORP_SECRET=your_corp_secret_here
WECHAT_KF_TOKEN=your_kf_token_here
WECHAT_ENCODING_AES_KEY=your_encoding_aes_key_here
WECHAT_CALLBACK_BASE_URL=https://your-domain.com
```

### Coze配置
```env
COZE_API_TOKEN=your_coze_api_token_here
COZE_BOT_ID=your_bot_id_here
```

### 系统配置
```env
REDIS_HOST=localhost
REDIS_PORT=6379
APP_SECRET_KEY=your_secret_key_here
APP_DEBUG=false
```

## 部署方式

### 方式1: 直接Python运行 (开发)

```bash
# 1. 启动Redis
docker run -d --name redis -p 6379:6379 redis:7-alpine

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动服务
python run.py

# 4. 验证服务
curl http://localhost:8000/monitoring/health
```

### 方式2: Docker容器部署 (推荐)

```bash
# 1. 构建镜像
docker build -t weixin-coze-service .

# 2. 运行容器
docker run -d \
  --name weixin-coze \
  -p 8000:8000 \
  --env-file .env \
  -v $(pwd)/temp_media:/app/temp_media \
  weixin-coze-service

# 3. 查看日志
docker logs -f weixin-coze
```

### 方式3: Docker Compose部署 (生产)

```bash
# 1. 启动所有服务
docker-compose up -d

# 2. 查看服务状态
docker-compose ps

# 3. 查看日志
docker-compose logs -f weixin-coze-service
```

## 微信配置步骤

### 1. 注册企业微信
1. 访问 https://work.weixin.qq.com
2. 注册企业账号
3. 获取CorpID和CorpSecret

### 2. 配置微信客服
1. 登录企业微信管理后台
2. 进入"微信客服"模块
3. 创建客服账号
4. 获取客服Token和EncodingAESKey

### 3. 配置回调URL
1. 在微信客服设置中配置回调URL
2. URL格式: `https://your-domain.com/wechat/kf/callback`
3. 确保URL支持HTTPS

### 4. 验证回调
```bash
# 测试回调验证
curl "https://your-domain.com/wechat/kf/callback?signature=test&timestamp=123&nonce=test&echostr=hello"
```

## Coze配置步骤

### 1. 注册Coze账号
1. 访问 https://www.coze.com
2. 注册开发者账号
3. 获取API Token

### 2. 创建工作流
1. 在Coze平台创建智能体
2. 配置工作流节点:
   - Start Node (输入节点)
   - 条件分支 (消息类型判断)
   - 意图识别 (LLM节点)
   - 知识库检索 (RAG节点)
   - 回复生成 (LLM节点)
   - 转人工判断 (LLM节点)
   - End Node (输出节点)

### 3. 获取工作流ID
1. 发布工作流
2. 获取Workflow ID
3. 配置到环境变量中

## 网络和安全配置

### 1. HTTPS配置
```nginx
# Nginx配置示例
server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 2. 防火墙配置
```bash
# 开放必要端口
ufw allow 80
ufw allow 443
ufw allow 6379  # Redis (内部访问)
```

### 3. 安全加固
- 定期更新依赖包
- 使用强密码
- 启用日志审计
- 配置访问控制

## 监控和维护

### 1. 健康检查
```bash
# 基本健康检查
curl https://your-domain.com/monitoring/health

# 详细健康检查
curl https://your-domain.com/monitoring/health/detailed

# 指标数据
curl https://your-domain.com/monitoring/metrics
```

### 2. 日志查看
```bash
# Docker日志
docker-compose logs -f weixin-coze-service

# 应用日志 (如果配置了文件日志)
tail -f logs/app.log
```

### 3. 性能监控
- 监控响应时间
- 检查内存使用
- 监控Redis连接
- 跟踪API调用

## 故障排除

### 常见问题

#### 1. 微信回调验证失败
```
错误: Invalid signature
解决:
- 检查WECHAT_KF_TOKEN配置
- 确认回调URL参数
- 验证SHA1签名算法
```

#### 2. Coze API调用失败
```
错误: API token invalid
解决:
- 检查COZE_API_TOKEN配置
- 确认工作流ID正确
- 验证网络连接
```

#### 3. Redis连接失败
```
错误: Connection refused
解决:
- 检查Redis服务状态
- 确认REDIS_HOST和REDIS_PORT
- 验证网络连接
```

#### 4. 消息发送失败
```
错误: send message failed
解决:
- 检查微信Access Token
- 确认客服权限
- 验证消息格式
```

### 调试模式

启用调试模式获取更多信息:
```env
APP_DEBUG=true
APP_LOG_LEVEL=DEBUG
```

## 备份和恢复

### 数据备份
```bash
# Redis数据备份
docker exec redis redis-cli save

# 配置文件备份
cp .env .env.backup

# 日志备份
tar -czf logs_backup.tar.gz logs/
```

### 服务重启
```bash
# Docker Compose重启
docker-compose restart weixin-coze-service

# 查看重启状态
docker-compose ps
```

## 扩展和高可用

### 水平扩展
```yaml
# docker-compose.scale.yml
version: '3.8'
services:
  weixin-coze-service:
    scale: 3
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 1G
```

### 负载均衡
```nginx
upstream weixin_coze {
    server 127.0.0.1:8000;
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;
}

server {
    listen 443 ssl;
    server_name your-domain.com;

    location / {
        proxy_pass http://weixin_coze;
    }
}
```

### Redis集群
```yaml
# Redis集群配置
version: '3.8'
services:
  redis-master:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  redis-slave:
    image: redis:7-alpine
    command: redis-server --slaveof redis-master 6379
    depends_on:
      - redis-master
```

---

## 快速部署检查清单

- [ ] 环境变量配置完成
- [ ] Redis服务运行正常
- [ ] 微信回调URL配置正确
- [ ] Coze工作流发布完成
- [ ] HTTPS证书配置完成
- [ ] 防火墙规则设置正确
- [ ] 监控告警配置完成
- [ ] 备份策略制定完成

**✅ 全部检查通过后，服务即可正常运行！**