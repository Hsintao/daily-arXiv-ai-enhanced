# 自建内容过滤 Worker

替代上游的 `spam.dw-dengwei.workers.dev`,让你不再依赖别人的私人服务。

接口完全兼容(和上游一致):

```
POST /  { "text": "..." }  →  { "sensitive": true | false }
```

---

## 部署步骤

### 1. 注册 Cloudflare 账号

https://dash.cloudflare.com/sign-up — 免费,不需要域名和信用卡。

### 2. 安装 wrangler

```bash
npm install -g wrangler
wrangler login
```

`wrangler login` 会打开浏览器让你授权。

### 3. 部署

```bash
cd filter-worker
wrangler deploy
```

成功后会输出你的地址,形如:

```
https://arxiv-content-filter.<你的子域>.workers.dev
```

**记下这个地址**,下一步要用。

### 4. 验证部署

```bash
# 健康检查
curl https://arxiv-content-filter.<你的子域>.workers.dev/health
# 期望: {"ok":true,"service":"arxiv-content-filter"}

# 正常内容
curl -X POST https://arxiv-content-filter.<你的子域>.workers.dev \
  -H "Content-Type: application/json" \
  -d '{"text":"A new attention mechanism for 3D reconstruction."}'
# 期望: {"sensitive":false}

# 敏感内容
curl -X POST https://arxiv-content-filter.<你的子域>.workers.dev \
  -H "Content-Type: application/json" \
  -d '{"text":"澳门赌场 六合彩 时时彩"}'
# 期望: {"sensitive":true}
```

### 5. 配置到仓库

**Settings → Secrets and variables → Actions → Secrets** → 新建:

| Secret | 值 |
|---|---|
| `FILTER_URL` | 你上一步拿到的地址 |

**如果 `FILTER_URL` 不设置**,代码会自动回退到上游的默认服务 —— 也就是说,**这一步没做也不影响运行**,只是仍然依赖别人。

---

## 可选:加访问令牌

默认情况下,任何知道 URL 的人都能调用你的 Worker(和上游行为一致)。这在配额上通常无所谓,但如果你在意:

**1. 生成一个随机字符串**

```bash
openssl rand -hex 16
```

**2. 在 `wrangler.toml` 里设置**

```toml
[vars]
REQUIRE_TOKEN = "你生成的随机串"
```

**3. 重新部署**

```bash
wrangler deploy
```

**4. 在 GitHub 加 Secret**

| Secret | 值 |
|---|---|
| `FILTER_TOKEN` | 同一个随机串 |

客户端会自动带上 `X-Filter-Token` 请求头。

> ⚠️ **不加 token 也能正常工作**,但加了之后如果两边不一致,过滤会返回 401 并触发 **fail-open**(即不过滤,而不是报错)。这是刻意的设计 —— 过滤器故障不应该阻断整个流程。

---

## 自定义敏感词

编辑 `worker.js` 里的 `DEFAULT_TERMS`,或通过环境变量追加:

```toml
[vars]
BLOCK_TERMS = "你的词1,你的词2"
```

`ALLOWLIST` 用于避免误杀。比如 "gambling disorder"(赌博成瘾)是正经的医学研究主题,所以它被放在允许列表里。

**建议**:先跑几轮,看看有没有误杀,再决定要不要加词。过滤过严会导致**论文被静默丢弃** —— 代码里 `is_sensitive` 返回 `True` 的论文会直接从结果中移除。

---

## 调整判定逻辑

当前实现是**关键词匹配**,简单、零成本、可预测。它的局限很明显:

- 不理解语义(反讽、学术讨论 vs 推广)
- 需要你维护词表
- 可能误杀,也可能漏掉变形写法

如果要做更好的判定,可以换成:

- **Cloudflare Workers AI** — 在 Worker 里直接调用小模型分类
- **OpenAI Moderation API** — 免费,但对中文效果一般
- **国内内容安全 API** — 阿里云/腾讯云,对中文准确度最高,但要额外配置

改动位置就在 `worker.js` 的 `classify()` 函数,接口契约不用变。

---

## 成本

Cloudflare Workers 免费额度对个人使用**绰绰有余**:每天 10 万次请求。

你的实际用量:约 200 次/天(每篇论文一次) × 2(摘要 + AI 输出各检查一次)≈ 400 次/天。

---

## 文件说明

| 文件 | 用途 |
|---|---|
| `worker.js` | Worker 主逻辑 |
| `wrangler.toml` | 部署配置 |

本地测试(**不需要 Cloudflare 账号**):

```bash
node test-worker.mjs
```
