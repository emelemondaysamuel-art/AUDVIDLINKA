import os, shutil, tempfile, time, sqlite3, secrets, hmac
from functools import wraps
from urllib.parse import urlparse
from collections import defaultdict
from flask import (Flask, request, jsonify, send_file, after_this_request,
                   render_template_string, session, redirect)
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Accounts: set USERS="alice:pass1,bob:pass2" in the environment
USERS = dict(p.split(":", 1) for p in os.environ.get("USERS", "admin:change-me").split(",") if ":" in p)
DB = os.environ.get("DB_PATH", "linkgrab.db")

ALLOWED = ("youtube.com", "youtu.be", "tiktok.com", "facebook.com", "fb.watch", "instagram.com")
BLOCKED = ("spotify.com", "spotify.link")  # DRM-protected: not supported
HEIGHTS = {144, 240, 360, 480, 720, 1080, 1440, 2160}
BITRATES = {128, 192, 320}
hits = defaultdict(list)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS history(
        id INTEGER PRIMARY KEY, user TEXT, url TEXT, title TEXT,
        thumb TEXT, kind TEXT, quality TEXT, ts INTEGER)""")


def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if "u" not in session:
            if request.path.startswith("/api"):
                return jsonify(error="Please log in."), 401
            return redirect("/login")
        return f(*a, **k)
    return wrap


def check(url):
    host = (urlparse(url).hostname or "").lower()
    if any(host == b or host.endswith("." + b) for b in BLOCKED):
        return "Spotify is DRM-protected and can't be downloaded here."
    if not any(host == a or host.endswith("." + a) for a in ALLOWED):
        return "Unsupported site. Use YouTube, TikTok, Facebook or Instagram links."
    return None


def rate_limited(key, limit=10, window=60):
    now = time.time()
    hits[key] = [t for t in hits[key] if now - t < window]
    if len(hits[key]) >= limit:
        return True
    hits[key].append(now)
    return False


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        u, p = request.form.get("u", ""), request.form.get("p", "")
        if rate_limited("login:" + str(request.remote_addr), limit=8, window=300):
            err = "Too many attempts. Try again in a few minutes."
        elif u in USERS and hmac.compare_digest(USERS[u], p):
            session["u"] = u
            return redirect("/")
        else:
            err = "Wrong username or password."
    return render_template_string(LOGIN, err=err)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.post("/api/info")
@login_required
def info():
    url = (request.json or {}).get("url", "").strip()
    err = check(url)
    if err:
        return jsonify(error=err), 400
    if rate_limited(session["u"]):
        return jsonify(error="Too many requests. Wait a minute."), 429
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as y:
            d = y.extract_info(url, download=False)
    except Exception:
        return jsonify(error="Could not read that link (private, removed, or login required)."), 400
    hs = sorted({f["height"] for f in d.get("formats", [])
                 if f.get("height") and f.get("vcodec") != "none" and f["height"] in HEIGHTS})
    return jsonify(title=d.get("title"), thumbnail=d.get("thumbnail"),
                   duration=d.get("duration"), uploader=d.get("uploader"), heights=hs)


@app.get("/dl")
@login_required
def dl():
    url, kind = request.args.get("url", ""), request.args.get("kind", "video")
    err = check(url)
    if err:
        return err, 400
    if rate_limited("dl:" + session["u"], limit=5):
        return "Too many downloads. Wait a minute.", 429

    tmp = tempfile.mkdtemp()
    opts = {"quiet": True, "noplaylist": True,
            "outtmpl": os.path.join(tmp, "%(title).80s.%(ext)s")}
    if kind == "audio":
        abr = request.args.get("q", type=int)
        abr = abr if abr in BITRATES else 192
        quality = f"{abr}kbps"
        opts.update(format="bestaudio/best", postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(abr)}])
    else:
        h = request.args.get("q", type=int)
        if h in HEIGHTS:
            quality = f"{h}p"
            opts.update(format=f"bv*[height<={h}]+ba/b[height<={h}]")
        else:
            quality = "best"
            opts.update(format="bv*+ba/b")
        opts["merge_output_format"] = "mp4"
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(url, download=True)
        path = os.path.join(tmp, os.listdir(tmp)[0])
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return "Download failed.", 500

    with db() as c:
        c.execute("INSERT INTO history(user,url,title,thumb,kind,quality,ts) VALUES(?,?,?,?,?,?,?)",
                  (session["u"], url, d.get("title"), d.get("thumbnail"), kind, quality, int(time.time())))

    @after_this_request
    def cleanup(resp):
        shutil.rmtree(tmp, ignore_errors=True)
        return resp

    return send_file(path, as_attachment=True)


@app.get("/api/history")
@login_required
def history():
    with db() as c:
        rows = c.execute("SELECT id,url,title,thumb,kind,quality,ts FROM history "
                         "WHERE user=? ORDER BY id DESC LIMIT 50", (session["u"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.delete("/api/history")
@login_required
def clear_history():
    with db() as c:
        c.execute("DELETE FROM history WHERE user=?", (session["u"],))
    return jsonify(ok=True)


STYLE = """<meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;padding:20px;background:#0f1115;color:#eee}
input,select,button{width:100%;padding:14px;border-radius:10px;border:0;font-size:16px;box-sizing:border-box;margin-top:10px}
input,select{background:#1c2029;color:#eee}button{background:#4f8cff;color:#fff;font-weight:600}
a.btn{display:block;text-align:center;padding:14px;border-radius:10px;background:#22c55e;color:#fff;
text-decoration:none;margin-top:10px;font-weight:600}a.btn.a{background:#a855f7}
img.t{width:100%;border-radius:10px;margin-top:14px}#msg,.err{color:#f87171;margin-top:10px}small{color:#888}
.row{display:flex;gap:10px;align-items:center;padding:10px;background:#1c2029;border-radius:10px;margin-top:8px;cursor:pointer}
.row img{width:56px;height:40px;object-fit:cover;border-radius:6px}.row span{font-size:14px;flex:1;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}.row small{white-space:nowrap}
.top{display:flex;justify-content:space-between;align-items:center}.top a{color:#888;font-size:14px}
button.g{background:#2a2f3a;font-weight:400;font-size:14px;padding:10px}
</style>"""

LOGIN = """<!doctype html><html><head><title>LinkGrab login</title>""" + STYLE + """</head><body>
<h2>LinkGrab</h2><form method=post>
<input name=u placeholder=Username autocomplete=username>
<input name=p type=password placeholder=Password autocomplete=current-password>
<button>Log in</button></form><div class=err>{{err}}</div></body></html>"""

PAGE = """<!doctype html><html><head><title>LinkGrab</title>""" + STYLE + """</head><body>
<div class=top><h2>LinkGrab</h2><a href=/logout>Log out</a></div>
<small>YouTube · TikTok · Facebook · Instagram</small>
<input id=u placeholder="Paste link here" inputmode=url>
<button onclick=go()>Get download links</button><div id=msg></div><div id=out></div>
<p><small>Only download content you own or have permission to use.</small></p>
<h3>History</h3><div id=hist></div>
<button class=g onclick=clr()>Clear history</button>
<script>
let cur=null;
const el=(t,p={},kids=[])=>{const e=document.createElement(t);Object.assign(e,p);kids.forEach(k=>e.append(k));return e};
const safeImg=s=>s&&s.startsWith('https://')?s:'';

async function go(){
 const url=document.getElementById('u').value.trim(),m=document.getElementById('msg'),o=document.getElementById('out');
 m.textContent='Checking…';o.replaceChildren();
 const r=await fetch('/api/info',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
 if(r.status===401){location='/login';return}
 const d=await r.json();
 if(!r.ok){m.textContent=d.error;return}
 m.textContent='';cur=url;
 const vq=el('select',{id:'vq'},[el('option',{value:'best',textContent:'Video quality: Best'}),
  ...[...d.heights].reverse().map(h=>el('option',{value:h,textContent:h+'p'}))]);
 const aq=el('select',{id:'aq'},[128,192,320].map(b=>el('option',{value:b,selected:b==192,textContent:'Audio quality: '+b+' kbps'})));
 const v=el('a',{className:'btn',textContent:'Download video (MP4)'});
 const a=el('a',{className:'btn a',textContent:'Download audio (MP3)'});
 const upd=()=>{const q=encodeURIComponent(url);
  v.href='/dl?kind=video&q='+vq.value+'&url='+q; a.href='/dl?kind=audio&q='+aq.value+'&url='+q};
 vq.onchange=aq.onchange=upd;upd();
 if(safeImg(d.thumbnail))o.append(el('img',{className:'t',src:d.thumbnail}));
 o.append(el('h3',{textContent:d.title||''}),vq,v,aq,a);
 [v,a].forEach(x=>x.addEventListener('click',()=>setTimeout(loadHist,4000)));
}

async function loadHist(){
 const r=await fetch('/api/history');if(!r.ok)return;
 const rows=await r.json(),h=document.getElementById('hist');h.replaceChildren();
 if(!rows.length){h.append(el('small',{textContent:'Nothing yet.'}));return}
 rows.forEach(x=>{
  const row=el('div',{className:'row'},[
   ...(safeImg(x.thumb)?[el('img',{src:x.thumb})]:[]),
   el('span',{textContent:x.title||x.url}),
   el('small',{textContent:(x.kind==='audio'?'MP3 ':'MP4 ')+x.quality})]);
  row.onclick=()=>{document.getElementById('u').value=x.url;go();scrollTo(0,0)};
  h.append(row)});
}
async function clr(){await fetch('/api/history',{method:'DELETE'});loadHist()}
loadHist();
</script></body></html>"""


@app.get("/")
@login_required
def home():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
