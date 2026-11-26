#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
央视 10 台直播源自动刷新（GitHub Actions 专用）

原理：央视直播的 HLS 地址带时效鉴权（auth_key，约 15 小时过期），
      只能在播放器解密后从网络请求里拿到。所以这里用无头浏览器打开
      每个频道的直播页，监听真实发出的 .m3u8 请求，取出带 auth_key 的地址，
      校验可播后重新生成 m3u。

用法：
    python scripts/cctv_live.py                # 默认输出 央视直播_10台.m3u
    python scripts/cctv_live.py --out x.m3u    # 指定输出文件
    python scripts/cctv_live.py --jobs 3       # 并发抓取数（默认 3）
    python scripts/cctv_live.py --timeout 40   # 单台等待秒数（默认 40）

环境变量：
    CHROME_PATH   指定浏览器可执行文件（不设则用 playwright 自带的 chromium）
    OUT           输出文件名（等价于 --out）
    JOBS          并发数
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from playwright.async_api import async_playwright
except ImportError:
    sys.exit("缺少依赖：pip install -r requirements.txt")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE = "https://m-live.cctvnews.cctv.com/live/landscape.html?liveRoomNumber={room}"

# (显示名, 台号, liveRoomNumber)
CHANNELS = [
    ("CCTV-1 综合", "1", "11200132825562653886"),
    ("CCTV-2 财经", "2", "12030532124776958103"),
    ("CCTV-4 中文国际", "4", "10620168294224708952"),
    ("CCTV-7 国防军事", "7", "8516529981177953694"),
    ("CCTV-9 纪录", "9", "7252237247689203957"),
    ("CCTV-10 科教", "10", "14589146016461298119"),
    ("CCTV-12 社会与法", "12", "13180385922471124325"),
    ("CCTV-13 新闻", "13", "16265686808730585228"),
    ("CCTV-17 农业农村", "17", "4496917190172866934"),
    ("CCTV-4I 超高清", "4I", "2127841942201075403"),
]

# 尽量让播放器自动起播，顺便把大播放按钮点掉
PLAY_JS = """() => {
  const v = document.querySelector('video');
  if (v) {
    try { v.muted = true; } catch (e) {}
    const p = v.play && v.play();
    if (p && p.catch) p.catch(() => {});
    return 1;
  }
  const b = document.querySelector(
    '.prism-big-play-btn,[class*=big-play],[class*=play-btn],[class*=play]');
  if (b) { try { b.click(); } catch (e) {} return 2; }
  return 0;
}"""


def log(msg):
    print(msg, flush=True)


def is_stream_url(u: str) -> bool:
    low = u.lower()
    return (
        ".m3u8" in low
        and "channel_cctv" in low
        and "auth_key=" in low
        and ".ts" not in low
    )


async def _grab_once(browser, room: str, timeout: int):
    """打开一个频道的直播页，抓它真实请求的 m3u8（带 auth_key）。"""
    ctx = await browser.new_context(
        locale="zh-CN",
        user_agent=UA,
        viewport={"width": 1280, "height": 720},
    )
    found = []
    try:
        page = await ctx.new_page()

        def on_request(req):
            try:
                if is_stream_url(req.url) and req.url not in found:
                    found.append(req.url)
            except Exception:
                pass

        page.on("request", on_request)

        try:
            await page.goto(PAGE.format(room=room), wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass  # 超时也继续等，网络请求可能已经发出来了

        deadline = time.time() + timeout
        while time.time() < deadline and not found:
            try:
                await page.evaluate(PLAY_JS)
            except Exception:
                pass
            await asyncio.sleep(1.5)

        return found[0] if found else None
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def grab_channel(browser, name: str, room: str, timeout: int, attempts: int = 2):
    for i in range(attempts):
        url = await _grab_once(browser, room, timeout)
        if url:
            return name, url
        if i + 1 < attempts:
            log(f"  {name} 第一次没抓到，重试…")
            await asyncio.sleep(3)
    return name, None


def verify_url(url: str, timeout: int = 20) -> bool:
    """确认这个地址现在真能拉到 m3u8 文本。"""
    req = Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://m-live.cctvnews.cctv.com/",
            "Accept": "*/*",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as r:
            body = r.read(8192)
            status = getattr(r, "status", 200)
        return status == 200 and b"#EXT" in body
    except (HTTPError, URLError, TimeoutError, OSError):
        return False
    except Exception:
        return False


def load_existing(path: Path):
    """读上一次的 m3u，用于个别台抓失败时兜底，避免整台消失。"""
    old = {}
    if not path.exists():
        return old
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return old
    last_name = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            last_name = None
            # 取逗号后面那个台名
            if "," in line:
                last_name = line.rsplit(",", 1)[1].strip()
        elif line.startswith("#"):
            continue
        elif last_name and line.startswith("http"):
            old[last_name] = line
            last_name = None
    return old


def write_m3u(path: Path, chosen: dict, stamp: str):
    lines = [
        "#EXTM3U",
        f"# generated_at={stamp}  (auto-refreshed by GitHub Actions)",
    ]
    for name, no, _room in CHANNELS:
        url = chosen.get(name)
        if not url:
            continue
        lines.append(
            f'#EXTINF:-1 tvg-name="{name}" tvg-chno="{no}" group-title="央视",{name}'
        )
        lines.append(url)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args) -> int:
    out = Path(args.out)
    old = load_existing(out)
    log(f"开始刷新：共 {len(CHANNELS)} 台，并发 {args.jobs}，单台等待 {args.timeout}s")

    launch_kwargs = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--mute-audio",
            "--autoplay-policy=no-user-gesture-required",
        ],
    }
    chrome = os.environ.get("CHROME_PATH")
    if chrome:
        launch_kwargs["executable_path"] = chrome

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        sem = asyncio.Semaphore(max(1, args.jobs))

        async def worker(name, room):
            async with sem:
                got = await grab_channel(browser, name, room, args.timeout)
                ok = False
                if got[1]:
                    ok = await asyncio.to_thread(verify_url, got[1])
                    log(f"  {name}: {'抓到且可播' if ok else '抓到但校验失败'}")
                else:
                    log(f"  {name}: 没抓到")
                return got[0], (got[1] if ok else None)

        tasks = [worker(n, r) for n, _no, r in CHANNELS]
        pairs = await asyncio.gather(*tasks)
        await browser.close()

    fresh = {n: u for n, u in pairs if u}

    chosen = {}
    fallback, missing = [], []
    for name, _no, _room in CHANNELS:
        if name in fresh:
            chosen[name] = fresh[name]
        elif name in old:
            chosen[name] = old[name]
            fallback.append(name)
        else:
            missing.append(name)

    log("")
    log(f"结果：新抓到 {len(fresh)} 台 | 用旧地址兜底 {len(fallback)} 台 | 缺失 {len(missing)} 台")
    if fallback:
        log("  兜底：" + "、".join(fallback))
    if missing:
        log("  缺失：" + "、".join(missing))

    if not fresh:
        log("")
        log("这次一个都没抓到，不覆盖原文件（多半是央视页面改版或被限流）。")
        return 1

    if len(fresh) < args.min_ok:
        log(f"只抓到 {len(fresh)} 台，低于 --min-ok {args.min_ok}，不覆盖原文件。")
        return 1

    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    write_m3u(out, chosen, stamp)
    log("")
    log(f"已写入 {out}（{len(chosen)} 台，更新时间 {stamp}）")

    # 再存一份纯英文文件名，方便当 raw 直链导入播放器（中文名有时会被客户端编码坏）
    alias = (args.alias_out or "").strip()
    if alias and Path(alias) != out:
        write_m3u(Path(alias), chosen, stamp)
        log(f"已写入 {alias}（同一份内容，英文名便于做订阅链接）")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.environ.get("OUT", "央视直播_10台.m3u"))
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("JOBS", "3")))
    ap.add_argument("--timeout", type=int, default=40)
    ap.add_argument(
        "--alias-out",
        default=os.environ.get("ALIAS_OUT", "cctv10.m3u"),
        dest="alias_out",
        help="额外输出一份英文名副本，传空字符串关闭",
    )
    ap.add_argument("--min-ok", type=int, default=5, dest="min_ok")
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
    