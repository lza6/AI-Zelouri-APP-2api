"""
第一轮本地压测 + 上游存活矩阵。

用法（Windows，无需 .sh）：
  python stress_test.py                     # 默认打本机 :8951
  python stress_test.py --base http://127.0.0.1:8951 --total 120 --conc 8
  python stress_test.py --upstream          # 只跑上游存活矩阵（真实链路，不去重试刷量）

输出：p50 / p95 / p99、吞吐、成功率、状态码分布、上游矩阵。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

PROMPTS = [
    "a red apple on a rustic wooden table, soft morning light",
    "cyberpunk alley at night, neon reflections, rain",
    "portrait of an old fisherman, wrinkled face, dramatic light",
    "watercolor landscape with mountains and a lone cabin",
    "a cat wearing astronaut helmet, studio photography",
]


_CLIENT: httpx.Client | None = None


def _client(timeout: float, conc: int) -> httpx.Client:
    """复用连接池（trust_env=False 避开系统代理，Windows 上每次新建 client 会慢到秒级）。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            timeout=timeout,
            trust_env=False,
            limits=httpx.Limits(max_connections=conc + 8, max_keepalive_connections=conc + 8),
        )
    return _CLIENT


def one_call(base: str, timeout: float, size: str = "512x512", conc: int = 4) -> tuple[int, float, int, str]:
    t0 = time.perf_counter()
    try:
        c = _client(timeout, conc)
        if True:
            r = c.post(
                f"{base}/v1/images/generations",
                json={
                    "prompt": PROMPTS[int(time.time() * 1000) % len(PROMPTS)],
                    "n": 1,
                    "size": size,
                    "steps": 12,
                    "response_format": "b64_json",
                },
            )
            n = len(r.content)
            return r.status_code, time.perf_counter() - t0, n, ""
    except Exception as e:  # noqa: BLE001
        return 0, time.perf_counter() - t0, 0, f"{type(e).__name__}: {e}"


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100) * (len(s) - 1)))))
    return round(s[k], 3)


def run_load(base: str, total: int, conc: int, timeout: float) -> dict:
    print(f"\n=== 压测 {base} : total={total} concurrency={conc} ===")
    results: list[tuple[int, float, int, str]] = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = [ex.submit(one_call, base, timeout, "512x512", conc) for _ in range(total)]
        for f in as_completed(futs):
            results.append(f.result())
    wall = time.perf_counter() - t0

    ok = [r for r in results if r[0] == 200]
    lat = [r[1] for r in ok]
    codes: dict[str, int] = {}
    errs: dict[str, int] = {}
    for r in results:
        codes[str(r[0])] = codes.get(str(r[0]), 0) + 1
        if r[3]:
            key = r[3][:90]
            errs[key] = errs.get(key, 0) + 1

    out = {
        "total": total,
        "concurrency": conc,
        "wall_s": round(wall, 2),
        "throughput_rps": round(len(results) / wall, 2),
        "success": len(ok),
        "success_rate": round(len(ok) / len(results) * 100, 2),
        "codes": codes,
        "client_errors": errs,
        "latency_s": {
            "min": round(min(lat), 3) if lat else 0,
            "p50": percentile(lat, 50),
            "p95": percentile(lat, 95),
            "p99": percentile(lat, 99),
            "max": round(max(lat), 3) if lat else 0,
            "mean": round(statistics.fmean(lat), 3) if lat else 0,
        },
        "bytes_mean": int(statistics.fmean([r[2] for r in ok])) if ok else 0,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return out


def upstream_matrix() -> dict:
    """真实上游存活矩阵（只打一次，不刷量）。"""
    import zelouri_client as zc

    c = zc.ZelouriClient(timeout=60)
    res: dict = {}
    content = zc.TOKEN_PREFIX + str(int(time.time() * 1000))
    local = zc.sha256_hex(zc.TOKEN_SECRET + content)
    try:
        res["hash_formula"] = {"server": c.verify_hash_endpoint(content), "local": local,
                               "match": c.verify_hash_endpoint(content) == local}
    except Exception as e:  # noqa: BLE001
        res["hash_formula"] = f"ERR {e}"
    try:
        res["token"] = "ok " + c.token()[:16] + "…"
    except Exception as e:  # noqa: BLE001
        res["token"] = f"ERR {e}"
    checks = {
        "txt2img_v2": lambda: c.txt2img("probe", style_name="anime", steps=8),
        "img2img_v2": lambda: c.img2img(zc.make_probe_image(), "probe"),
        "qr_art_v2": lambda: c.qr_art("https://example.com", "forest"),
        "style_list": lambda: c.style_list(),
        "static_styles": lambda: c.static_style_catalog(),
        "static_video": lambda: c.static_video_catalog(),
    }
    for k, fn in checks.items():
        t = time.perf_counter()
        try:
            r = fn()
            el = round(time.perf_counter() - t, 2)
            if isinstance(r, dict):
                res[k] = {"ok": True, "count": len(r), "elapsed": el}
            elif hasattr(r, "msg"):
                res[k] = {"msg": r.msg, "ok": r.ok, "elapsed": round(r.elapsed, 2)}
            else:
                res[k] = {"ok": True, "elapsed": el}
        except Exception as e:  # noqa: BLE001
            res[k] = f"ERR {type(e).__name__}: {e}"
    print("\n=== 上游存活矩阵 ===")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8951")
    ap.add_argument("--total", type=int, default=60)
    ap.add_argument("--conc", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--upstream", action="store_true")
    ap.add_argument("--out", default="evidence/stress_report.json")
    args = ap.parse_args()

    report: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "base": args.base}
    if args.upstream:
        report["upstream"] = upstream_matrix()
    else:
        report["load"] = [
            run_load(args.base, args.total, args.conc, args.timeout),
            run_load(args.base, args.total, max(1, args.conc * 2), args.timeout),
        ]
    import os

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n报告已写入 {args.out}")


if __name__ == "__main__":
    main()
