# scrape_messenger_live.py
"""Messenger export: MODE=test|scrape. In test, run pure HTML parsers; in scrape, lazy‑import Selenium and fail clearly if SSL/driver/browser are missing. Includes pytest micro‑tests with inline fixtures."""
import os, sys, time, csv, json, shutil, re, types
from urllib.parse import quote, urlparse, parse_qs, urlencode, urlunparse
from bs4 import BeautifulSoup

# Config (env/CLI)
MODE = (os.getenv("MODE") or (sys.argv[1] if len(sys.argv)>1 else "test")).lower()
FB_USER = os.getenv("FB_USER")
FB_PASS = os.getenv("FB_PASS")
THREAD_QUERY = os.getenv("THREAD_QUERY", "Fabularzyści - Gothic Larp 2025")
THREAD_URL = os.getenv("THREAD_URL")
HEADLESS = os.getenv("HEADLESS", "1") == "1"
MAX_OLD_PAGES = int(os.getenv("MAX_OLD_PAGES", "60"))
TID_KIND_DEFAULT = os.getenv("TID_KIND", "c").lower()  # 'g' (group) | 'c' (1:1)

wait = time.sleep

# -- Lazy Selenium import to avoid ssl errors in sandboxes --
def _lazy_selenium():
    try:
        import ssl  # noqa
    except Exception as e:
        raise RuntimeError("SCRAPE wymaga modułu 'ssl'. Użyj MODE=test lub doinstaluj SSL/TLS.") from e
    import importlib
    try:
        webdriver = importlib.import_module('selenium.webdriver')
        Options = importlib.import_module('selenium.webdriver.chrome.options').Options
        Service = importlib.import_module('selenium.webdriver.chrome.service').Service
        By = importlib.import_module('selenium.webdriver.common.by').By
        Keys = importlib.import_module('selenium.webdriver.common.keys').Keys
        Wait = importlib.import_module('selenium.webdriver.support.ui').WebDriverWait
        EC = importlib.import_module('selenium.webdriver.support.expected_conditions')
        ChromeDriverManager = importlib.import_module('webdriver_manager.chrome').ChromeDriverManager
    except Exception as e:
        raise RuntimeError("Brak Selenium/webdriver_manager. Zainstaluj: pip install selenium webdriver-manager") from e
    return types.SimpleNamespace(webdriver=webdriver, Options=Options, Service=Service, By=By, Keys=Keys, Wait=Wait, EC=EC, ChromeDriverManager=ChromeDriverManager)

# -- Browser binary detection --
def _find_browser_binary():
    for p in ["/usr/bin/google-chrome","/usr/bin/chromium","/usr/bin/chromium-browser"]:
        if os.path.exists(p):
            return p
    for n in ["google-chrome","chromium","chromium-browser"]:
        p = shutil.which(n)
        if p:
            return p
    return None

# -- Driver factory --
def make_driver(S):
    opts = S.Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1200,2200")
    binloc = _find_browser_binary()
    if not binloc:
        raise RuntimeError("Nie znaleziono binarki Chrome/Chromium.")
    opts.binary_location = binloc
    sys_cd = shutil.which("chromedriver")
    service = S.Service(sys_cd) if sys_cd else S.Service(S.ChromeDriverManager().install())
    return S.webdriver.Chrome(service=service, options=opts)

# -- Small helpers --
def dismiss_overlays(driver, S):
    try:
        for locator in [
            (S.By.CSS_SELECTOR, 'button[data-cookiebanner="accept_button"]'),
            (S.By.XPATH, '//button[contains(., "Akceptuj") or contains(., "Zezwól") or contains(., "Accept")]'),
        ]:
            try:
                S.Wait(driver, 2).until(S.EC.element_to_be_clickable(locator)).click(); wait(0.2)
            except Exception:
                pass
        driver.execute_script(
            """
const b=document.querySelectorAll('[role="dialog"],[data-pagelet="root"] [style*="position: fixed"]');
b.forEach(e=>e.style.display='none');
"""
        )
    except Exception:
        pass

def safe_click(driver, S, elem):
    try:
        elem.click()
    except Exception:
        driver.execute_script("arguments[0].click();", elem)

# -- TID handling --
def parse_tid_from_url(u: str) -> str:
    if not u:
        return ""
    pr = urlparse(u)
    q = parse_qs(pr.query)
    if 'tid' in q and q['tid']:
        return q['tid'][0]
    # sometimes path carries cid.* directly under /messages/t/
    if pr.path.startswith('/messages/t/'):
        tid = pr.path.split('/messages/t/',1)[1].split('/',1)[0]
        return tid
    return ""

def build_tid(thread_id: str, preferred_kind: str = TID_KIND_DEFAULT) -> str:
    if not thread_id:
        return ""
    if thread_id.startswith('cid.'):
        return thread_id
    if 'cid.g.' in thread_id or 'cid.c.' in thread_id:
        return thread_id
    kind = 'g' if preferred_kind == 'g' else 'c'
    return f"cid.{kind}.{thread_id}"

def to_mbasic_url(any_url: str) -> str:
    if not any_url:
        return ""
    pr = urlparse(any_url)
    # normalize host → mbasic
    netloc = 'mbasic.facebook.com'
    path = pr.path
    q = parse_qs(pr.query)
    # derive tid
    existing_tid = parse_tid_from_url(any_url)
    if existing_tid:
        tid = build_tid(existing_tid)
    elif path.startswith('/messages/t/'):
        raw = path.split('/messages/t/',1)[1].split('/',1)[0]
        tid = build_tid(raw)
    else:
        # unknown form → pass through, try existing q['tid'] if present later
        tid = q.get('tid',[""])[0]
    # rebuild query preserving all params, allow repeated keys
    if tid:
        q['tid'] = [tid]
    else:
        q.pop('tid', None)
    query = urlencode(q, doseq=True)
    return urlunparse(('https', netloc, '/messages/read/', '', query, ''))

# -- Search helpers --
def find_thread_link_by_href_or_text(driver, S, query):
    links = driver.find_elements(S.By.CSS_SELECTOR, 'a[href*="/messages/read/"]')
    q = (query or "").strip().lower()
    if q:
        for a in links:
            txt = (a.text or "").strip().lower()
            aria = (a.get_attribute("aria-label") or "").strip().lower()
            title = (a.get_attribute("title") or "").strip().lower()
            if q in txt or q in aria or q in title:
                return a
    return links[0] if links else None

# -- Extractors --
def extract_messages_mbasic(html: str):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("#messageGroup") or soup
    msgs, seen = [], set()
    last_author, last_ts = "", ""
    for block in root.select("div, p, span"):
        strong = block.find("strong")
        abbr = block.find("abbr")
        if strong and not block.find("p") and not block.find("span"):
            last_author = strong.get_text(strip=True)
            if abbr and (abbr.get("title") or abbr.get_text(strip=True)):
                last_ts = abbr.get("title") or abbr.get_text(strip=True)
            continue
        text_el = None
        for sel in ["p", "span[dir='auto']", "div"]:
            t = block.select_one(sel)
            if t and t.get_text(strip=True):
                text_el = t
                break
        if not text_el and block.name in ("p", "span", "div") and block.get_text(strip=True):
            text_el = block
        if text_el:
            text = text_el.get_text(" ", strip=True)
            if not text or text.lower() in (
                "wyświetl starsze wiadomości",
                "pokaż starsze wiadomości",
                "view older messages",
            ):
                continue
            ts = last_ts
            key = (last_author, text, ts)
            if key not in seen:
                seen.add(key)
                msgs.append({"author": last_author, "text": text, "timestamp": ts})
    return msgs

def extract_messages_m(html: str):
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div[role='main']") or soup
    msgs, seen = [], set()
    last_author, last_ts = "", ""
    for c in root.select("div, p, span"):
        st = c.find("strong")
        abbr = c.find("abbr")
        if st and not c.find("p") and not c.find("span"):
            last_author = st.get_text(strip=True)
            if abbr and (abbr.get("title") or abbr.get_text(strip=True)):
                last_ts = abbr.get("title") or abbr.get_text(strip=True)
            continue
        text_el = None
        for sel in ["p", "div[dir='auto']", "span[dir='auto']", "._5w-5", "._2_1w", "._3oh-"]:
            t = c.select_one(sel)
            if t and t.get_text(strip=True):
                text_el = t
                break
        if not text_el and c.name in ("p", "span", "div") and c.get_text(strip=True):
            text_el = c
        if text_el:
            text = text_el.get_text(" ", strip=True)
            if not text:
                continue
            ts = last_ts
            key = (last_author, text, ts)
            if key not in seen:
                seen.add(key)
                msgs.append({"author": last_author, "text": text, "timestamp": ts})
    return msgs

# -- Older buttons --
def click_older_mbasic(driver, S):
    cur = driver.current_url
    cur_tid = parse_tid_from_url(cur)
    for a in driver.find_elements(S.By.CSS_SELECTOR, 'a[href*="/messages/read/"]'):
        href = (a.get_attribute("href") or "").lower(); txt = (a.text or "").strip().lower()
        if ("older" in href or "start=" in href or "starsze" in txt or "older" in txt) and (not cur_tid or cur_tid in href):
            try:
                safe_click(driver, S, a); S.Wait(driver,10).until(S.EC.presence_of_element_located((S.By.TAG_NAME,"body"))); wait(1); return True
            except Exception:
                pass
    for a in driver.find_elements(S.By.TAG_NAME, "a"):
        txt = (a.text or "").strip().lower()
        if "starsze" in txt or "older" in txt:
            try:
                safe_click(driver, S, a); S.Wait(driver,10).until(S.EC.presence_of_element_located((S.By.TAG_NAME,"body"))); wait(1); return True
            except Exception:
                pass
    return False

def click_older_m(driver, S):
    for a in driver.find_elements(S.By.CSS_SELECTOR, "a"):
        txt = (a.text or "").strip().lower(); href = (a.get_attribute("href") or "").lower()
        if "older" in txt or "starsze" in txt or "older" in href:
            try:
                safe_click(driver, S, a); S.Wait(driver,10).until(S.EC.presence_of_element_located((S.By.TAG_NAME,"body"))); wait(1); return True
            except Exception:
                pass
    return False

# -- Login & thread open --
def login_and_get_driver_on_thread(S):
    driver = make_driver(S)
    driver.get("https://mbasic.facebook.com/login"); dismiss_overlays(driver, S)
    email_el = S.Wait(driver,20).until(S.EC.visibility_of_element_located((S.By.NAME,"email")))
    pass_el  = S.Wait(driver,20).until(S.EC.visibility_of_element_located((S.By.NAME,"pass")))
    email_el.clear(); email_el.send_keys(FB_USER); pass_el.clear(); pass_el.send_keys(FB_PASS); pass_el.send_keys(S.Keys.ENTER); wait(2)
    if "login" in driver.current_url:
        dismiss_overlays(driver,S)
        try:
            btn = S.Wait(driver,5).until(S.EC.element_to_be_clickable((S.By.NAME,"login"))); safe_click(driver, S, btn)
        except Exception:
            try: driver.execute_script("document.querySelector('form').submit();")
            except Exception: pass
        wait(2)
    if THREAD_URL:
        mb_url = to_mbasic_url(THREAD_URL)
        exp_tid = parse_tid_from_url(mb_url)
        driver.get(mb_url); S.Wait(driver,10).until(S.EC.presence_of_element_located((S.By.TAG_NAME,"body"))); wait(1)
        got_tid = parse_tid_from_url(driver.current_url)
        if exp_tid and exp_tid != got_tid:
            raise RuntimeError(f"TID mismatch: expected {exp_tid}, got {got_tid}")
        return driver, "mbasic"
    driver.get("https://mbasic.facebook.com/messages/?q=" + quote(THREAD_QUERY)); S.Wait(driver,15).until(S.EC.presence_of_element_located((S.By.TAG_NAME,"body"))); wait(1)
    a = find_thread_link_by_href_or_text(driver, S, THREAD_QUERY)
    if not a:
        with open("debug_search.html","w",encoding="utf-8") as f: f.write(driver.page_source)
        driver.get("https://m.facebook.com/messages/?q=" + quote(THREAD_QUERY)); S.Wait(driver,15).until(S.EC.presence_of_element_located((S.By.TAG_NAME,"body"))); wait(1)
        for link in driver.find_elements(S.By.CSS_SELECTOR, 'a[href*="/messages/read/"]'):
            a = link; break
        if not a:
            with open("debug_search_m.html","w",encoding="utf-8") as f: f.write(driver.page_source)
            raise RuntimeError("Nie znalazłam linku do rozmowy. Zapisano debug_search*.html")
        safe_click(driver, S, a); wait(1); return driver, "m"
    safe_click(driver, S, a); wait(1); return driver, "mbasic"

# -- Scrape loop --
def run_scrape():
    if not FB_USER or not FB_PASS: raise RuntimeError("Ustaw FB_USER i FB_PASS")
    S = _lazy_selenium()
    driver, mode = login_and_get_driver_on_thread(S)
    all_msgs = []
    for i in range(MAX_OLD_PAGES):
        html = driver.page_source
        if i == 0:
            with open("thread_initial.html","w",encoding="utf-8") as f: f.write(html)
        page_msgs = extract_messages_mbasic(html) if mode=="mbasic" else extract_messages_m(html)
        seen = {(m["author"],m["text"],m["timestamp"]) for m in all_msgs}
        for m in page_msgs:
            key = (m["author"],m["text"],m["timestamp"])
            if key not in seen and m["text"]: all_msgs.append(m); seen.add(key)
        if not (click_older_mbasic(driver,S) if mode=="mbasic" else click_older_m(driver,S)): break
        wait(0.8)
    if not all_msgs:
        with open("thread_final.html","w",encoding="utf-8") as f: f.write(driver.page_source)
        try: driver.get_screenshot_as_file("thread_final.png")
        except Exception: pass
    with open("messenger_live.jsonl","w",encoding="utf-8") as f:
        for m in all_msgs: f.write(json.dumps(m, ensure_ascii=False)+"\n")
    with open("messenger_live.csv","w",newline="",encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp","author","text"]); w.writeheader(); [w.writerow(m) for m in all_msgs]
    print(f"Zaciągnięte wiadomości: {len(all_msgs)} → messenger_live.csv / messenger_live.jsonl"); driver.quit()

# ---------------- Pytest micro‑tests (inline fixtures) ----------------
MBASIC_SAMPLE = '''
<div id="messageGroup">
  <div><strong>Ala</strong><abbr title="2025-08-20 12:00">2025-08-20</abbr></div>
  <p>Cześć</p>
  <div><strong>Ola</strong></div>
  <span dir="auto">Hej!</span>
  <a href="/messages/read/?tid=cid.g.123&start=0">Wyświetl starsze wiadomości</a>
</div>'''
M_SAMPLE = '''
<div role="main">
  <div><strong>Ala</strong></div>
  <div dir="auto">Jak leci?</div>
  <div><strong>Ola</strong><abbr title="2025-08-20 12:05">12:05</abbr></div>
  <p>Ok!</p>
</div>'''

def test_extract_mbasic_counts_and_content():
    msgs = extract_messages_mbasic(MBASIC_SAMPLE)
    assert len(msgs) == 2
    assert msgs[0]["author"] == "Ala" and msgs[0]["text"] == "Cześć"
    assert msgs[1]["author"] == "Ola" and msgs[1]["text"] == "Hej!"

def test_extract_m_counts_and_content():
    msgs = extract_messages_m(M_SAMPLE)
    assert len(msgs) == 2
    assert msgs[0]["author"] == "Ala" and msgs[0]["text"].startswith("Jak leci")
    assert msgs[1]["author"] == "Ola" and msgs[1]["text"] == "Ok!"

def test_to_mbasic_url_transform_group_and_1to1_and_query():
    # group (already prefixed)
    u1 = to_mbasic_url("https://www.facebook.com/messages/t/cid.g.123?start=40&abc=1")
    pr1 = urlparse(u1); q1 = parse_qs(pr1.query)
    assert pr1.netloc == 'mbasic.facebook.com' and pr1.path == '/messages/read/'
    assert q1['tid'][0] == 'cid.g.123' and q1['start'][0] == '40' and q1['abc'][0] == '1'
    # 1:1 numeric id → defaults to cid.c
    u2 = to_mbasic_url("https://www.facebook.com/messages/t/987654321")
    assert 'tid=cid.c.987654321' in u2
    # already read/?tid stays same
    u3 = to_mbasic_url("https://m.facebook.com/messages/read/?tid=cid.c.42&foo=bar")
    assert u3.startswith("https://mbasic.facebook.com/messages/read/?") and 'tid=cid.c.42' in u3 and 'foo=bar' in u3
    # multiple query params preserved
    u4 = to_mbasic_url("https://www.facebook.com/messages/t/cid.g.123?foo=1&foo=2")
    q4 = parse_qs(urlparse(u4).query)
    assert q4['foo'] == ['1', '2']

def test_parse_tid_from_url():
    assert parse_tid_from_url("https://mbasic.facebook.com/messages/read/?tid=cid.c.42") == "cid.c.42"
    assert parse_tid_from_url("https://www.facebook.com/messages/t/cid.g.123") == "cid.g.123"

def test_deduplication():
    html = '<div id="messageGroup"><div><strong>Ala</strong></div><p>Hi</p><div><strong>Ala</strong></div><p>Hi</p></div>'
    msgs = extract_messages_mbasic(html)
    assert len(msgs) == 1 and msgs[0]["text"] == "Hi"

if __name__ == "__main__":
    if MODE == "scrape":
        run_scrape()
    elif MODE == "test":
        print("Tryb testów: uruchom 'pytest -q' aby wykonać mikro‑testy. Parsery można też użyć bezpośrednio.")
    else:
        print("Nieznany MODE. Użyj: test | scrape.")
