#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏠 Kleinanzeigen-Scraper für GitHub Actions (Playwright Edition)
============================================
Beschreibung:
  - Wird via GitHub Actions Cron gestartet
  - Nutzt Playwright, um JavaScript-Rendering und Bot-Schutz zu umgehen
  - Extrahiert Daten robust (unabhängig von CSS-Klassen-Änderungen)
  - Persistiert gesehene Anzeigen-IDs via GitHub Cache
"""

import requests
import json
import os
import html
import sys
import re
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path

# Playwright Import
from playwright.sync_api import sync_playwright

# =============================================================================
# Optional: python-dotenv für lokales Testen laden
# =============================================================================
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =============================================================================
# 📋 KONFIGURATION
# =============================================================================
KLEINANZEIGEN_URL = os.getenv("KLEINANZEIGEN_URL", "").strip()
if not KLEINANZEIGEN_URL:
    KLEINANZEIGEN_URL = "https://www.kleinanzeigen.de/s-wohnung-kaufen/freiburg-im-breisgau/sortierung:neuste/preis::250000/c196l9354r30"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SEEN_FILE = "seen_ads.json"
LOG_FILE = "scraper.log"

# =============================================================================
# 🌐 PLAYWRIGHT: HTML MIT JAVASCRIPT LADEN
# =============================================================================
def fetch_html_with_playwright(url: str) -> str:
    """Lädt die Seite mit einem echten Browser, um Bot-Schutz und JS-Rendering zu bewältigen."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="de-DE",
                timezone_id="Europe/Berlin"
            )
            page = context.new_page()
            
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            
            # Cookie-Banner akzeptieren
            try:
                page.click('button:has-text("Alle akzeptieren")', timeout=3000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            
            # Warten, bis die Anzeigen geladen sind
            try:
                page.wait_for_selector('article[data-adid]', timeout=10000)
            except Exception:
                pass # Falls keine da sind, machen wir trotzdem weiter
            
            html_content = page.content()
            browser.close()
            return html_content
            
    except Exception as e:
        print(f"[ERROR] Playwright-Fehler: {e}")
        return None

# =============================================================================
# 📝 HELPER-FUNKTIONEN
# =============================================================================
def log_message(message, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output = f"[{timestamp}] [{level}] {message}"
    print(output)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(output + "\n")
    except Exception as e:
        print(f"[WARN] Konnte nicht in Log schreiben: {e}")

def load_seen_ads():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                seen_ids = set(json.load(f))
            log_message(f"✅ {len(seen_ids)} bekannte Anzeigen-IDs aus Cache geladen")
            return seen_ids
        except Exception as e:
            log_message(f"⚠️ Fehler beim Laden des Caches: {e}", "WARNING")
    log_message("ℹ️ Keine seen_ads.json gefunden (erster Run oder Cache expired)")
    return set()

def save_seen_ads(seen_ids):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_ids), f, ensure_ascii=False, indent=2)
        log_message(f"💾 {len(seen_ids)} IDs gespeichert - Wird via Cache persistiert")
        return True
    except Exception as e:
        log_message(f"❌ Fehler beim Speichern: {e}", "ERROR")
        return False

def escape_html(text):
    return html.escape(str(text), quote=False)

# =============================================================================
# 🔍 ROBUSTES PARSING (Unabhängig von CSS-Klassen)
# =============================================================================
def parse_new_ads(html_content, seen_ids):
    soup = BeautifulSoup(html_content, "html.parser")
    new_ads = []
    
    # NEU: Suche nach article mit data-adid Attribut (statt class="aditem")
    articles = soup.find_all("article", attrs={"data-adid": True})
    log_message(f"🔍 {len(articles)} Anzeigen-Elemente gefunden")
    
    for i, article in enumerate(articles):
        try:
            ad_id = article.get("data-adid")
            if not ad_id or not str(ad_id).isdigit():
                continue
            
            if ad_id in seen_ids:
                continue
            
            # 1. TITEL & URL (Suche nach dem Hauptlink zur Anzeige)
            title = "Kein Titel"
            link = None
            link_elem = article.find("a", href=re.compile(r"/s-anzeige/"))
            if link_elem:
                href = link_elem["href"]
                link = f"https://www.kleinanzeigen.de{href}" if str(href).startswith("/") else str(href)
                title = link_elem.get_text(separator=" ", strip=True)
                if len(title) < 5:
                    heading = link_elem.find(["h2", "h3"])
                    title = heading.get_text(strip=True) if heading else title
            
            # Fallback für Titel
            if len(title) < 5:
                title = article.get_text(separator=" ", strip=True)[:100].strip() + "..."

            # 2. PREIS (Suche nach € oder VB)
            price = "Preis nicht angegeben"
            for elem in article.find_all(["p", "span", "div", "strong"]):
                text = elem.get_text(strip=True)
                if "€" in text or "VB" in text or "zu verschenken" in text.lower():
                    if len(text) < 50 and re.search(r'\d', text):
                        price = text
                        break
            
            # 3. ORT / DETAILS (Suche nach 5-stelliger PLZ oder spezifischem Text)
            location = "Ort unbekannt"
            for elem in article.find_all(["p", "span", "div"]):
                text = elem.get_text(strip=True)
                # Matcht z.B. "79106 Freiburg" oder "196 m² · 5,5 Zi."
                if re.match(r'^\d{5}(\s+.+)?$', text) or ("m²" in text and "Zi" in text):
                    location = text
                    break
            
            new_ads.append({
                "id": str(ad_id),
                "title": title,
                "price": price,
                "location": location,
                "url": link,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
            })
            
        except Exception as e:
            log_message(f"⚠️ Fehler beim Parsen einer Anzeige: {e}", "WARNING")
            continue
    
    return new_ads

# =============================================================================
# 📱 TELEGRAM
# =============================================================================
def send_telegram_message(ad):
    message = f"""🏠 <b>Neue Immobilie in Freiburg!</b>

<b>{escape_html(ad['title'])}</b>
💰 {escape_html(ad['price'])}
📍 {escape_html(ad['location'])}
🕐 {ad['timestamp']}

🔗 <a href="{ad['url']}">Zur Anzeige</a>"""
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        if result.get("ok"):
            log_message(f"✅ Telegram gesendet: {ad['title'][:40]}...")
            return True
        else:
            log_message(f"❌ Telegram-API-Fehler: {result.get('description')}", "ERROR")
            return False
    except Exception as e:
        log_message(f"❌ Telegram-Request-Fehler: {e}", "ERROR")
        return False

# =============================================================================
# 🚀 MAIN
# =============================================================================
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ ERROR: Telegram credentials not found!")
        sys.exit(1)
    
    log_message("=" * 60)
    log_message(f"🚀 Scraper gestartet um {datetime.now()}")
    log_message("=" * 60)
    
    seen_ids = load_seen_ads()
    
    log_message("📡 Starte Browser und lade Seite...")
    html_content = fetch_html_with_playwright(KLEINANZEIGEN_URL)
    
    if not html_content:
        log_message("⚠️ Konnte Seite nicht laden, breche ab", "WARNING")
        sys.exit(0)
    
    log_message(f"✅ Seite geladen ({len(html_content)} Bytes)")
    new_ads = parse_new_ads(html_content, seen_ids)
    
    if new_ads:
        log_message(f"🎉 {len(new_ads)} neue Anzeige(n) gefunden!")
        for ad in new_ads:
            if send_telegram_message(ad):
                seen_ids.add(ad["id"])
        save_seen_ads(seen_ids)
    else:
        log_message("ℹ️ Keine neuen Anzeigen gefunden")
    
    log_message("✅ Scraper-Durchlauf erfolgreich abgeschlossen")
    log_message("=" * 60)

if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as e:
        print(f"❌ Unhandled exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)