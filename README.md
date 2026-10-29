# AI Zelouri 1.9.6 → 2API（第一轮交付）

`com.aiart.aigenerator.aifilter.aieditor` 的协议还原 + 本地 OpenAI 兼容网关 + 压测 + 深度分析。

> ⚠️ **先看结论**：协议 100% 还原并实测通过，但**上游 AI 算力已整族下线**
> （SD 推理死 + 对象存储 9000 关闭 + 294 个 OpenAI key 全部 deactivated + Gemini 项目 suspended）。
> 真实出图为 0。详见 `docs/REVERSE-REPORT.md` §6。
> 本地网关默认 `auto` 模式：真上游无产物 → 回落 mock，并用响应头 `X-Zelouri-Source` 如实标注。

## 文件

| 文件 | 说明 |
|---|---|
| `zelouri_client.py` | 协议客户端。token 签名（SHA-256 + 硬编码盐）、10 分钟 JWT 自动续签、txt2img / img2img / transfer / qr_art / expand / video、旧版 HmacSHA512 视频签名 |
| `demo_server.py` | FastAPI OpenAI 兼容网关：`/v1/images/generations`、`/v1/images/edits`、`/v1/models`、`/api/styles`、`/api/upstream-probe`、`/healthz`、`/metrics`；并发闸门 + 背压 429 |
| `demo.html` | 浏览器演示台（风格下拉 / 文生图 / 图生图 / 耗时与来源 KPI / 一键探活）→ `GET /demo` |
| `stress_test.py` | 压测（p50/p95/p99、吞吐、状态码分布）+ `--upstream` 上游存活矩阵 |
| `verify_demo.py` | Playwright 真实浏览器 e2e（8 项断言 + 截图） |
| `docs/REVERSE-REPORT.md` | 深度逆向报告：架构 / 端点全表 / DTO / 签名 / 基础设施地图 / 停摆证据链 |
| `docs/NEW-API-HANDOFF.md` | new-api 对接交接书（路径 A 中转 / 路径 B 原生渠道 / 计费 / 排错表 / 验证四步） |
| `evidence/` | 原始证据：上游 openapi、风格表、脱敏 RC 快照、压测报告、上游矩阵、截图 |

## 快速开始

```powershell
cd 2api

# 1) 协议冒烟（打真实上游，只读 + 1 次生成探测）
python zelouri_client.py

# 2) 起网关（mock：不碰上游，用于 demo/压测）
$env:ZELOURI_MODE="mock"; $env:ZELOURI_PORT="8951"
Start-Process -WindowStyle Hidden python demo_server.py

# 3) 打开演示台
start http://127.0.0.1:8951/demo

# 4) 压测
python stress_test.py --base http://127.0.0.1:8951 --total 100 --conc 4

# 5) 浏览器 e2e
python verify_demo.py --url http://127.0.0.1:8951/demo

# 6) 上游存活矩阵（复核开关：txt2img_v2.ok 由 false 变 true 即上游复活）
python stress_test.py --upstream
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ZELOURI_MODE` | `auto` | `mock` / `real` / `auto`（auto = 先 real，空产物回落 mock） |
| `ZELOURI_PORT` | `8951` | 监听端口 |
| `ZELOURI_MAX_CONCURRENCY` | `4` | 生成并发上限（超出排队，30 s 超时 → 429） |
| `ZELOURI_QUEUE_TIMEOUT` | `30` | 排队超时秒 |
| `ZELOURI_UPSTREAM_TIMEOUT` | `180` | 上游超时秒 |

## OpenAI 兼容调用

```bash
curl -X POST http://127.0.0.1:8951/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red apple on a wooden table","style":"anime","size":"512x512","n":1}'
```

响应为标准 OpenAI 壳，额外带 `usage.source`；响应头 `X-Zelouri-Source: real|mock`。

## 实测基线（本机，mock，连接池复用）

| 并发 | 总数 | 成功率 | 吞吐 | p50 | p95 |
|---|---|---|---|---|---|
| 4 | 100 | 100% | 55.3 rps | 30 ms | 41 ms |
| 8 | 100 | 100% | 112.7 rps | 63 ms | 97 ms |

## 安全说明

- `evidence/remote_config.sample.json` 内所有密钥段（`config_api_key`×294、`config_api_key_gemini`、`key_hash_hmac_sha512`）
  **已打码**，入库前已用正则自检（无 `sk-` / `AIza` / HMAC 明文）。
- 上游签名盐 `serverapp@proxglobal%23%^?!154625` 是客户端硬编码常量，属于协议还原必需项，保留在 `zelouri_client.py`。
- 未提交任何 APK、`.so`、账号、代理凭据。
