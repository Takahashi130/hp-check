"""ホームページチェックくん：確認してもらうページの縦長の画面写真（PC 幅・スマホ幅）を撮る。

使い方：
  python 道具/shot.py <ページの URL> <名前（英数字）> [--title "題"] [--pc 1440] [--sp 390]

  例：python 道具/shot.py https://crifa.co.jp/ crifa-top --title "クリファ トップ"

  pages/<名前>/pc.jpg・sp.jpg・page.json ができる。見るリンクは index.html?p=<名前>

手元の Edge をヘッドレスで動かし、開発者用のつなぎ口（CDP）で「縦長全体」を撮る。
  - msedge --screenshot だけだと窓の高さまでしか撮れないので、CDP の captureBeyondViewport を使う。
  - 下までゆっくり動かしてから撮る（下に来てから出る絵・動きを出しておくため）。
  - 長いページは区切って撮ってつなぐ（一度に撮ると途中から上の絵がくり返し写るため）。
必要なもの：Edge、Python の websocket-client と Pillow（pip install websocket-client pillow）
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, base64, urllib.request
from datetime import datetime
import io
import websocket
from PIL import Image

EDGE = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
PORT = 9339
SP_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=120, suppress_origin=True)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == my:
                if "error" in m:
                    raise RuntimeError(method + ": " + str(m["error"]))
                return m.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")


def wait_ready(c, sec=30):
    t0 = time.time()
    while time.time() - t0 < sec:
        if c.js("document.readyState") == "complete":
            return
        time.sleep(0.3)


def shoot(c, url, width, mobile, out, max_px=30000):
    c.call("Emulation.setDeviceMetricsOverride", width=width, height=900 if not mobile else 844,
           deviceScaleFactor=1 if not mobile else 2, mobile=mobile)
    c.call("Emulation.setUserAgentOverride", userAgent=SP_UA if mobile else "")
    c.call("Emulation.setTouchEmulationEnabled", enabled=mobile)
    c.call("Page.navigate", url=url)
    time.sleep(1.0)
    wait_ready(c)
    time.sleep(1.5)
    # 下までゆっくり動かす（あとから出る絵・ふわっと出る文字を出しておく）→ 上に戻す
    c.js("""new Promise(res => { let y = 0; const step = () => { y += innerHeight * 0.6; scrollTo(0, y);
           if (y < document.documentElement.scrollHeight) setTimeout(step, 180); else setTimeout(res, 800); }; step(); })""")
    c.js("scrollTo(0, 0)")
    time.sleep(1.5)
    title = c.js("document.title") or ""
    m = c.call("Page.getLayoutMetrics")
    size = m.get("cssContentSize") or m.get("contentSize")
    w, h = width, int(size["height"])
    dsf = 1 if not mobile else 2
    scale = 1.0
    if h * dsf > max_px:            # とても長いページは JPEG の上限に入るよう縮める
        scale = max_px / (h * dsf)
    # 一度に長く撮ると、Edge の描画の上限（およそ 16000px）を超えた所から上の絵がくり返し写る。
    # 区切って撮って、あとでつなぐ。
    step_h = max(200, int(6000 / dsf))
    parts, y = [], 0
    while y < h:
        ch = min(step_h, h - y)
        r = c.call("Page.captureScreenshot", format="png", captureBeyondViewport=True, fromSurface=True,
                   clip={"x": 0, "y": y, "width": w, "height": ch, "scale": scale})
        parts.append(Image.open(io.BytesIO(base64.b64decode(r["data"]))).convert("RGB"))
        y += ch
    W = parts[0].width
    img = Image.new("RGB", (W, sum(p.height for p in parts)), "white")
    oy = 0
    for p in parts:
        img.paste(p, (0, oy)); oy += p.height
    img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return {"w": w, "h": h, "title": title}


def main():
    ap = argparse.ArgumentParser(description="確認用の縦長の画面写真を撮る")
    ap.add_argument("url")
    ap.add_argument("name", help="pages/ の下のフォルダ名（英数字とハイフン）")
    ap.add_argument("--title", default="")
    ap.add_argument("--pc", type=int, default=1440)
    ap.add_argument("--sp", type=int, default=390)
    a = ap.parse_args()

    edge = next((p for p in EDGE if os.path.exists(p)), None)
    if not edge:
        sys.exit("Edge が見つかりません")
    outdir = os.path.join(ROOT, "pages", a.name)
    os.makedirs(outdir, exist_ok=True)
    prof = tempfile.mkdtemp(prefix="hpcheck_edge_")
    p = subprocess.Popen([edge, "--headless=new", f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}",
                          "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", "--lang=ja", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws = None
        for _ in range(60):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2))
                ws = next(t["webSocketDebuggerUrl"] for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.5)
        if not ws:
            sys.exit("Edge につながりませんでした")
        c = CDP(ws)
        c.call("Page.enable")
        c.call("Runtime.enable")
        pc = shoot(c, a.url, a.pc, False, os.path.join(outdir, "pc.jpg"))
        print(f"PC   {pc['w']}×{pc['h']} → pc.jpg")
        sp = shoot(c, a.url, a.sp, True, os.path.join(outdir, "sp.jpg"))
        print(f"スマホ {sp['w']}×{sp['h']} → sp.jpg")
        meta = {"title": a.title or pc["title"] or a.url, "url": a.url, "made": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "shots": {"pc": {"img": "pc.jpg", "w": pc["w"], "h": pc["h"]}, "sp": {"img": "sp.jpg", "w": sp["w"], "h": sp["h"]}}}
        with open(os.path.join(outdir, "page.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        # ページは page.js を読む（ファイルを直接開いた時も読めるように。JSON の取り寄せは file:// だと止められる）
        with open(os.path.join(outdir, "page.js"), "w", encoding="utf-8") as f:
            f.write("(window.HP_PAGES = window.HP_PAGES || {})[" + json.dumps(a.name) + "] = " +
                    json.dumps(meta, ensure_ascii=False) + ";\n")
        print("できました：index.html?p=" + a.name)
    finally:
        p.terminate()
        try:
            p.wait(10)
        except Exception:
            p.kill()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
