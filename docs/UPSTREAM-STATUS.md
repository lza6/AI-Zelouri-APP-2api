# 上游存活台账（AI Zelouri / Prox Global）

每一次复核都往这里追一行，**只记事实和证据**。这是"要不要接生产"的唯一判据。

## 复核命令

```powershell
cd 2api
python stress_test.py --upstream          # 输出直接贴到下面表格
```

判据只看两个字段：`txt2img_v2.ok` 与 `img2img_v2.ok`。
任一为 `true` 且 `content` 非 null → 上游复活，可切 `ZELOURI_MODE=real`。
`qr_art_v2.ok=true` **不算复活**（它返回的是裸二维码，不是 AI 出图）。

---

## 台账

| 时间 | token | 哈希自证 | txt2img_v2 | img2img_v2 | style_list | 静态目录 | 结论 |
|---|---|---|---|---|---|---|---|
| 2026-10-30 05:2x | ok | match=true | `200 Finish / content=null` (2.11s) | `200 Finish / content=null` (1.26s) | 14 | ok (175KB / 11.6KB) | 算力停摆 |
| 2026-10-30 06:07 | ok | match=true | `200 Finish / content=null` (2.11s) | `200 Finish / content=null` (1.56s) | 14 | ok | 算力停摆（无变化） |

旁证（同期一次性确认，之后不必每次重测）：

| 组件 | 观测 | 时间 |
|---|---|---|
| `69.10.54.174:9000`（结果存储） | TCP 连接被拒 / `GET /data/*.mp4` → 503 | 2026-10-30 |
| `69.10.54.174:8006`（年鉴） | TCP 连接被拒 | 2026-10-30 |
| `art12.procyoni.com` | 停放页 `/lander` 跳转 | 2026-10-30 |
| `aiart.proxglobal.co/now/v1/art/` | Cloudflare 502 | 2026-10-30 |
| `aiart-api.nowtechai.com/api/monitor` | `total=235, success=0`，`last_success_time=null` | 2026-10-30 |
| OpenAI 池（RC `config_api_key`，294 个） | 抽样 8 个 → `401 account deactivated` | 2026-10-30 |
| Gemini（RC `config_api_key_gemini`） | `403 projects/324322657249 suspended` | 2026-10-30 |

---

## 复活时的动作清单（照做即可，0 改代码）

1. `python stress_test.py --upstream` 确认 `txt2img_v2.ok=true` 且 `content` 非 null。
2. `$env:ZELOURI_MODE="real"` 重启 `demo_server.py`，抽 3 张验证真实出图（本地留样）。
3. 若走 `image_url` 分支，先单独 `curl` 该 URL 确认可下载（对象存储是单点故障）。
4. 通过后再做 new-api 渠道（`docs/NEW-API-HANDOFF.md` 路径 B）。
5. 在本表追加一行"复活"记录。

> ⚠️ 绝不要在 `ok=false` 时把渠道接进 new-api：网关会如实返回 502，但上游日志会被空跑打满，
> 且容易误把 `msg:"Finish"` 当成成功计入调用量。
