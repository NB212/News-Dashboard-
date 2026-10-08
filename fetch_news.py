#!/usr/bin/env python3
"""News Hub: holt RSS/Atom-Feeds, gruppiert ähnliche Meldungen zu Ereignissen,
bewertet die Relevanz (0-100) und schreibt news.json. Nur Standardbibliothek."""
import hashlib, html, json, os, re, sys, urllib.parse, urllib.request, urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

UA = "NewsHubBot/1.0 (persoenliches News-Dashboard)"
MAX_PER_FEED = 15   # Artikel pro Feed und Lauf
MAX_AGE_H = 72      # ältere Artikel werden ignoriert
SIM = 0.35          # Titel-Ähnlichkeit (0-1): ab hier gelten Artikel als ein Ereignis
MAX_LLM = 20        # KI-Zusammenfassungen pro Lauf (Kostenbremse)
W = {"recency": 0.45, "sources": 0.35, "priority": 0.20}  # Gewichte der Relevanz
STOP = set("der die das und oder ein eine von mit für auf im in zu den dem des ist the and for with that this are was".split())


def get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


_robots = {}
def allowed(url):
    """Respektiert robots.txt der jeweiligen Seite."""
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(get(base + "/robots.txt", 10).decode("utf-8", "ignore").splitlines())
        except Exception:
            rp = None
        _robots[base] = rp
    return _robots[base] is None or _robots[base].can_fetch(UA, url)


def discover(url):
    """Sucht auf der Startseite nach einem RSS/Atom-Link."""
    page = get(url).decode("utf-8", "ignore")
    for m in re.finditer(r"<link[^>]+>", page, re.I):
        tag = m.group(0)
        if re.search(r"type=[\"']application/(rss|atom)\+xml", tag, re.I):
            h = re.search(r"href=[\"']([^\"']+)", tag)
            if h:
                return urllib.parse.urljoin(url, h.group(1))
    return None


def text(el, *names):
    for n in names:
        e = el.find(n)
        if e is not None and (e.text or "").strip():
            return e.text.strip()
    return ""


def clean(s, n=300):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_bytes):
    """Liest RSS 2.0 und Atom: Liste aus (titel, link, beschreibung, datum)."""
    root = ET.fromstring(xml_bytes)
    A = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in root.iter("item"):
        out.append((text(it, "title"), text(it, "link"), text(it, "description"),
                    text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date")))
    for en in root.iter(A + "entry"):
        l = en.find(A + "link")
        out.append((text(en, A + "title"), l.get("href") if l is not None else "",
                    text(en, A + "summary", A + "content"), text(en, A + "published", A + "updated")))
    return out


def tokens(t):
    return {w for w in re.findall(r"\w{3,}", t.lower()) if w not in STOP}


def sim(a, b):
    return len(a & b) / max(1, len(a | b))


def score(arts, now):
    """Relevanz 0-100 aus Aktualität, Anzahl Quellen und Quellen-Priorität (Gewichte oben)."""
    newest = max(a["t"] for a in arts)
    rec = max(0.0, 1 - (now - newest).total_seconds() / 3600 / MAX_AGE_H)
    n = len({a["source"] for a in arts})
    prio = max(a["prio"] for a in arts) / 10
    return round(100 * (W["recency"] * rec + W["sources"] * min(n, 4) / 4 + W["priority"] * prio))


def llm_summary(arts, key):
    items = "\n".join(f"- {a['source']}: {a['title']} - {a['desc']}" for a in arts[:5])
    prompt = ("Fasse dieses Nachrichtenereignis neutral auf Deutsch in 2-4 Sätzen zusammen und erkläre in 1-2 Sätzen, "
              "warum es wichtig ist. Nutze nur die Angaben unten, erfinde nichts, kennzeichne Unklares. "
              'Antworte nur als JSON: {"sum": "...", "why": "..."}\n\n' + items)
    body = json.dumps({"model": os.environ.get("AI_MODEL", "claude-haiku-5-5"), "max_tokens": 400,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body,
                                 {"content-type": "application/json", "x-api-key": key,
                                  "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = json.load(r)["content"][0]["text"]
    d = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    return {"sum": str(d["sum"]), "why": str(d.get("why", ""))}


def load_cache():
    try:
        return json.load(open("summary_cache.json", encoding="utf-8"))
    except Exception:
        return {}


def main():
    cfg = json.load(open("sources.json", encoding="utf-8"))
    now = datetime.now(timezone.utc)
    arts, status = [], []
    for cat in cfg["categories"]:
        for s in cat["sources"]:
            if not s.get("active", True):
                continue
            st = {"source": s["name"], "status": "ok"}
            try:
                feed = s.get("rss") or discover(s["url"])
                if not feed:
                    raise RuntimeError("kein RSS-Feed gefunden")
                if not allowed(feed):
                    raise RuntimeError("robots.txt verbietet den Abruf")
                for title, link, desc, date in parse_feed(get(feed))[:MAX_PER_FEED]:
                    t = parse_date(date) or now
                    if not title or not link or (now - t).total_seconds() > MAX_AGE_H * 3600:
                        continue
                    arts.append(dict(cat=cat["id"], source=s["name"], prio=s.get("priority", 5),
                                     title=html.unescape(title), url=link, desc=clean(desc), t=t))
            except Exception as e:
                st.update(status="error", error=str(e)[:120])
            status.append(st)

    events = []
    for cat in cfg["categories"]:
        pool = sorted((a for a in arts if a["cat"] == cat["id"]), key=lambda a: a["t"], reverse=True)
        seen, clusters = set(), []
        for a in pool:
            if a["url"] in seen:
                continue
            seen.add(a["url"])
            tk = tokens(a["title"])
            for c in clusters:
                if sim(tk, c["tk"]) >= SIM and a["source"] not in {x["source"] for x in c["arts"]}:
                    c["arts"].append(a)
                    break
            else:
                clusters.append({"tk": tk, "arts": [a]})
        for c in clusters:
            group = sorted(c["arts"], key=lambda a: a["t"])
            best = max(group, key=lambda a: a["prio"])
            rel = score(group, now)
            events.append({"id": hashlib.sha1(group[0]["url"].encode()).hexdigest()[:10], "cat": cat["id"],
                           "title": best["title"], "sum": best["desc"] or best["title"], "why": "", "ai": False,
                           "rel": rel, "breaking": rel >= cfg.get("breaking_min", 90),
                           "src": [[a["source"], a["url"]] for a in group],
                           "updated": max(a["t"] for a in group).isoformat(), "_arts": group})

    key, cache, calls = os.environ.get("ANTHROPIC_API_KEY"), load_cache(), 0
    for ev in sorted(events, key=lambda e: -e["rel"]):
        if ev["id"] in cache:                       # schon zusammengefasst: wiederverwenden
            ev.update(cache[ev["id"]], ai=True)
        elif key and calls < MAX_LLM:               # nur neue Ereignisse an die KI schicken
            try:
                cache[ev["id"]] = llm_summary(ev["_arts"], key)
                ev.update(cache[ev["id"]], ai=True)
                calls += 1
            except Exception as e:
                print("KI-Zusammenfassung fehlgeschlagen:", e, file=sys.stderr)
    ids = {e["id"] for e in events}
    json.dump({k: v for k, v in cache.items() if k in ids}, open("summary_cache.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    for ev in events:
        ev.pop("_arts")
    events.sort(key=lambda e: -e["rel"])
    out = {"generated": now.isoformat(), "categories": [{"id": c["id"], "name": c["name"]} for c in cfg["categories"]],
           "events": events, "sources": status}
    json.dump(out, open("news.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    errs = [s for s in status if s["status"] == "error"]
    print(f"{len(arts)} Artikel, {len(events)} Ereignisse, {len(errs)} Quellen mit Fehlern, {calls} KI-Aufrufe")


if __name__ == "__main__":
    main()
