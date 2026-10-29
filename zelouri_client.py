"""
AI Zelouri 1.9.6 (com.aiart.aigenerator.aifilter.aieditor) — upstream protocol client.

反编译还原的完整上游协议封装。所有常量均来自 APK 静态还原 + 线上实测验证：

  签名常量  serverapp@proxglobal%23%^?!154625
            来源：HomeViewModel.processImage$1.invokeSuspend
            用法：hash = SHA256(SECRET + content)，content = "TokenImg2Img" + epoch_ms

  网关      http://69.10.54.174:8005            (ServiceGenerator retrofitGenImage，线上存活)
            https://aiart-api.nowtechai.com     (同一台机器，仅 443 上的新网关，协议不同)
            http://69.10.54.174:8006            (retrofitGenYearBook，已下线)
            http://art12.procyoni.com           (retrofit 默认 base，已停放 park)

注意：截至 2026-10-30 实测，SD 生成算力已下线，生成端点返回
`{"msg":"Finish","content":{"image_url":null}}`。本客户端保留真实链路实现，
用于上游恢复后零改动复用；R1 演示走 mock 模式。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

# ── 协议常量（静态还原，勿改） ────────────────────────────────────────────────
TOKEN_SECRET = "serverapp@proxglobal%23%^?!154625"
TOKEN_PREFIX = "TokenImg2Img"

BASE_8005 = "http://69.10.54.174:8005"
BASE_8006 = "http://69.10.54.174:8006"
BASE_EDGE = "https://aiart-api.nowtechai.com"
BASE_LEGACY = "http://art12.procyoni.com"
BASE_STATIC = "https://ai-art.proxglobal.co"

PACKAGE = "com.aiart.aigenerator.aifilter.aieditor"
APP_VERSION = "1.9.6"

# 上游签发的 JWT 实测有效期很短（exp ≈ 签发 + 12~15 分钟），
# 因此不按固定值缓存，而是解析 JWT exp 并在到期前 60s 刷新（见 ZelouriClient.token）。
TOKEN_TTL_SAFE = 24 * 3600 - 600  # 仅在无法解析 exp 时的兜底


def jwt_exp(token: str) -> int | None:
    """取 JWT 的 exp（秒）。解析失败返回 None。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        import base64 as _b64

        return int(json.loads(_b64.urlsafe_b64decode(payload))["exp"])
    except Exception:  # noqa: BLE001
        return None


class UpstreamError(RuntimeError):
    """上游返回非 2xx 或业务失败。"""

    def __init__(self, msg: str, status: int | None = None, body: Any = None):
        super().__init__(msg)
        self.status = status
        self.body = body


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_token_payload(now_ms: int | None = None) -> dict[str, str]:
    """构造 /api/v2.0/token 请求体（等价于 APK 内 HomeViewModel 的写法）。"""
    content = TOKEN_PREFIX + str(now_ms if now_ms is not None else int(time.time() * 1000))
    return {"content": content, "hash": sha256_hex(TOKEN_SECRET + content)}


@dataclass
class StyleInfo:
    name: str
    prompt: str = ""
    negative_prompt: str = ""


@dataclass
class GenResult:
    """统一生成结果。真实链路下二者只会有其一非空。"""

    base64_image: str | None = None
    image_url: str | None = None
    video_url: str | None = None
    msg: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.base64_image or self.image_url or self.video_url)


class ZelouriClient:
    """线程安全的协议客户端：自动取/续 token，Bearer 鉴权，统一重试。"""

    def __init__(
        self,
        base_url: str = BASE_8005,
        timeout: float = 180.0,
        max_retries: int = 2,
        proxy: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._http = httpx.Client(
            timeout=timeout,
            verify=False,
            proxy=proxy,
            headers={"Accept": "application/json", "User-Agent": f"Zelouri/{APP_VERSION} (Android)"},
        )

    # ── token ────────────────────────────────────────────────────────────────
    def token(self, force: bool = False) -> str:
        with self._lock:
            fresh = self._token and (time.time() < self._token_exp - 60)
            if fresh and not force:
                return self._token  # type: ignore[return-value]
            body = make_token_payload()
            r = self._http.post(f"{self.base_url}/api/v2.0/token", json=body)
            if r.status_code != 200:
                raise UpstreamError(f"token HTTP {r.status_code}", r.status_code, r.text[:300])
            tok = r.json().get("access_token")
            if not tok:
                raise UpstreamError("token 响应缺少 access_token", r.status_code, r.text[:300])
            self._token = tok
            exp = jwt_exp(tok)
            self._token_exp = float(exp) if exp else time.time() + TOKEN_TTL_SAFE
            return tok

    def verify_hash_endpoint(self, content: str) -> str:
        """上游提供的哈希自证端点 GET /api/sha256?content=…（用于校验算法还原）。"""
        r = self._http.get(f"{self.base_url}/api/sha256", params={"content": content})
        r.raise_for_status()
        return r.json()["hash"]

    # ── 底层请求 ─────────────────────────────────────────────────────────────
    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token()}"}
        if extra:
            h.update(extra)
        return h

    def _post_json(self, path: str, payload: dict[str, Any], retry_401: bool = True) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.post(
                    f"{self.base_url}{path}", json=payload, headers=self._auth_headers()
                )
                if r.status_code == 401 and retry_401:
                    self.token(force=True)
                    retry_401 = False
                    continue
                if r.status_code >= 400:
                    raise UpstreamError(
                        f"{path} HTTP {r.status_code}", r.status_code, r.text[:400]
                    )
                return r.json()
            except (httpx.HTTPError, UpstreamError) as e:  # noqa: PERF203
                last = e
                if attempt < self.max_retries:
                    time.sleep(0.8 * (attempt + 1))
        raise UpstreamError(f"{path} 重试耗尽: {last}")

    def _post_multipart(
        self,
        path: str,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        params: dict[str, str] | None = None,
    ) -> Any:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self._http.post(
                    f"{self.base_url}{path}",
                    data=fields,
                    files=files,
                    params=params,
                    headers=self._auth_headers(),
                )
                if r.status_code >= 400:
                    raise UpstreamError(
                        f"{path} HTTP {r.status_code}", r.status_code, r.text[:400]
                    )
                return r.json()
            except (httpx.HTTPError, UpstreamError) as e:  # noqa: PERF203
                last = e
                if attempt < self.max_retries:
                    time.sleep(0.8 * (attempt + 1))
        raise UpstreamError(f"{path} 重试耗尽: {last}")

    # ── 元数据 ───────────────────────────────────────────────────────────────
    def style_list(self) -> dict[str, StyleInfo]:
        d = self._post_json_allow_get("/api/v1.0/style_list")
        content = d.get("content") or {}
        out: dict[str, StyleInfo] = {}
        for name in content.get("names", []):
            detail = (content.get("details") or {}).get(name) or {}
            out[name] = StyleInfo(
                name=name,
                prompt=detail.get("prompt", ""),
                negative_prompt=detail.get("negative_prompt", ""),
            )
        return out

    def _post_json_allow_get(self, path: str) -> Any:
        r = self._http.get(f"{self.base_url}{path}", headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise UpstreamError(f"{path} HTTP {r.status_code}", r.status_code, r.text[:300])
        return r.json()

    def static_style_catalog(self) -> Any:
        """App 首页风格目录（静态文件，独立于生成服务，实测存活）。"""
        r = self._http.get(f"{BASE_STATIC}/images/styles.json")
        r.raise_for_status()
        return r.json()

    def static_video_catalog(self) -> Any:
        r = self._http.get(f"{BASE_STATIC}/ai-video/data.json")
        r.raise_for_status()
        return r.json()

    # ── 生成 ─────────────────────────────────────────────────────────────────
    def txt2img(
        self,
        prompt: str,
        style_name: str = "",
        negative_prompt: str = "",
        steps: int = 20,
        cfg_scale: int = 7,
        seed: int = -1,
        width: int = 512,
        height: int = 512,
        api_version: str = "v2.0",
    ) -> GenResult:
        """POST /api/{v}/generator/txt2img  (InputTXT2IMGSchema, JSON)"""
        payload = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style_name": style_name,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "seed": seed,
            "width": width,
            "height": height,
        }
        t0 = time.time()
        d = self._post_json(f"/api/{api_version}/generator/txt2img", payload)
        content = d.get("content") or {}
        return GenResult(
            base64_image=content.get("base64_image"),
            image_url=content.get("image_url"),
            msg=d.get("msg", ""),
            raw=d,
            elapsed=time.time() - t0,
        )

    def img2img(
        self,
        image_bytes: bytes,
        prompt: str,
        style_name: str = "",
        negative_prompt: str = "",
        steps: int = 15,
        strength: float = 0.75,
        cfg_scale: int = 10,
        seed: int = -1,
        api_version: str = "v2.0",
    ) -> GenResult:
        """POST /api/{v}/generator/img2img  (InputTemplate, JSON + base64_image)"""
        b64 = base64.b64encode(image_bytes).decode()
        payload = {
            "base64_image": b64,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style_name": style_name,
            "steps": steps,
            "strength": strength,
            "cfg_scale": cfg_scale,
            "seed": seed,
        }
        t0 = time.time()
        d = self._post_json(f"/api/{api_version}/generator/img2img", payload)
        content = d.get("content") or {}
        return GenResult(
            base64_image=content.get("base64_image"),
            image_url=content.get("image_url"),
            msg=d.get("msg", ""),
            raw=d,
            elapsed=time.time() - t0,
        )

    def style_transfer(
        self,
        image_bytes: bytes,
        style: str,
        version: str = "v1.0",
        controlnet: bool = False,
        faceswap: bool = False,
        filename: str = "input.jpg",
    ) -> GenResult:
        """POST /api/{version}/transfer?style=<name>  (multipart file)"""
        t0 = time.time()
        d = self._post_multipart(
            f"/api/{version}/transfer",
            {"controlnet": str(controlnet).lower(), "faceswap": str(faceswap).lower()},
            {"file": (filename, image_bytes, "image/jpeg")},
            params={"style": style},
        )
        content = d.get("content") or {}
        return GenResult(
            base64_image=content.get("base64_image"),
            image_url=content.get("image_url"),
            msg=d.get("msg", ""),
            raw=d,
            elapsed=time.time() - t0,
        )

    def qr_art(self, text: str, art_prompt: str, api_version: str = "v2.0") -> GenResult:
        """POST /api/{v}/generator/qr_art → 二维码合成图（当前上游只回裸二维码）。"""
        t0 = time.time()
        d = self._post_json(
            f"/api/{api_version}/generator/qr_art",
            {"qr_text_data": text, "art_prompt": art_prompt},
        )
        content = d.get("content") or {}
        return GenResult(
            base64_image=content.get("base64_image"),
            msg=d.get("msg", ""),
            raw=d,
            elapsed=time.time() - t0,
        )

    def expand(self, image_bytes: bytes, ratio: str = "2:3") -> GenResult:
        """POST /api/v1/expand  (扩图 outpaint，multipart file + ratio)"""
        t0 = time.time()
        d = self._post_multipart(
            "/api/v1/expand",
            {"ratio": ratio},
            {"file": ("input.jpg", image_bytes, "image/jpeg")},
        )
        content = d.get("content") or {}
        return GenResult(
            base64_image=content.get("base64_image"),
            image_url=content.get("image_url"),
            msg=d.get("msg", ""),
            raw=d,
            elapsed=time.time() - t0,
        )

    def video_generate(self, image_bytes: bytes, prompt: str = "animated video") -> GenResult:
        """POST /api/v1/video-generate  (图生视频，multipart image)"""
        t0 = time.time()
        d = self._post_multipart(
            "/api/v1/video-generate",
            {"prompt": prompt},
            {"image": ("input.jpg", image_bytes, "image/jpeg")},
        )
        content = d.get("content") or {}
        return GenResult(
            video_url=content.get("video_output_url"),
            msg=d.get("msg", ""),
            raw=d,
            elapsed=time.time() - t0,
        )

    # ── 兼容旧版（art12.procyoni.com，已停放；保留协议备查） ───────────────────
    def legacy_video_sign(self, secret_key: str, ts_ms: int | None = None) -> dict[str, Any]:
        """AIToolRepositoryImpl.fetchAIVideo 的签名算法：
        Key = base64( HMAC-SHA512(key=RC key_hash_hmac_sha512, msg=str(epoch_ms)) )，NO_WRAP
        Guard 头默认字面量 "admin"（源码硬编码）。
        """
        ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
        import hmac

        mac = hmac.new(secret_key.encode(), str(ts).encode(), hashlib.sha512).digest()
        return {"TimeStamps": ts, "Key": base64.b64encode(mac).decode(), "Guard": "admin"}


def make_probe_image(size: int = 512, seed_text: str = "zelouri") -> bytes:
    """确定性测试图（无外部依赖 PIL 时的纯 PNG 兜底）。"""
    try:
        from PIL import Image, ImageDraw

        h = int(sha256_hex(seed_text)[:6], 16)
        im = Image.new("RGB", (size, size), ((h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF))
        d = ImageDraw.Draw(im)
        d.rectangle([size // 4, size // 4, size * 3 // 4, size * 3 // 4], outline=(255, 255, 255), width=4)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        return buf.getvalue()
    except Exception:  # pragma: no cover
        # 1x1 白色 PNG
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
        )


if __name__ == "__main__":  # 冒烟：token → styles → 生成
    c = ZelouriClient()
    tok = c.token()
    print("token:", tok[:24], "...")
    probe = "TokenImg2Img" + str(int(time.time() * 1000))
    print("server sha256:", c.verify_hash_endpoint(probe))
    print("local  sha256:", sha256_hex(TOKEN_SECRET + probe))
    styles = c.style_list()
    print("styles:", len(styles), list(styles)[:6])
    r = c.txt2img("a red apple on a wooden table", style_name="anime")
    print("txt2img:", r.msg, "ok=", r.ok, "elapsed=%.2fs" % r.elapsed, json.dumps(r.raw)[:200])
