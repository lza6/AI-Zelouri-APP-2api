"""
AI Zelouri 1.9.6 → OpenAI 兼容中转（2API 网关）

三种上游模式：
  mock  纯本地渲染，不碰上游。压测/demo 用。
  real  只走上游，失败即失败（用于诚实探测上游状态）。
  auto  先 real，上游无产物则回落 mock，并在响应头 X-Zelouri-Source 标注来源。

启动：
  python demo_server.py                     # 默认 auto, :8951
  ZELOURI_MODE=mock ZELOURI_PORT=8951 python demo_server.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from zelouri_client import (
    APP_VERSION,
    PACKAGE,
    BASE_8005,
    ZelouriClient,
    UpstreamError,
    sha256_hex,
    TOKEN_SECRET,
    TOKEN_PREFIX,
)

MODE = os.environ.get("ZELOURI_MODE", "auto").lower()
PORT = int(os.environ.get("ZELOURI_PORT", "8951"))
MAX_CONCURRENCY = int(os.environ.get("ZELOURI_MAX_CONCURRENCY", "4"))
QUEUE_TIMEOUT = float(os.environ.get("ZELOURI_QUEUE_TIMEOUT", "30"))
UPSTREAM_TIMEOUT = float(os.environ.get("ZELOURI_UPSTREAM_TIMEOUT", "180"))
MODEL_ID = "zelouri-art-v1"
FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

client = ZelouriClient(base_url=BASE_8005, timeout=UPSTREAM_TIMEOUT)
_sema = asyncio.Semaphore(MAX_CONCURRENCY)

# ── 指标 ─────────────────────────────────────────────────────────────────────
STATS: dict[str, Any] = {
    "started_at": time.time(),
    "requests": 0,
    "ok": 0,
    "upstream_null": 0,
    "mock": 0,
    "real": 0,
    "errors": 0,
    "throttled": 0,
    "latencies": deque(maxlen=2000),
}


def _record(latency: float, bucket: str) -> None:
    STATS["requests"] += 1
    STATS["latencies"].append(latency)
    STATS[bucket] = STATS.get(bucket, 0) + 1


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return round(s[k], 3)


# ── mock 渲染器：确定性、可重复、有真实 PNG 字节 ─────────────────────────────
_PALETTES = [
    ((255, 107, 107), (255, 209, 102)),
    ((6, 214, 160), (17, 138, 178)),
    ((239, 71, 111), (58, 12, 163)),
    ((255, 183, 3), (33, 158, 188)),
    ((131, 56, 236), (58, 134, 255)),
]


def render_mock_png(prompt: str, style: str, size: str = "512x512") -> bytes:
    """用 PIL 画一张确定性图：渐变底 + 分形色块 + prompt 指纹。
    不是 AI 出图，仅用于把 demo/压测的链路跑通并产出真实像素。
    """
    from PIL import Image, ImageDraw, ImageFont

    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except Exception:
        w, h = 512, 512
    w, h = max(64, min(w, 1024)), max(64, min(h, 1024))

    seed = int(sha256_hex(f"{prompt}|{style}")[:12], 16)
    base, accent = _PALETTES[seed % len(_PALETTES)]

    # 竖向渐变：2px 宽的渐变条再放大，避免逐像素 Python 循环（<10ms）
    strip = Image.new("RGB", (2, h))
    spx = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        col = (
            int(base[0] * (1 - t) + accent[0] * t),
            int(base[1] * (1 - t) + accent[1] * t),
            int(base[2] * (1 - t) + accent[2] * t),
        )
        spx[0, y] = col
        spx[1, y] = col
    im = strip.resize((w, h), Image.Resampling.BILINEAR)

    d = ImageDraw.Draw(im, "RGBA")
    s = seed
    for _ in range(9):
        s = (s * 6364136223846793005 + 1442695040888963407) & ((1 << 64) - 1)
        cx, cy = s % w, (s >> 17) % h
        rad = 24 + (s >> 33) % (min(w, h) // 3)
        col = ((s >> 8) & 0xFF, (s >> 24) & 0xFF, (s >> 40) & 0xFF, 70)
        d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=col)

    d.rectangle([0, h - 74, w, h], fill=(0, 0, 0, 130))
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
    cap = (prompt[:46] + "…") if len(prompt) > 46 else prompt
    d.text((12, h - 58), f"[MOCK] {cap}", fill=(255, 255, 255), font=font)
    d.text((12, h - 32), f"style={style or 'none'}  {w}x{h}  sha={sha256_hex(prompt)[:10]}",
           fill=(210, 210, 210), font=font)

    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _save(bytes_: bytes) -> str:
    os.makedirs(FILES_DIR, exist_ok=True)
    name = f"{uuid.uuid4().hex}.png"
    with open(os.path.join(FILES_DIR, name), "wb") as f:
        f.write(bytes_)
    return name


# ── FastAPI ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(FILES_DIR, exist_ok=True)
    print(f"[zelouri] mode={MODE} port={PORT} upstream={BASE_8005} package={PACKAGE} v{APP_VERSION}")
    yield
    client._http.close()


app = FastAPI(title="AI Zelouri 2API Gateway", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class ImageGenRequest(BaseModel):
    prompt: str = Field(..., description="正向提示词")
    model: str = MODEL_ID
    n: int = 1
    size: str = "512x512"
    style: str | None = None            # 我们的扩展：上游 style_name
    negative_prompt: str = ""
    steps: int = 20
    cfg_scale: int = 7
    seed: int = -1
    strength: float = 0.75
    response_format: str = "b64_json"   # b64_json | url


def _openai_image(data: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"created": int(time.time()), "data": data}
    if extra:
        body.update(extra)
    return body


async def _run_real_txt2img(req: ImageGenRequest) -> list[bytes]:
    loop = asyncio.get_running_loop()
    out: list[bytes] = []
    for _ in range(max(1, min(req.n, 4))):
        r = await loop.run_in_executor(
            None,
            lambda: client.txt2img(
                req.prompt,
                style_name=req.style or "",
                negative_prompt=req.negative_prompt,
                steps=req.steps,
                cfg_scale=req.cfg_scale,
                seed=req.seed,
            ),
        )
        if r.base64_image:
            out.append(base64.b64decode(r.base64_image))
    return out


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "mode": MODE,
        "in_flight": MAX_CONCURRENCY - _sema._value,  # noqa: SLF001
        "max_concurrency": MAX_CONCURRENCY,
        "uptime_s": round(time.time() - STATS["started_at"], 1),
    }


@app.get("/metrics")
async def metrics():
    lat = list(STATS["latencies"])
    return {
        "requests": STATS["requests"],
        "ok": STATS["ok"],
        "real": STATS["real"],
        "mock": STATS["mock"],
        "upstream_null": STATS["upstream_null"],
        "errors": STATS["errors"],
        "throttled": STATS["throttled"],
        "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": round(max(lat), 3) if lat else 0},
        "uptime_s": round(time.time() - STATS["started_at"], 1),
    }


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_ID, "object": "model", "owned_by": "zelouri", "created": 1730000000},
            {"id": "zelouri-txt2img-v2", "object": "model", "owned_by": "zelouri", "created": 1730000000},
            {"id": "zelouri-img2img-v2", "object": "model", "owned_by": "zelouri", "created": 1730000000},
            {"id": "zelouri-transfer-v1", "object": "model", "owned_by": "zelouri", "created": 1730000000},
        ],
    }


@app.get("/api/styles")
async def styles():
    """线上风格目录（生成服务已死，但风格表仍在，落盘缓存兜底）。"""
    try:
        got = await asyncio.get_running_loop().run_in_executor(None, client.style_list)
        return {"count": len(got), "source": "upstream", "styles": [s.__dict__ for s in got.values()]}
    except Exception as e:  # noqa: BLE001
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence", "style_list_8005.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            content = d.get("content", {})
            out = [
                {"name": n, **content.get("details", {}).get(n, {})} for n in content.get("names", [])
            ]
            return {"count": len(out), "source": f"cache ({e.__class__.__name__})", "styles": out}
        raise HTTPException(503, f"style 目录不可用: {e}")


@app.post("/v1/images/generations")
async def images_generations(req: ImageGenRequest, response: Response):
    """OpenAI 兼容文生图。"""
    t0 = time.time()
    try:
        await asyncio.wait_for(_sema.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        STATS["throttled"] += 1
        raise HTTPException(429, "队列已满，稍后重试") from None

    try:
        imgs: list[bytes] = []
        source = "mock"
        if MODE in ("real", "auto"):
            try:
                imgs = await _run_real_txt2img(req)
            except UpstreamError as e:
                if MODE == "real":
                    STATS["errors"] += 1
                    raise HTTPException(502, f"上游失败: {e}") from e
        if not imgs:
            if MODE != "mock":
                STATS["upstream_null"] += 1
            if MODE == "real":
                STATS["errors"] += 1
                raise HTTPException(502, "上游返回空产物（生成算力未就绪）")
            imgs = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: [
                    render_mock_png(req.prompt, req.style or "", req.size)
                    for _ in range(max(1, min(req.n, 4)))
                ],
            )
        else:
            source = "real"
            STATS["real"] += 1

        if source == "mock":
            STATS["mock"] += 1

        data = []
        for b in imgs:
            if req.response_format == "url":
                data.append({"url": f"/files/{_save(b)}", "revised_prompt": req.prompt})
            else:
                data.append({"b64_json": base64.b64encode(b).decode(), "revised_prompt": req.prompt})
        response.headers["X-Zelouri-Source"] = source
        _record(time.time() - t0, "ok")
        return _openai_image(data, {"usage": {"images": len(imgs), "style": req.style or "", "source": source}})
    finally:
        _sema.release()


@app.post("/v1/images/edits")
async def images_edits(
    response: Response,
    image: UploadFile = File(...),
    prompt: str = Form(...),
    style: str = Form(""),
    n: int = Form(1),
    size: str = Form("512x512"),
    steps: int = Form(15),
    strength: float = Form(0.75),
):
    """OpenAI 兼容图生图（multipart）。"""
    t0 = time.time()
    raw = await image.read()
    try:
        await asyncio.wait_for(_sema.acquire(), timeout=QUEUE_TIMEOUT)
    except asyncio.TimeoutError:
        STATS["throttled"] += 1
        raise HTTPException(429, "队列已满，稍后重试") from None
    try:
        source, imgs = "mock", []
        if MODE in ("real", "auto"):
            try:
                r = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: client.img2img(
                        raw, prompt, style_name=style, steps=steps, strength=strength
                    ),
                )
                if r.base64_image:
                    imgs = [base64.b64decode(r.base64_image)]
            except UpstreamError as e:
                if MODE == "real":
                    STATS["errors"] += 1
                    raise HTTPException(502, f"上游失败: {e}") from e
        if not imgs:
            if MODE != "mock":
                STATS["upstream_null"] += 1
            if MODE == "real":
                STATS["errors"] += 1
                raise HTTPException(502, "上游返回空产物（生成算力未就绪）")
            imgs = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: [render_mock_png(prompt, style, size) for _ in range(max(1, min(n, 4)))],
            )
        else:
            source = "real"
            STATS["real"] += 1
        if source == "mock":
            STATS["mock"] += 1
        response.headers["X-Zelouri-Source"] = source
        _record(time.time() - t0, "ok")
        return _openai_image(
            [{"b64_json": base64.b64encode(b).decode(), "revised_prompt": prompt} for b in imgs],
            {"usage": {"images": len(imgs), "style": style, "source": source}},
        )
    finally:
        _sema.release()


@app.get("/api/upstream-probe")
async def upstream_probe():
    """一次性探活矩阵：token / 生成 / 风格 / 静态目录。真实反映上游状态。"""
    loop = asyncio.get_running_loop()

    def _probe() -> dict[str, Any]:
        out: dict[str, Any] = {}
        content = TOKEN_PREFIX + str(int(time.time() * 1000))
        out["hash_formula_local"] = sha256_hex(TOKEN_SECRET + content)
        try:
            out["hash_formula_server"] = client.verify_hash_endpoint(content)
            out["hash_match"] = out["hash_formula_local"] == out["hash_formula_server"]
        except Exception as e:  # noqa: BLE001
            out["hash_formula_server"] = f"ERR {e}"
        try:
            out["token"] = "ok " + client.token()[:18] + "…"
        except Exception as e:  # noqa: BLE001
            out["token"] = f"ERR {e}"
        for name, fn in (
            ("txt2img_v2", lambda: client.txt2img("probe", style_name="anime", steps=8)),
            ("img2img_v2", lambda: client.img2img(b"\xff\xd8\xff", "probe")),
            ("qr_art_v2", lambda: client.qr_art("https://example.com", "forest")),
        ):
            try:
                r = fn()
                out[name] = {
                    "msg": r.msg,
                    "ok": r.ok,
                    "elapsed": round(r.elapsed, 2),
                    "content_keys": list((r.raw.get("content") or {}).keys()),
                }
            except Exception as e:  # noqa: BLE001
                out[name] = f"ERR {e}"
        try:
            out["style_list"] = f"{len(client.style_list())} styles"
        except Exception as e:  # noqa: BLE001
            out["style_list"] = f"ERR {e}"
        try:
            cat = client.static_style_catalog()
            out["static_styles_json"] = f"ok code={cat.get('code')} groups={len(cat.get('data', []))}"
        except Exception as e:  # noqa: BLE001
            out["static_styles_json"] = f"ERR {e}"
        return out

    return await loop.run_in_executor(None, _probe)


@app.get("/files/{name}")
async def get_file(name: str):
    safe = os.path.basename(name)
    path = os.path.join(FILES_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    return FileResponse(path)


@app.get("/demo")
async def demo():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.html"))


@app.get("/")
async def root():
    return JSONResponse(
        {
            "name": "AI Zelouri 2API Gateway",
            "demo": "/demo",
            "models": "/v1/models",
            "health": "/healthz",
            "metrics": "/metrics",
            "probe": "/api/upstream-probe",
            "mode": MODE,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
