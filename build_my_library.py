#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
我的影视库合并构建器（2026-10-29）

把三个上游源实时拉取、去重合并成 movie.json，推到 GitHub 后即"我的固定库"。
任何上游(牛虎/9280/自己的tv-cdn)新增站点 -> 重跑本脚本 -> 自动合并 -> 地址不变。

用法：
    python build_my_library.py            # 合并 A+B+C 并写 movie.json + movie-lite.json
    python build_my_library.py --quality  # 质量优先：排除 9280 杂源(去广告/低清)
    python build_my_library.py --push     # 合并并 git push(走 SSH) 到 movie-sources

产出两个文件（同源，一次跑都生成）：
    movie.json       完整版：全部站点，含 type=3（依赖 spider jar / 远程JS / Python 脚本）
                     → 给安卓壳（默影视 / TVBOX / 影视仓），引擎齐全
    movie-lite.json  纯净版：只含 type!=3 的直连采集站，零外部依赖
                     → 给引擎不全的壳（Win-Box 桌面版等），保底可用

为什么要有 lite 版：movie.json 里 273 站有 224 个是 type=3，它们要靠
spider jar(csp_*) 或 ./py/xxx.py、远程 JS 规则才能跑。安卓壳一般自带，
但 Win-Box(Windows 桌面版，Electron 移植) 对 jar/脚本引擎支持不全，
喂完整版会看到一堆点不开的站点。lite 版剔除全部 type=3，任何壳都能吃。

去重规则：按站点 key 去重，A(我的精选)优先在前，B(自己的tv-cdn)次之，C(9280)最后。
          lives/parses/rules/flags/ads 同样按名称去重合并。
spider：合并所有源的 spider(空格分隔)，最大化解析兼容（仅完整版带）。
"""
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0"}

# 上游源（每个带主用+兜底候选）。A/B 一律 raw 优先：fastly.jsdelivr 的 @main 有 12h 缓存，
# 拿旧版本会把最新一次提交的站点"回退"掉。
SOURCES = [
    ("A_movie", [
        "https://gh-proxy.com/https://raw.githubusercontent.com/Venus521/movie-sources/main/movie.json",
        "https://fastly.jsdelivr.net/gh/Venus521/movie-sources@main/movie.json",
    ]),
    ("B_winbox", [
        "https://gh-proxy.com/https://raw.githubusercontent.com/Venus521/tv-cdn/main/winbox-config.json",
        "https://fastly.jsdelivr.net/gh/Venus521/tv-cdn@main/winbox-config.json",
    ]),
    ("C_9280", [
        "https://9280.kstore.vip/newwex.json",
    ]),
    # D: ggrrttyyiii 的精选 43 站，类在它自己的 jar 里（easy1008 和 wexguard 都没有），
    # 所以并入时给每个站单独挂 jar，并把这个 jar 镜像到本仓库 jar/ 下，避免依赖对方 CDN。
    ("D_demo", [
        "https://gh-proxy.org/raw.githubusercontent.com/ggrrttyyiii/CatVodSpider/refs/heads/main/json/demo.json",
        "https://fastly.jsdelivr.net/gh/ggrrttyyiii/CatVodSpider@main/json/demo.json",
    ]),
]

# 镜像后的 jar 由 movie-sources 自己托管；md5 在下载时现算
DEMO_JAR_MIRROR = "https://gh-proxy.com/https://raw.githubusercontent.com/Venus521/movie-sources/main/jar/demo_spider.jar"


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


def mirror_demo_jar(spider):
    """把上游 spider 指向的 jar 下载并镜像到 jar/demo_spider.jar，返回带 md5 的镜像地址。"""
    src = (spider or "").split(";md5;")[0].strip()
    if not src:
        return None
    try:
        req = urllib.request.Request(src, headers={"User-Agent": "okhttp/3.15"})
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            data = r.read()
    except Exception as e:
        print(f"  [jar 镜像失败] {src} -> {e}")
        return None
    if not data.startswith(b"PK"):
        print(f"  [jar 镜像失败] 内容不是 zip/jar: {src}")
        return None
    os.makedirs(os.path.join(HERE, "jar"), exist_ok=True)
    with open(os.path.join(HERE, "jar", "demo_spider.jar"), "wb") as f:
        f.write(data)
    md5 = hashlib.md5(data).hexdigest()
    print(f"  镜像 jar: {len(data)} bytes md5={md5}")
    # 站点级 jar 只给纯 URL：带 ;md5; 是否被解析取决于壳，纯 URL 两种壳都能吃
    return DEMO_JAR_MIRROR


def load_spider_blobs(spiders):
    """下载全局 spider 里的 jar（含伪装的图片 jar），返回 (dex 字节列表, 失败数)。"""
    urls = []
    for sp in spiders:
        for tok in sp.split():
            u = tok.split(";md5;")[0].strip()
            if u.startswith("http") and u not in urls:
                urls.append(u)
    blobs, failed = [], 0
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "okhttp/3.15"})
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                data = r.read()
            if data.startswith(b"PK"):
                z = zipfile.ZipFile(io.BytesIO(data))
                if "classes.dex" in z.namelist():
                    blobs.append(z.read("classes.dex"))
                else:
                    failed += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            print(f"  [spider 检查跳过] {u[:60]} -> {e}")
    return blobs, failed


def drop_unresolvable(sites, spiders):
    """兜底：csp_ 站点既没挂自己的 jar、类也不在任何 spider jar 里，就是点开必报错的死站，删掉。
    任一个 jar 下载失败就不删（宁可留着也不误杀）。"""
    blobs, failed = load_spider_blobs(spiders)
    if failed:
        print(f"  [跳过死站清理] 有 {failed} 个 spider jar 下载失败，避免误杀")
        return sites
    kept, dropped = [], []
    for s in sites:
        api = s.get("api", "")
        if api.startswith("csp_") and not s.get("jar") and not class_exists(api, blobs):
            dropped.append(s)
            continue
        kept.append(s)
    if dropped:
        print(f"  清理死站 {len(dropped)} 个: {'、'.join(str(s.get('key'))[:12] for s in dropped)}")
    return kept


def class_exists(api, blobs):
    """api 形如 csp_Xxx，检查 L.../csp 包下是否有该类的定义描述符。"""
    if not api.startswith("csp_"):
        return False
    d = b"Lcom/github/catvod/spider/" + api[4:].encode() + b";"
    return any(d in b for b in blobs)


def prepare_demo_sites(dsites, jar, usable_apis):
    """D 源的站点：加 demo_ 前缀避开 key 冲突，挂上自己的 jar，跳过库里已有且能正常加载的同类实现。"""
    out, skipped = [], []
    for s in dsites:
        api = s.get("api", "")
        if not api.startswith("csp_"):
            continue
        if api in usable_apis:
            skipped.append(s.get("name", api))
            continue
        s = dict(s)
        s["key"] = "demo_" + str(s.get("key") or api[4:])
        if jar:
            s["jar"] = jar
        out.append(s)
    if skipped:
        print(f"  跳过同类已有 {len(skipped)} 个: {'、'.join(x[:16] for x in skipped)}")
    return out


def main():
    quality = "--quality" in sys.argv
    push = "--push" in sys.argv

    # 质量优先：排除 9280 杂源（带回广告/低清解析线）
    exclude = {"C_9280"} if quality else set()
    print(f"模式: {'质量优先(排除9280)' if quality else '全量合并(A+B+C+D)'}")

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
        if label == "D_demo":
            jar = mirror_demo_jar(d.get("spider"))
            blobs, _ = load_spider_blobs(spiders)
            usable = {s.get("api") for s in sites if class_exists(s.get("api", ""), blobs)}
            print(f"  全局 spider 可解析类: {len(usable)} 个")
            sites.extend(prepare_demo_sites(d.get("sites", []), jar, usable))
            # D 的 spider 只给它的站点单独用，不进全局，避免和 easy1008 的同名类打架
        else:
            sites.extend(d.get("sites", []))
            sp = d.get("spider")
            if sp:
                spiders.append(sp)
        lives.extend(d.get("lives", []))
        parses.extend(d.get("parses", []))
        rules.extend(d.get("rules", []))
        flags.extend(d.get("flags", []))
        ads.extend(d.get("ads", []) or [])
        dm = d.get("danmaku")
        # 上游有把字符串 extend 成单字符数组的坏数据（join 后已不可还原），只收完整字符串
        if isinstance(dm, str):
            danmaku.append(dm)
        elif isinstance(dm, list):
            joined = "".join(x for x in dm if isinstance(x, str))
            if dm and not all(len(x) == 1 for x in dm if isinstance(x, str)):
                danmaku.append(joined)
            else:
                print(f"  丢弃损坏的 danmaku（单字符数组，{len(dm)} 项）")
        if not wallpaper and d.get("wallpaper"):
            wallpaper = d.get("wallpaper")

    sites = dedup_merge(sites, lambda s: s.get("key"))
    sites = drop_unresolvable(sites, spiders)
    lives = dedup_merge(lives, lambda x: (x.get("name") or str(x))[:40])
    parses = dedup_merge(parses, lambda x: x.get("name") or json.dumps(x, ensure_ascii=False)[:60])
    rules = dedup_merge(rules, lambda x: x.get("name") or json.dumps(x, ensure_ascii=False)[:60])
    flags = dedup_merge(flags, lambda x: json.dumps(x, ensure_ascii=False)[:80])
    ads = dedup_merge(ads, lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)[:80])
    danmaku = dedup_merge(danmaku, lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)[:80])

    out = {
        # 按 token 去重：各源的 spider 往往是同一份，整串去重会拼出重复 jar
        "spider": " ".join(dict.fromkeys(t for sp in spiders for t in sp.split())),
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
        out["danmaku"] = danmaku[0] if len(danmaku) == 1 else danmaku

    dst = os.path.join(HERE, "movie.json")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n写出 movie.json: {len(sites)} 站点 / {len(lives)} 直播 / {len(parses)} 解析")
    print(f"  spider: {out['spider'][:80]}...")

    # ── 纯净版：剔除所有 type=3（依赖 spider jar / 远程JS / Python 脚本）──
    lite_sites = [s for s in sites if str(s.get("type")) != "3"]
    out_lite = {
        "spider": "",  # 无 type=3 站点则不需要 jar，留空避免壳白下载
        "sites": lite_sites,
        "rules": rules,
        "flags": flags,
        "lives": lives,
        "parses": parses,
        "ads": ads,
    }
    if wallpaper:
        out_lite["wallpaper"] = wallpaper
    if danmaku:
        out_lite["danmaku"] = danmaku[0] if len(danmaku) == 1 else danmaku
    dst_lite = os.path.join(HERE, "movie-lite.json")
    # lite 在仓库里是 CRLF，跟 movie.json(LF) 保持各自原样，别制造整文件 diff
    with open(dst_lite, "w", encoding="utf-8") as f:
        json.dump(out_lite, f, ensure_ascii=False, indent=2)
    print(f"写出 movie-lite.json: {len(lite_sites)} 站点（纯直连，无 spider）"
          f" / {len(lives)} 直播 / {len(parses)} 解析")

    if push:
        print("\n>>> git push (SSH) ...")
        subprocess.run(
            "git remote set-url origin git@github.com:Venus521/movie-sources.git",
            shell=True, cwd=HERE,
        )
        msg = "merge: A+B+C+D 全量整合" if not quality else "merge: 质量优先(A+B+D, 排除9280)"
        msg += f" | {len(sites)}站 / lite {len(lite_sites)}站"
        subprocess.run(f'git add movie.json movie-lite.json jar build_my_library.py && git commit -q -m "{msg}"',
                       shell=True, cwd=HERE)
        # Windows 下 shell=True 走 cmd.exe，不认 `VAR=val cmd` 这种 bash 前缀赋值，
        # 必须用 env 传（否则报 "'GIT_SSH_COMMAND' is not recognized"）。
        env = os.environ.copy()
        env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=no"
        rc = subprocess.run(
            "git push origin main", shell=True, cwd=here, env=env,
        ).returncode
        print("push rc =", rc)


if __name__ == "__main__":
    main()
