#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
我的影视库合并构建器（2026-10-29）

把三个上游源实时拉取、去重合并成 movie.json，推到 GitHub 后即"我的固定库"。
任何上游(牛虎/9280/自己的tv-cdn)新增站点 -> 重跑本脚本 -> 自动合并 -> 地址不变。

用法：
    python build_my_library.py            # 合并 A+B+C 并写 movie.json
    python build_my_library.py --quality  # 质量优先：排除 9280 杂源(去广告/低清)
    python build_my_library.py --push     # 合并并 git push(走 SSH) 到 movie-sources

去重规则：按站点 key 去重，A(我的精选)优先在前，B(自己的tv-cdn)次之，C(9280)最后。
          lives/parses/rules/flags/ads 同样按名称去重合并。
spider：合并所有源的 spider(空格分隔)，最大化解析兼容。
"""
import json
import os
import ssl
import subprocess
import sys
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0"}

# 三个上游源（每个带主用+兜底候选）。B 的 cdn.jsdelivr 在本机 SSL 不稳，用 fastly/gh-proxy。
SOURCES = [
    ("A_movie", [
        "https://fastly.jsdelivr.net/gh/Venus521/movie-sources@main/movie.json",
        "https://gh-proxy.com/https://raw.githubusercontent.com/Venus521/movie-sources/main/movie.json",
    ]),
    ("B_winbox", [
        "https://fastly.jsdelivr.net/gh/Venus521/tv-cdn@main/winbox-config.json",
        "https://gh-proxy.com/https://raw.githubusercontent.com/Venus521/tv-cdn/main/winbox-config.json",
    ]),
    ("C_9280", [
        "https://9280.kstore.vip/newwex.json",
    ]),
]


def fetch(urls):
    for u in urls:
        try:
            req = urllib.request.Request(u, headers=UA)
            r = urllib.request.urlopen(req, timeout=40, context=ctx)
            return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            print(f"  [拉取失败] {u} -> {e}")
    return None


def dedup_merge(items, keyfn):
    """按 keyfn 去重合并列表，保留首次出现。"""
    out, seen = [], set()
    for it in items:
        k = keyfn(it)
        if k is None or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def main():
    quality = "--quality" in sys.argv
    push = "--push" in sys.argv

    # 质量优先：排除 9280 杂源（带回广告/低清解析线）
    exclude = {"C_9280"} if quality else set()
    print(f"模式: {'质量优先(排除9280)' if quality else '全量合并(A+B+C)'}")

    sites, lives, parses, rules, flags, ads, danmaku = [], [], [], [], [], [], []
    spiders, wallpaper = [], None

    for label, urls in SOURCES:
        if label in exclude:
            print(f"  跳过 {label}")
            continue
        d = fetch(urls)
        if not d:
            print(f"  !! {label} 全部候选失败，跳过（不影响其余源）")
            continue
        print(f"  {label}: sites={len(d.get('sites', []))} lives={len(d.get('lives', []))} parses={len(d.get('parses', []))}")
        sites.extend(d.get("sites", []))
        lives.extend(d.get("lives", []))
        parses.extend(d.get("parses", []))
        rules.extend(d.get("rules", []))
        flags.extend(d.get("flags", []))
        ads.extend(d.get("ads", []) or [])
        danmaku.extend(d.get("danmaku", []) or [])
        sp = d.get("spider")
        if sp:
            spiders.append(sp)
        if not wallpaper and d.get("wallpaper"):
            wallpaper = d.get("wallpaper")

    sites = dedup_merge(sites, lambda s: s.get("key"))
    lives = dedup_merge(lives, lambda x: (x.get("name") or str(x))[:40])
    parses = dedup_merge(parses, lambda x: x.get("name") or json.dumps(x, ensure_ascii=False)[:60])
    rules = dedup_merge(rules, lambda x: x.get("name") or json.dumps(x, ensure_ascii=False)[:60])
    flags = dedup_merge(flags, lambda x: json.dumps(x, ensure_ascii=False)[:80])
    ads = dedup_merge(ads, lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)[:80])
    danmaku = dedup_merge(danmaku, lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)[:80])

    out = {
        "spider": " ".join(dict.fromkeys(spiders)),
        "sites": sites,
        "rules": rules,
        "flags": flags,
        "lives": lives,
        "parses": parses,
        "ads": ads,
    }
    if wallpaper:
        out["wallpaper"] = wallpaper
    if danmaku:
        out["danmaku"] = danmaku

    here = os.path.dirname(os.path.abspath(__file__))
    dst = os.path.join(here, "movie.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n写出 movie.json: {len(sites)} 站点 / {len(lives)} 直播 / {len(parses)} 解析")
    print(f"  spider: {out['spider'][:80]}...")

    if push:
        print("\n>>> git push (SSH) ...")
        subprocess.run(
            "git remote set-url origin git@github.com:Venus521/movie-sources.git",
            shell=True, cwd=here,
        )
        msg = "merge: A+B+C 全量整合" if not quality else "merge: 质量优先(A+B, 排除9280)"
        subprocess.run(f'git add movie.json && git commit -q -m "{msg}"', shell=True, cwd=here)
        rc = subprocess.run(
            'GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=no" git push origin main',
            shell=True, cwd=here,
        ).returncode
        print("push rc =", rc)


if __name__ == "__main__":
    main()
