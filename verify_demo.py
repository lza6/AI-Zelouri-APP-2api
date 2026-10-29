"""
demo 页 e2e 验证（真实浏览器，不是静态断言）。

跑法：python verify_demo.py [--url http://127.0.0.1:8951/demo]
产出：evidence/demo_*.png 截图 + 控制台输出 PASS/FAIL。
"""

from __future__ import annotations

import argparse
import os
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8951/demo")
    ap.add_argument("--shots", default="evidence")
    args = ap.parse_args()
    os.makedirs(args.shots, exist_ok=True)

    fails: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
        if not cond:
            fails.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(args.url, wait_until="networkidle")

        check("标题渲染", "Zelouri" in page.title(), page.title())

        # 模式徽章
        badge = page.inner_text("#modeBadge")
        check("mode 徽章有值", "mode:" in badge and "…" not in badge, badge)

        # 风格下拉由 /api/styles 填充（上游 style_list 约 0.8s，必须等到选项就位）
        page.wait_for_function(
            "document.querySelectorAll('#style option').length > 2", timeout=60000
        )
        opts = page.eval_on_selector_all("#style option", "els => els.map(e => e.value)")
        check("风格下拉已填充", len(opts) >= 5, f"{len(opts)} options")

        page.screenshot(path=os.path.join(args.shots, "demo_1_initial.png"), full_page=True)

        # 点生成
        page.fill("#prompt", "a red apple on a rustic wooden table, soft morning light")
        page.select_option("#style", index=1)
        page.click("#go")
        page.wait_for_selector(".shot img", timeout=90000)
        n_imgs = page.eval_on_selector_all(".shot img", "els => els.length")
        src = page.inner_text("#kSrc")
        lat = page.inner_text("#kLat")
        check("生成出图", n_imgs >= 1, f"{n_imgs} imgs")
        check("来源已标注", src in ("real", "mock"), f"source={src} latency={lat}")

        # 图片真的解码成功（naturalWidth > 0）
        w = page.eval_on_selector(".shot img", "el => el.naturalWidth")
        check("图片像素可解码", isinstance(w, int) and w > 0, f"naturalWidth={w}")

        page.screenshot(path=os.path.join(args.shots, "demo_2_result.png"), full_page=True)

        # 探活按钮
        page.click("#probeBtn")
        page.wait_for_function("document.querySelector('#log').textContent.includes('探活结果')", timeout=120000)
        log_txt = page.inner_text("#log")
        check("探活返回 JSON", "hash_formula" in log_txt and "token" in log_txt)
        page.screenshot(path=os.path.join(args.shots, "demo_3_probe.png"), full_page=True)

        check("无 JS 运行时错误", not errors, "; ".join(errors[:3]))
        browser.close()

    print("\n结果:", "PASS" if not fails else f"FAIL ({len(fails)}): {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
