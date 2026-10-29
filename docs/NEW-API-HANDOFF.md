# AI Zelouri 1.9.6 → new-api 对接交接书

> 面向执行会话：读完本文即可动手，不需要回看逆向过程。
> 配套仓库目录：`2api/`（`zelouri_client.py` / `demo_server.py` / `demo.html` / `stress_test.py` / `verify_demo.py` / `docs/REVERSE-REPORT.md`）
>
> **先读这一句**：截至 2026-10-30，上游真实出图能力为 **0**（详见 REPORT §6）。
> 本文给的是"上游一恢复就能立刻接上"的完整方案 + 当前可用于联调的 mock 通道。

---

## 0. 上游事实（已实测，可直接依赖）

| 项 | 值 |
|---|---|
| 网关 | `http://69.10.54.174:8005`（DNS：`aiart-api.nowtechai.com` 同机；443 是另一套网关，别用） |
| 鉴权 | 两层：`content = "TokenImg2Img"+epoch_ms`，`hash = sha256("serverapp@proxglobal%23%^?!154625"+content)` → `POST /api/v2.0/token` 换 JWT；之后 `Authorization: Bearer <jwt>` |
| JWT | HS256，payload `{unique, app:"prox_app", exp}`，**TTL 600 s**，必须续签 |
| 无需 | 账号 / 设备指纹 / 证书固定 / 签名头 / App 完整性校验 |
| 文生图 | `POST /api/v2.0/generator/txt2img`，JSON `InputTXT2IMGSchema`（`prompt`+`negative_prompt` 必填） |
| 图生图 | `POST /api/v2.0/generator/img2img`，JSON `InputTemplate`（`base64_image` 裸 base64，不带 `data:` 前缀） |
| 风格迁移 | `POST /api/{v1.0\|v2.0\|v3}/transfer?style=<name>`，**multipart**，字段名 `file` |
| 风格表 | `GET /api/v1.0/style_list` → 14 个风格，每个带官方 prompt / negative_prompt |
| 响应壳 | `{"msg":"Finish","content":{"image_url"\|"base64_image"\|"video_output_url": ...}}` |
| 规格上限 | `steps` 1–50，`width/height` 512/768 档位，`n` 建议 ≤4 |
| 当前产物 | **恒为 null**（SD 推理 + 对象存储 9000 端口双死） |
| 自证端点 | `GET /api/sha256?content=…` 返回服务端哈希，可随时校验算法是否被改 |

---

## 1. 两条对接路径

### 路径 A：零 Go 代码 —— 把 `demo_server.py` 当中转上游（30 分钟）

1. 起中转（建议与 new-api 同机 / 内网可达）：
   ```powershell
   cd 2api
   $env:ZELOURI_MODE="auto"      # real=只走真上游；auto=真上游无产物则回落 mock 并在 Header 标注
   $env:ZELOURI_PORT="8951"
   $env:ZELOURI_MAX_CONCURRENCY="4"
   Start-Process -WindowStyle Hidden python demo_server.py
   ```
2. new-api 建渠道：**类型 = OpenAI**，`Base URL = http://<内网IP>:8951`，模型挂：
   - `zelouri-art-v1`（默认，走 txt2img）
   - `zelouri-img2img-v2`（走 /v1/images/edits）
3. 客户端调用：
   ```bash
   curl -X POST http://<new-api>/v1/images/generations \
     -H "Authorization: Bearer <new-api-token>" -H 'Content-Type: application/json' \
     -d '{"model":"zelouri-art-v1","prompt":"a red fox in snow","size":"512x512","n":1}'
   ```

   **必做**：
   - 渠道**超时 ≥ 180 s**（真上游单张可达 45 s+）
   - `n ≤ 4`（中转层已 clamp）
   - 中转常驻（`Start-Process -WindowStyle Hidden`；bash 后台会随会话被杀）
   - 响应头 `X-Zelouri-Source: real|mock` 必须回传/记录，**mock 结果不得当真实产出计费**

**优点**：不动 Go、不动前端、不碰计费表。**缺点**：多一跳；计费按 OpenAI 图像默认口径，不是 2API 2 币口径。

### 路径 B：原生 Go 渠道（仅在上游复活后再做）

当前**不建议**：出图为 0 时上渠道只会把余额打成空转日志。上游恢复后用下面清单一次性接好。

#### B.1 落点清单

| 文件 | 改动 |
|---|---|
| `constant/channel.go` | 新增 `ChannelTypeZelouri`（取当前最大值 +1）+ `ChannelTypeNames` 加名称 |
| `constant/api_type.go` | 新增 `APITypeZelouri`（与 channel type 同序） |
| `relay/relay_adaptor.go` | `GetAdaptor` 加 `case constant.APITypeZelouri: return &zelouri.Adaptor{}` |
| `relay/channel/zelouri/adaptor.go` | `GetRequestURL` / `SetupRequestHeader` / `ConvertImageRequest` / `DoResponse` / `GetModelList` / `GetChannelName` |
| `relay/channel/zelouri/client.go` | 直译 `zelouri_client.py`：token 续签（600 s）+ txt2img/img2img |
| `service/billing.go` | `isTwoAPIBillingChannel` 列表加入新 channel（**漏这步 → 2 币不扣，走常规余额**） |
| `web/src/features/channels/constants.ts` | 渠道类型标签 |
| `web/src/features/pricing/components/twoapi-badge.tsx` | 2API 徽章白名单 |

模板参考：`relay/channel/imagerator/`（同为「同步 relay 出 media url」的 2API 渠道）。

#### B.2 关键实现骨架

```go
package zelouri

const (
    baseURL     = "http://69.10.54.174:8005"
    tokenSecret = "serverapp@proxglobal%23%^?!154625"
    tokenPrefix = "TokenImg2Img"
)

func tokenBody() (string, map[string]string) {
    content := tokenPrefix + strconv.FormatInt(time.Now().UnixMilli(), 10)
    sum := sha256.Sum256([]byte(tokenSecret + content))
    body, _ := json.Marshal(map[string]string{
        "content": content,
        "hash":    hex.EncodeToString(sum[:]),
    })
    return string(body), nil
}

// 续签：解析 JWT exp，剩余 <60s 重新取；并发下用 singleflight 合并
func (c *Client) Token() (string, error) { /* POST /api/v2.0/token → access_token */ }

// 文生图
// POST /api/v2.0/generator/txt2img
// body: {"prompt","negative_prompt","style_name","steps","cfg_scale","seed","width","height"}
// resp: {"msg","content":{"image_url":string|null}}
func (c *Client) Txt2Img(prompt, style string, steps, w, h int) (string, error)
```

#### B.3 出图产物怎么进 new-api

上游成功时会给 `image_url`（对象存储）或 `base64_image`（部分端点）。
- `image_url` 非空 → 直接作为 media url 回给 new-api。
- `base64_image` 非空 → 中转落盘成 `/files/<uuid>.png` 再回 url（`demo_server.py` 已实现该分支）。
- 两者皆 null → **必须报错**（`502 上游返回空产物`），不能静默返回空数组，否则前端白屏且计费口径会错乱。

---

## 2. 计费与配额建议

| 场景 | 建议 |
|---|---|
| 上游 200 但产物 null | **不计费**，返回 502 + 明确错误码 `ZELOURI_EMPTY_OUTPUT` |
| `X-Zelouri-Source: mock` | **不计费**（或将 ModelRatio 设为 0），仅用于联调 |
| 真出图 | 按 2API 2 币口径，`t2m_coin=15`（上游自家标价，见 RC `t2m_coin`），建议 ModelRatio 对齐 |
| 频控 | 中转默认 `MAX_CONCURRENCY=4`、队列 30 s 超时 → 429 背压；上游 token 600 s 复用，避免 token 端点被打爆 |

---

## 3. 验证四步（按顺序，任一步失败即停）

```powershell
cd D:\apk-analysis\zelouri196\2api

# ① 协议层：token + 哈希自证 + 风格表 + 生成返回
python zelouri_client.py
#   期望：server/local sha256 完全相同；styles: 14；txt2img ok= False（上游空产物）

# ② 上游存活矩阵
python stress_test.py --upstream
#   期望：hash_formula.match=true, token=ok, style_list=14；
#         txt2img_v2.ok / img2img_v2.ok = false（上游恢复后会变 true，这条就是复核开关）

# ③ 本地网关 + 压测
$env:ZELOURI_MODE="mock"; Start-Process -WindowStyle Hidden python demo_server.py
python stress_test.py --base http://127.0.0.1:8951 --total 100 --conc 4
#   期望：success_rate=100%，p50 < 100ms

# ④ 浏览器真实路径
python verify_demo.py --url http://127.0.0.1:8951/demo
#   期望：8 项断言全 PASS，截图落在 evidence/demo_*.png
```

---

## 4. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `422 {"loc":["body","negative_prompt"],"msg":"field required"}` | txt2img 的 `negative_prompt` 是必填（哪怕空串） | 传 `""` |
| `422 {"loc":["body"],"msg":"value is not a valid dict"}` | 该端点要 JSON，你发了 form | v1.0/v2.0 generator 系用 JSON；`/api/v1.0/aiart/txt2img`、`/api/v3/txt2img*`、`transfer` 系才用 form/multipart |
| `422 {"loc":["query","style"],"msg":"field required"}` | transfer 的 style 走 **query**，不是 body | `?style=anime`（名字必须是 `style_list` 里的） |
| `202 {"detail":"Error!"}` | 上游受理但推理失败（当前常态） | 判定上游死；不要重试刷量 |
| `200 {"msg":"Finish","content":{"image_url":null}}` | 推理成功但产物上传失败 / 无产物 | 同上 |
| `401` | JWT 过期（600 s） | 强制续签；客户端已内置 `force=True` 路径 |
| `502 {"detail":"..."}` on `aiart-api.nowtechai.com` | 443 网关依赖 proxglobal，已 502 | 用 8005，别用 443 |
| 图片 310×310 且只有黑白 | QR 端点返回的是裸二维码，不是艺术化结果 | 不要当 AI 出图 |
| `500 Stage 1 failed: API returned 404` | `/api/v1/expand` 依赖的下游没了 | 扩图不可用 |

---

## 5. 交接清单

- [x] token 算法（服务端自证一致）
- [x] 37 端点契约 + 入参形态差异表
- [x] 14 风格目录（含 prompt/negative）与 175 KB 静态目录
- [x] 旧版视频 HMAC 签名（`HmacSHA512` + `Guard:admin`）
- [x] Remote Config 全量键（111 个，密钥段已脱敏）
- [x] 上游停摆证据链（端口 / 235:0 成功率 / 45 s 空转 / 294 key 全停 / Gemini 项目停用）
- [x] 本地网关 + demo 页 + 压测 + 浏览器 e2e
- [ ] 真实出图（**阻塞，非我方问题**）
- [ ] new-api 渠道上线（建议等上游复活）

---

## 6. 需要用户拍板的事

见交付说明的「需你决策」一节：是否现在就以 **mock 通道**接进 new-api 做联调（不产生费用、不产生真实产出），还是等上游复活再一次性接真实链路。
