# Model Compare：多模型响应对比工具

一个零第三方依赖的 Python 3.9+ 命令行工具，按供应商配置档案并发发送同一份文本提示词。它记录首字延迟、总耗时和完整回答，生成 JSON 数据和 Markdown 报告。

*A small, dependency-free CLI for comparing the latency and text responses of multiple model APIs. Configure provider endpoints, protocols, API keys, and model IDs in one JSON file.*

## 快速开始

1. 将仓库克隆到本地，进入项目目录。
2. 复制 `model-compare.example.json` 为 `model-compare.json`，填写你实际使用的供应商地址和模型 ID。
3. 设置所选档案对应的密钥环境变量，例如 `export OPENAI_API_KEY='你的密钥'`。
4. 运行下面的示例。只需保留你已配置并有权调用的模型。

```bash
python3 model_compare.py \
  --config model-compare.json \
  --prompt '用三句话解释什么是向量数据库。' \
  --model openai:main \
  --model anthropic:main \
  --model gemini:fast \
  --runs 3
```

程序会在 `model-compare-results/` 中生成包含逐次回答的 Markdown 报告和完整 JSON 数据。API 调用可能产生费用；`--runs 3` 表示每个模型发送三次请求。

## 先配置供应商

复制 `model-compare.example.json` 为 `model-compare.json`，再按实际服务修改。每个 `providers` 条目就是一个独立的供应商档案：

```json
{
  "providers": {
    "my_gateway": {
      "protocol": "openai-chat",
      "base_url": "https://your-gateway.example/v1",
      "api_key_env": "MY_GATEWAY_API_KEY",
      "models": {"fast": "provider/model-id"}
    }
  }
}
```

这里 `my_gateway` 是你自己起的档案名；`fast` 是命令行使用的模型别名；`provider/model-id` 是该服务 API 实际接收的模型 ID。运行 `--model my_gateway:fast` 时，工具从这个档案同时取出 URL、协议、密钥和模型 ID。

`base_url` 填 API 根地址，不含具体接口路径。工具会按 `protocol` 自动附加路径：

| 协议 | 自动附加的路径 | 典型用途 |
| --- | --- | --- |
| `openai-responses` | `/responses` | OpenAI Responses API |
| `openai-chat` | `/chat/completions` | OpenAI 兼容接口、聚合服务 |
| `anthropic-messages` | `/messages` | Anthropic Messages API |
| `gemini-generate-content` | `/models/{模型 ID}:streamGenerateContent?alt=sse` | Gemini GenerateContent API |

例如 OpenAI 官方 `base_url` 是 `https://api.openai.com/v1`；聚合服务常有自己的 `/v1` 根地址。协议表示请求体、认证头和流式响应的格式。一个聚合服务即使出售 Claude 或 Gemini 模型，只要它要求 OpenAI 兼容接口，就选 `openai-chat`。同一档案里的所有模型共用该 URL、协议和密钥；若某些模型需要不同协议，建另一个档案。

密钥推荐用 `api_key_env` 指定环境变量名，再在终端设置实际值，例如 `export MY_GATEWAY_API_KEY='你的密钥'`。也可在本地私有配置里用 `"api_key": "你的密钥"` 代替 `api_key_env`，两者只能选一个。不要分享带实际密钥的配置文件；输出报告不会写入密钥。

## 运行

只测试自己配置的聚合服务时：

```bash
python3 model_compare.py \
  --config model-compare.json \
  --prompt-file question.txt \
  --context-file context.txt \
  --system-file system.txt \
  --model my_gateway:fast \
  --out-dir results
```

`--prompt-file -` 可从标准输入读取。`--model` 可重复多次。默认每个模型请求一次，最多输出 1024 token，单次网络读取超时 180 秒。`--runs` 会并发发送重复请求；增加次数会增加费用和速率限制风险。

首字延迟从本机发起请求算到第一段可见文本；总耗时算到流结束。汇总采用成功请求的中位数。不同厂商的 token 统计口径、推理方式、缓存和输出长度可能不同；内容质量仍需人工判断。报告及 JSON 包含完整输入和回答，可能含敏感内容。工具不会自动重试，以保留首轮延迟。

## 开发与贡献

运行解析测试：`python3 -m unittest -v test_model_compare.py`。欢迎通过 Issue 报告接口兼容问题，并在 Pull Request 中附上复现步骤和对应测试。请勿提交真实密钥、私有提示词或运行结果。

## 许可证

本项目采用 [MIT License](LICENSE)。
