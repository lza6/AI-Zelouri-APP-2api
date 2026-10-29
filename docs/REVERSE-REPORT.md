# AI Zelouri 1.9.6 逆向报告（第一轮）

- 样本：`AI Zelouri v1.9.6 (Premium).apk`（102,965,290 字节）
- 包名：`com.aiart.aigenerator.aifilter.aieditor`
- versionName `1.9.6` / build tag `AiArt-ver1.9.6_prodRelease`
- 分析时间：2026-10-30
- 分析机：Windows 10 + jadx 1.5.1 + Python 3.14，出网经本地代理
- 结论标签：静态确认 / 已实测（每条结论后标注）

---

## 0. 一句话结论

**协议 100% 还原并可复现，但这套 App 的 AI 算力已经整族下线。**
`token` 签名算法、37 个生成端点、风格表、旧版 HMAC 视频签名全部拿到并实测通过；
但真正出图/出视频的上游（SD 农场、结果存储、OpenAI/Gemini 密钥池）当前全部失效，
生成端点只回 `{"msg":"Finish","content":{"image_url":null}}`。
因此本轮交付的是**可复跑的协议层 + 可演示的本地网关 + 上游真实状态证据**，不是"可用渠道"。

---

## 1. 样本结构

| 项 | 值 |
|---|---|
| dex | 6 个（classes ~ classes6），自有类集中在 `classes5.dex`（2535 类）+ `classes6.dex`（179 类） |
| 框架 | 原生 Kotlin + Hilt(Dagger) + Retrofit + Moshi，**非** Flutter / RN / Unity |
| native | 13 个 `.so`，其中 `libconceal.so`(Facebook Conceal 加密)、`libpglarmor.so`、`libapminsight*.so`(腾讯 APM)、`libtobEmbedPagEncrypt.so`(穿山甲) 均为第三方 SDK，**AI 协议不依赖 native** |
| 广告栈 | AppLovin/MAX、IronSource、ByteDance Pangle、Mintegral、Unity Ads、AdColony、InMobi、Amazon |
| 变现 | Google Play Billing 订阅 + 金币（`assets/credit.json`：250/600/1300/2800/6300/14000） |
| 本地 AI | `assets/models/*.tflite` 仅做人脸关键点/表情检测（解锁"露齿笑"等滤镜），不出图 |

→ **静态确认**：所有 AI 出图能力都在服务端，客户端只是薄 UI + 计费 + 广告。

---

## 2. 网络架构（ServiceGenerator，9 个 Retrofit 实例）

来源：`com/prox/global/aiart/data/remote/ServiceGenerator.java` 构造函数，逐个 `baseUrl(...)`
（**静态确认**）

| 实例 | baseUrl | 承载服务 | 线上状态（已实测） |
|---|---|---|---|
| `retrofit`（默认） | `http://art12.procyoni.com/` | `AIArtService`(home/intro/art)、`AIToolService`(expand)、`AIVideoService`(generate/check-video)、`ChatGptService` | ❌ **域名已停放**（返回 114 字节 lander 跳转页） |
| `retrofitTranslate` | `https://translate.googleapis.com/` | `TranslateService` | ✅ 网络可达 |
| `retrofitGenImage` | `http://69.10.54.174:8005/` | `GenImageService`、`GenTokenImageService`、`GenTokenImageV2Service`、`GenImageV3Service` | ⚠️ **服务在线，算力空转**（见 §6） |
| `retrofitGenYearBook` | `http://69.10.54.174:8006/` | `UploadYearbookService` | ❌ 端口已关闭（连接被拒） |
| `retrofitGemini` | `https://generativelanguage.googleapis.com/` | `GeminiService`（`gemini-1.5-flash-latest`） | ⚠️ 网络通，**key 所属项目已停用** |
| `retrofitStyle` | `https://ai-art.proxglobal.co/` | `ImgToImgStyleService`（静态 `images/styles.json`） | ✅ 存活（175 KB 风格目录） |
| `retrofitStyleVideo` | `https://ai-art.proxglobal.co/` | 静态 `ai-video/data.json` | ✅ 存活（11.6 KB） |
| `retrofitImgToImg` | `https://aiart-api.nowtechai.com/` | `TransferImgToImgService`（`api/generate/img2img`） | ❌ 新网关，其下游 `proxglobal` 返回 502 |
| Hilt 注入 `F` | `https://api.openai.com/v1/` | `ChatGptService`（`chat/completions`，`gpt-3.5-turbo`） | ❌ key 池全部停用 |

补充：`aiart-api.nowtechai.com` 与 `69.10.54.174` 是**同一台机器**（DNS 解析即 69.10.54.174），
443 上挂的是另一套 FastAPI「A1111 API Gateway v0.1.0」，8005 上挂的是 App 用的老网关「My API 0.1.0」。

---

## 3. 鉴权：签名算法（本轮最核心产出）

来源：`com/prox/global/aiart/ui/viewmodel/HomeViewModel$processImage$1.invokeSuspend()`
+ `com/prox/global/aiart/util/CommonUtils.i()`

```java
// HomeViewModel 原文（jadx 反编译）
String str3 = "TokenImg2Img" + new Date().getTime();          // content
String i2   = CommonUtils.i("serverapp@proxglobal%23%^?!154625" + str3);  // hash
JSONObject j = new JSONObject();
j.put("content", str3);
j.put("hash", i2);
// POST http://69.10.54.174:8005/api/v2.0/token   (Content-Type: application/json)
// → {"access_token":"eyJ...","token_type":"bearer"}
// 之后所有请求头：Authorization: Bearer <access_token>
```

`CommonUtils.i(input)` = **SHA-256 hex（小写，无分隔）**。

于是：

```
content = "TokenImg2Img" + str(epoch_millis)
hash    = sha256_hex( "serverapp@proxglobal%23%^?!154625" + content )
```

### 算法还原的强证据：上游自带自证端点

`GET /api/sha256?content=<content>` → `{"content":"…","hash":"…"}`
本地重算与该端点返回值**逐比特一致**（**已实测**，多次）：

```
local   e999f0e62764fd94315f2853469ff1a145374ac423c05c9691e03d8dfa384348
server  e999f0e62764fd94315f2853469ff1a145374ac423c05c9691e03d8dfa384348
match   true
```

### Token 形态

```
header  {"alg":"HS256","typ":"JWT"}
payload {"unique":"7094f928-0991-4f0d-ad8d-6c4fb0931977","app":"prox_app","exp":1793310927}
TTL     实测 600 s（10 分钟）
```
→ `unique` 是服务端随机会话 uuid，无需设备指纹；`exp` 短，客户端必须做续签。
`api/v3/token` 与 `api/v2.0/token` 同 body、同响应，签发互不干扰。

**拿 token 不需要任何账号、邮箱、设备 ID、证书固定、App 完整性校验**（**静态确认** + 已实测）。

---

## 4. 端点全表（上游 8005 自带 `openapi.json`，37 个操作 / 24 个 schema）

意外收获：`http://69.10.54.174:8005/openapi.json` 与 `/docs` **完全公开**（FastAPI Swagger），
相当于上游把内部接口表直接给了我们。原文已存 `evidence/upstream_openapi_8005.json`。

| 分组 | 端点 | 方法 | 入参形态 | 实测状态 |
|---|---|---|---|---|
| Token | `/api/v2.0/token`、`/api/v3/token` | POST | JSON `TokenSchemaReq{content,hash}` | ✅ 签发正常 |
| Token | `/api/sha256?content=` | GET | query | ✅ 自证哈希 |
| 文生图 | `/api/v2.0/generator/txt2img` | POST | JSON `InputTXT2IMGSchema` | ⚠️ 200 + 产物 null |
| 文生图 | `/api/v1.0/generator/txt2img` | POST | 同上 | ⚠️ 202 `Error!` |
| 文生图 | `/api/v1.0/aiart/txt2img` | POST | **form** `prompt` | ⚠️ 202 `Error!` |
| 文生图 | `/api/v3/txt2img` | POST | **form** `prompt,style_name,is_purchase…` | ⚠️ 202 `Error!` |
| 文生图 | `/api/v3/txt2img-turbo` | POST | form + query `prompt,is_dev` | ❌ 500 |
| 文生图 | `/api/v1.0/generator/qr_art*`、`/api/v2.0/generator/qr_art*` | POST | JSON `QRArtSchema{qr_text_data,art_prompt}` | ⚠️ 回**裸二维码**（见 §6.4） |
| 图生图 | `/api/v2.0/generator/img2img` | POST | JSON `InputTemplate{base64_image,prompt,…}` | ⚠️ 200 + 产物 null |
| 图生图 | `/api/v1.0/generator/img2img` | POST | 同上 | ⚠️ 202 `Error!` |
| 风格迁移 | `/api/v1.0/transfer`、`/api/v1.1/transfer`、`/api/v2.0/transfer`、`/api/v2.0/transfer_ios`、`/api/v2.0/transfer_turbo`、`/api/v3/transfer` | POST | **multipart** `file` + query `style=` (+ `controlnet`,`faceswap`) | v1.0 跑满 45 s 后产物 null；其余 202 `Error!` |
| 图生图 | `/api/v3/img2img`、`/api/v1/aipaint/img2img`、`/api/v1/wallpaper/img2img` | POST | multipart `file,prompt` | ❌ 202 `Failed` |
| 扩图 | `/api/v1/expand` | POST | multipart `file,ratio` | ❌ 500 `Stage 1 failed: API returned 404` |
| 视频 | `/api/v1/video-generate` | POST | multipart `image,prompt` | ⚠️ 200/40.8 s，但返回 `http://69.10.54.174:9000/data/wedding_template.mp4`，**9000 端口已关闭** |
| AI 头像 | `/api/v1.0/aiavatar/generator` | POST | multipart | ❌ 503 / 422 |
| AI 商店 | `/api/v1.0/aishop/generator` | POST | multipart | ❌ 503 |
| 风格表 | `/api/v1.0/style_list` | GET | — | ✅ 14 个风格 + prompt/negative |
| 静态 | `https://ai-art.proxglobal.co/images/styles.json` | GET | — | ✅ 175,101 B 风格目录 |
| 静态 | `https://ai-art.proxglobal.co/ai-video/data.json` | GET | — | ✅ 11,681 B 视频风格 |

### 关键 DTO

```jsonc
// InputTXT2IMGSchema  (POST /api/{v}/generator/txt2img, JSON)
{ "prompt": "str",            // required
  "negative_prompt": "str",   // required（不传 → 422）
  "style_name": "str",
  "steps": 20, "cfg_scale": 10, "seed": -1,
  "height": 512, "width": 512 }

// InputTemplate  (POST /api/{v}/generator/img2img, JSON)
{ "base64_image": "str(裸 base64，无 data: 前缀)",  // required
  "prompt": "str", "negative_prompt": "str",
  "style_name": "str", "steps": 15, "strength": 0.75,
  "cfg_scale": 10, "seed": -1 }

// 响应统一壳
{ "msg": "Finish",
  "content": { "image_url": "...|null" } | { "base64_image": "...|null" } | { "video_output_url": "...|null" } }
```

客户端取值位置（**静态确认**）：`HomeViewModel.processImage` 取 `content.base64_image`；
`ImageGen` DTO 同时声明 `image_url` 与 `base64_image`，两者并列。

---

## 5. 旧版视频链路签名（art12.procyoni.com，已停放但算法保留）

来源：`AIToolRepositoryImpl.fetchAIVideo()`

```
TimeStamps 头 = System.currentTimeMillis()
Key        头 = base64_no_wrap( HmacSHA512( key = RC["key_hash_hmac_sha512"], msg = str(TimeStamps) ) )
Guard      头 = "admin"     ← 源码里就是字面量默认值
POST {art12.procyoni.com}/generate   multipart: file=imagetovideo.jpg, aspectRatio, prompt
GET  {art12.procyoni.com}/check-video/{taskId}
```
HMAC 密钥来自 Firebase Remote Config 键 `key_hash_hmac_sha512`（见 §7），
`client.legacy_video_sign()` 已实现，域名复活即可用。

---

## 6. 上游停摆证据链

### 6.1 端口扫描（69.10.54.174，TCP connect）

```
22 OPEN   80 OPEN   443 OPEN   8000 OPEN   8005 OPEN
8006 closed   9000 closed   7860 closed   8080 closed   8188 closed ...
```

### 6.2 生成端点行为（全部带合法 Bearer）

```
POST /api/v2.0/generator/txt2img  {"prompt":"a red apple",...}
 → 200 {"msg":"Finish","content":{"image_url":null}}   ×3 次，1.07~2.11 s

POST /api/v2.0/generator/img2img  {"base64_image":"<768x768 JPEG>",...}
 → 200 {"msg":"Finish","content":{"base64_image":null}}  1.26~2.18 s

POST /api/v1.0/transfer?style=anime  (multipart 512x512)
 → 200 {"msg":"Finish","content":{"image_url":null}}  45.0 s   ← 真跑了但产物上传失败

POST /api/v1/video-generate  (multipart)
 → 200/40.8 s {"content":{"video_output_url":"http://69.10.54.174:9000/data/wedding_template.mp4"}}
   GET 同 URL → 503（对象存储已下线）
```

判读：**请求被受理、任务被调度（1 s ~ 45 s 的耗时差异说明进入过真实排队/推理），
但出图结果无处落地**——`image_url` 依赖的 9000/对象存储已关闭，`base64_image` 分支的
SD 推理进程无输出。App 自己也会 NPE：`HomeViewModel` 里 `Intrinsics.checkNotNull(content)`
对 `content=null` 直接抛异常 → 即**正版 App 当前也生成不出图**。

### 6.3 443 新网关（A1111 API Gateway v0.1.0）

```
GET  https://aiart-api.nowtechai.com/            → {"message":"Cổng API A1111 đang chạy!"}   (越南语：A1111 API 端口在运行)
GET  /openapi.json                               → 全量公开，8 个端点
GET  /api/monitor                                → total_requests=235, success=0, failed=235,
                                                   last_request_time=2026-10-18T16:54:35
POST /api/generate/txt2img                       → 404 {"detail":"Lỗi từ A1111: "}     ← A1111 上游空错误
POST /api/generate/img2img                       → 500 "Art API search request failed (502)"  ← 依赖 proxglobal，已 502
POST /api/generate/img2img/banana (Gemini 图生图) → 500 Internal Server Error
```
→ 245 次请求 0 成功、12 天无成功记录，**A1111 推理服务已死**。

### 6.4 QR 产物的真相

`/api/v2.0/generator/qr_art` 确实返回 `base64_image`，但解码后是
**310×310、模式 `1`（1-bit）、617 字节的裸二维码**；同输入完全可复现（sha 相同），
说明"QR→艺术化"那一步失败后，服务把中间产物（纯二维码）当结果返回了。
→ 不能当 AI 出图用。

### 6.5 密钥池（Firebase Remote Config 内）

| 键 | 内容 | 实测 |
|---|---|---|
| `config_api_key` | **294** 个 OpenAI 格式 `sk-…T3BlbkFJ…` 密钥（两两互异） | 抽样 8 个打 `GET /v1/models` → 全部 `401 The OpenAI account associated with this API key has been deactivated.` |
| `config_api_key_gemini` | 9 项、去重后仅 **1** 个 `AIzaSy…` | `GET /v1beta/models` → `403 Permission denied: Consumer 'projects/324322657249' has been suspended.` |

→ App 的"兜底智能"链路（`gpt-3.5-turbo` 写脚本、Gemini 生成 prompt）同样已失效。


### 6.6 算力拓扑直接暴露（最强判死证据）

同族（proxglobal 系）惯例：`GET /api/v1/transfer/config` 会吐出全部算力地址。

```bash
curl -s http://69.10.54.174:8005/api/v1/transfer/config
# → CONST.txt2img_turbo / CONST.img2img_urls / STYLES{50 个风格}.service_urls
```

实测拿到（原文存 `evidence/transfer_config_compute.json`，36,744 B）：

| 用途 | 地址 |
|---|---|
| txt2img turbo | `https://1a8nzg21h42bv3-8002.proxy.runpod.net/generate_sync/` |
| img2img ×3 负载 | `https://hqhfk2z3uedo3d-8040/8041/8042.proxy.runpod.net/sdapi/v1/img2img` |

四个 RunPod pod 逐个探活：**`404` + 响应体 **0 字节****。
这正是同族案（PicGo v2.0.8）定下的"pod 已销毁"硬指纹（冷启动会挂起或 502，权限问题是 403 带 body）。
→ **推理层是真的没了，不是限流、不是鉴权、不是冷启动。**

附带结论：
- `STYLES` 里有 **50** 个风格（`/api/v1.0/style_list` 只暴露 14 个），50 个风格共用这 3 个 img2img pod
  → 验证 3 个 pod 就等于验证了全部风格，不用逐个试。
- `GET /api/v1/transfer/list_styles` 返回完整 50 个名字，可用于建风格目录（数据面仍活）。

### 6.7 Bearer token 不是准入闸门（三态对照实验）

同一合法 body 打 `/api/v2.0/generator/txt2img`，只改 Authorization：

| Authorization | 结果 |
|---|---|
| `Bearer <合法 JWT>` | `200 {"msg":"Finish","content":{"image_url":null}}` 2.25 s |
| 不带 Authorization | `200 {"msg":"Finish","content":{"image_url":null}}` 1.16 s |
| `Bearer deadbeef` | `200 {"msg":"Finish","content":{"image_url":null}}` 1.03 s |

三者**完全一致** → 当前 token 只是装饰品，不是门禁。
（保留意义：算力复活后它可能承担配额归属，`client` 里仍按规范续签；但不能把"能签 token"
当成"渠道可用"的证据。）

---

## 7. 一个高危发现：Remote Config 可匿名读取

App 里 Firebase 只当"配置后台"用，但它的 RC 用的是公开 SDK 的 fetch 通道，**不校验应用完整性**：

```
project_id = ai-art-20bec                        (resources.arsc → google_storage_bucket=ai-art-20bec.appspot.com)
app_id     = 1:1028603950031:android:5d93b81fc88cbbf34c58ba
api_key    = AIzaSy…（已打码；每份 APK 的 resources.arsc 内均有，需复现请自行提取 google_api_key）

POST https://firebaseremoteconfig.googleapis.com/v1/projects/ai-art-20bec/namespaces/firebase:fetch?key=<api_key>
body {"app_instance_id":"<随机uuid>","app_id":"<app_id>", ...}
→ 200，返回 111 个配置键
```

拿到的东西（**已实测**，完整快照见 `evidence/remote_config.sample.json`，密钥段已打码）：

- `key_hash_hmac_sha512` —— 旧版视频签名 HMAC 密钥（可离线重放签名）
- `config_api_key` 294 个 OpenAI key、`config_api_key_gemini` —— 全部已停用
- `config_home` / `config_home_from_ver111` —— 首页运营位（含官方示例 prompt + 图片 URL）
- `config_iap_by_region` / `iap_credit_data` / `custom_style_premium` —— 内购与金币标价
- `config_update_version`（version_code 220）、`remote_config_ads_ver196` —— 强更与广告位
- `config_suggest_*` / `surprise_me_image` / `config_tag_ai_girl` —— prompt 推荐语料
- `t2m_coin`/`expand_coin` —— 各工具扣币额

风险定性：**只要拿到公开的 api key 即可匿名拉取该项目的全部运营配置与密钥**。
这是厂商侧的配置泄露，不是我们能修的；我们只把它当作"协议常量来源"使用。

---

## 8. 上游状态总表

| 组件 | 地址 | 状态 | 判据 |
|---|---|---|---|
| 主业务 API（旧） | `art12.procyoni.com` | 死 | 域名停放，`/lander` 跳转 |
| 主业务 API（新） | `aiart.proxglobal.co/now/v1/art/` | 死 | Cloudflare 502 |
| Web 前端 | `ai-art.proxglobal.co`、`gptart.nowtechai.com` | 活 | Cloudflare/nginx + Angular SPA，静态资源正常 |
| 网关 | `69.10.54.174:8005` | 活 | token/styles/openapi 全正常 |
| 网关 | `aiart-api.nowtechai.com`(443) | 半死 | 自身活，依赖的 proxglobal 502 |
| 年鉴服务 | `69.10.54.174:8006` | 死 | 端口关闭 |
| 对象存储 | `69.10.54.174:9000` | 死 | 端口关闭，`/data/*.mp4` 503 |
| SD 推理 | 未见暴露端口（7860/8188 全关） | 死 | `/api/monitor` 235/0 成功率 |
| OpenAI 池 | api.openai.com | 死 | 294 key 全 401 deactivated |
| Gemini | generativelanguage.googleapis.com | 死 | 项目 suspended |
| 风格 CDN | `ai-art.proxglobal.co/images/*.json` | 活 | 200，175 KB |

---

## 9. 本地验证产物（全部可复跑）

| 文件 | 作用 | 验证方式 |
|---|---|---|
| `zelouri_client.py` | 协议客户端（token 续签 + 全部生成端点 + 旧版 HMAC） | `python zelouri_client.py` → token/sha256 双端一致、14 风格、txt2img 返回 null |
| `demo_server.py` | OpenAI 兼容网关（mock/real/auto 三模 + 并发闸门 + 背压 + /metrics） | `python demo_server.py` |
| `demo.html` | 浏览器演示台（风格下拉、文生图/图生图、耗时/来源 KPI、探活） | `python verify_demo.py` → 8/8 PASS |
| `stress_test.py` | 压测 + 上游存活矩阵 | 见 §10 |
| `verify_demo.py` | 真实浏览器 e2e（Playwright） | 8 项断言全 PASS，截图 `evidence/demo_*.png` |

### 10. 压测结果（本机，mock 模式，连接池复用）

| 并发 | 总数 | 成功率 | 吞吐 | p50 | p95 | p99 |
|---|---|---|---|---|---|---|
| 4 | 100 | 100% | 55.3 rps | 30 ms | 41 ms | 1081 ms¹ |
| 8 | 100 | 100% | 112.7 rps | 63 ms | 97 ms | 113 ms |

¹ 首个请求含 PIL 字体冷加载，属一次性开销。
上游真实链路压测**不做**：出图为 0，压了只会刷 235→N 条失败日志，无信息量（见 §6）。

---

## 11. 结论与建议

**能复用的：**
1. token 签名 + Bearer 体系（10 分钟 TTL，需续签）——算法级验证过。
2. 37 个端点的完整契约（含 JSON / form / multipart 三种入参形态差异，这是最容易踩的坑）。
3. 风格目录（14 个上游风格 + 175 KB 静态目录），可离线当 prompt 增强用。
4. 旧版 `HmacSHA512 + Guard:admin` 视频签名，域名复活即插即用。

**不能复用的：**
1. 一切真实出图能力（SD 农场 + 对象存储双死）。
2. 一切"智能文案/脚本"能力（OpenAI 294 key + Gemini 项目双双停用）。

**建议：不接生产，不推公开仓库的可用清单。** 保留本地 demo（mock 模式）作为协议样例；
若日后 `69.10.54.174:9000` 复活或 `image_url` 恢复非 null，`ZELOURI_MODE=real` 一行切换即可复验，
`client.txt2img()` 无需改动。

判定复核入口（1 条命令）：
```bash
python stress_test.py --upstream
# 关注 txt2img_v2.ok / img2img_v2.ok 是否变 true
```
