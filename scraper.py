#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🏠 Kleinanzeigen-Scraper für GitHub Actions
============================================
Beschreibung:
  - Wird alle 30 Minuten via GitHub Actions Cron gestartet
  - Prüft auf neue Immobilien-Anzeigen in Freiburg
  - Sendet Benachrichtigung per Telegram bei neuen Anzeigen
  - Persistiert gesehene Anzeigen-IDs via GitHub Cache (kein Artifact!)

⚠️ WICHTIG: 
   - Dieser Code läuft NICHT als Dauer-Loop!
   - Er wird von GitHub Actions gestartet, führt einen einzigen 
     Prüfdurchlauf aus und beendet sich wieder.
   - Secrets (Token) kommen aus GitHub Secrets (Production) 
     ODER aus .env Datei (lokales Testen).
   - Die Datei `seen_ads.json` wird via GitHub Cache zwischen 
     Runs persistiert – kein manuelles Upload/Download nötig!
"""

import requests
import json
import os
import html
import sys
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path

# =============================================================================
# Optional: python-dotenv für lokales Testen laden
# =============================================================================
# Installation: pip install python-dotenv
# Zweck: Ermöglicht lokale Tests mit .env Datei statt GitHub Secrets
try:
    from dotenv import load_dotenv
    load_dotenv()  # Lädt .env Datei im Projektordner (nur lokal!)
except ImportError:
    # dotenv nicht installiert = läuft auf GitHub Actions (kein Problem)
    # GitHub Actions nutzt automatisch Umgebungsvariablen aus Secrets
    pass


# =============================================================================
# 📋 KONFIGURATION
# =============================================================================

# URL der Kleinanzeigen-Suchseite
# Priorität: 1. GitHub Secret → 2. .env Datei → 3. Default-Wert
# WICHTIG: Falls Secret existiert aber leer ist, wird Default verwendet
KLEINANZEIGEN_URL = os.getenv("KLEINANZEIGEN_URL", "").strip()
if not KLEINANZEIGEN_URL:
    KLEINANZEIGEN_URL = "https://www.kleinanzeigen.de/s-wohnung-kaufen/freiburg-im-breisgau/sortierung:neuste/preis::250000/c196l9354r30"

# Telegram Bot Token (von @BotFather)
# Wird geladen aus: GitHub Secrets (Production) ODER .env (lokal)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Telegram Chat-ID (numerisch)
# Wird geladen aus: GitHub Secrets (Production) ODER .env (lokal)
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Datei zum Speichern bereits gesehener Anzeigen-IDs
# WICHTIG: Diese Datei wird via GitHub Cache zwischen Runs persistiert!
# - Kein manuelles Upload/Download nötig
# - Cache wird automatisch am Job-Ende gespeichert
SEEN_FILE = "seen_ads.json"

# Log-Datei (wird als Artifact hochgeladen für Debugging)
LOG_FILE = "scraper.log"

# User-Agent: Damit wir wie ein normaler Browser erscheinen
# Verhindert Blockierung durch Kleinanzeigen-Bot-Erkennung
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# =============================================================================


def log_message(message, level="INFO"):
    """
    Schreibt eine Nachricht in die Konsole und in die Log-Datei.
    
    Args:
        message (str): Die Nachricht, die geloggt werden soll
        level (str): Log-Level (INFO, WARNING, ERROR)
    
    Returns:
        None
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    output = f"[{timestamp}] [{level}] {message}"
    print(output)  # Wird in GitHub Actions Console angezeigt
    
    # In Log-Datei schreiben (für Debugging & späteres Artifact-Upload)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(output + "\n")
    except Exception as e:
        print(f"[WARN] Konnte nicht in Log schreiben: {e}")


def load_seen_ads():
    """
    Lädt bereits gesehene Anzeigen-IDs aus der JSON-Datei.
    
    WICHTIG: Diese Datei wird via GitHub Cache bereitgestellt!
    - Beim ersten Run: Datei existiert nicht → leere Liste
    - Bei folgenden Runs: Cache wird automatisch geladen
    
    Returns:
        set: Menge von bereits gesehene Anzeigen-IDs (Strings)
    """
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                seen_ids = set(json.load(f))
            log_message(f"✅ {len(seen_ids)} bekannte Anzeigen-IDs aus Cache geladen")
            return seen_ids
        except json.JSONDecodeError as e:
            # Fallback bei korrupter JSON-Datei
            log_message(f"❌ JSON-Fehler in {SEEN_FILE}: {e} - Starte mit leerer Liste", "ERROR")
        except Exception as e:
            log_message(f"⚠️ Fehler beim Laden des Caches: {e}", "WARNING")
    else:
        # Erwartet beim ersten Run oder wenn Cache expired ist
        log_message("ℹ️ Keine seen_ads.json gefunden (erster Run oder Cache expired)")
    
    return set()


def save_seen_ads(seen_ids):
    """
    Speichert gesehene Anzeigen-IDs in die JSON-Datei.
    
    WICHTIG: Diese Datei wird via GitHub Cache automatisch persistiert!
    - Kein manuelles Upload nötig
    - Cache wird am Ende des GitHub Actions Jobs automatisch gespeichert
    
    Args:
        seen_ids (set): Menge von Anzeigen-IDs zum Speichern
    
    Returns:
        bool: True bei Erfolg, False bei Fehler
    """
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_ids), f, ensure_ascii=False, indent=2)
        
        # Prüfen, ob Datei wirklich erstellt wurde
        if os.path.exists(SEEN_FILE):
            file_size = os.path.getsize(SEEN_FILE)
            log_message(f"💾 {len(seen_ids)} IDs gespeichert ({file_size} Bytes) - Wird via Cache persistiert")
            return True
        else:
            log_message("❌ Datei wurde nicht erstellt!", "ERROR")
            return False
    except Exception as e:
        log_message(f"❌ Fehler beim Speichern: {e}", "ERROR")
        return False


def fetch_listings(url):
    """
    Holt den HTML-Inhalt der Kleinanzeigen-Suchseite.
    
    Args:
        url (str): Die URL der Suchseite
    
    Returns:
        str or None: HTML-Inhalt der Seite, oder None bei Fehler
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    
    try:
        log_message(f"📡 Request an Kleinanzeigen...")
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()  # Wirft Fehler bei 4xx/5xx Status-Codes
        log_message(f"✅ Seite erfolgreich geladen ({len(response.text)} Bytes)")
        return response.text
    except requests.RequestException as e:
        log_message(f"❌ Request fehlgeschlagen: {e}", "ERROR")
        return None


def escape_html(text):
    """
    Escapt HTML-Sonderzeichen für sichere Telegram-Nachrichten.
    
    Verhindert, dass <, >, & in Titeln das HTML in Telegram kaputt machen.
    
    Args:
        text (str): Der zu escapende Text
    
    Returns:
        str: Escapeter Text, sicher für Telegram HTML-Parsing
    """
    return html.escape(str(text), quote=False)


def parse_new_ads(html_content, seen_ids):
    """
    Extrahiert neue Anzeigen aus dem HTML-Inhalt.
    
    ✅ AKTUALISIERT FÜR 2026: Passt auf die neue 'flex flex-col' Struktur.
    
    Args:
        html_content (str): HTML-String der Kleinanzeigen-Seite
        seen_ids (set): Menge bereits gesehener Anzeigen-IDs
    
    Returns:
        list: Liste von Dicts mit Anzeigen-Daten
    """
    soup = BeautifulSoup(html_content, "html.parser")
    new_ads = []
    
    # Alle Anzeigen-Artikel finden (Haupt-Container)
    articles = soup.find_all("article", class_="aditem")
    log_message(f"🔍 {len(articles)} Anzeigen-Elemente gefunden")
    
    for i, article in enumerate(articles):
        try:
            # 🆔 ANZEIGEN-ID extrahieren (UNABHÄNGIG VON POSITION!)
            # Dies ist der Schlüssel zur Deduplizierung:
            # - data-adid ist eine feste, interne ID von Kleinanzeigen
            # - Ändert sich nie, egal ob Anzeige oben/unten/Seite 1/Seite 5 steht
            ad_id = article.get("data-adid") or article.get("id", "").replace("ad-", "")
            
            # Validierung: Nur numerische IDs akzeptieren
            if not ad_id or not ad_id.isdigit():
                log_message(f"⚠️ Ungültige ad_id: '{ad_id}' - Überspringe Anzeige", "WARNING")
                continue
            
            # Bereits gesehene Anzeigen überspringen (Deduplizierung!)
            if ad_id in seen_ids:
                continue
            
            # 📝 TITEL extrahieren
            # Neue Struktur 2026: <h3><a href="...">Titel</a></h3>
            title_link = article.find("h3").find("a") if article.find("h3") else article.find("a", href=True)
            title = title_link.get_text(strip=True) if title_link else "Kein Titel"
            
            # 🔗 URL extrahieren
            link = None
            if title_link and title_link.get("href"):
                href = title_link["href"]
                # Relative URLs zu absoluten URLs konvertieren
                if href.startswith("/"):
                    link = f"https://www.kleinanzeigen.de{href}"
                else:
                    link = href
            
            # 💰 PREIS extrahieren
            # Neue Struktur 2026: <p class="... text-title3 ...">1.174.999 € VB</p>
            price = "Preis nicht angegeben"
            
            # Versuch 1: Spezifische Klasse für Preis (Tailwind-Utility-Klasse)
            price_elem = article.find("p", class_="text-title3")
            
            # Versuch 2: Fallback - Suche nach €-Zeichen in allen <p> oder <div>
            if not price_elem or "€" not in price_elem.get_text():
                for tag in article.find_all(["p", "div"]):
                    if "€" in tag.get_text():
                        price_elem = tag
                        break
                        
            if price_elem:
                price = price_elem.get_text(strip=True)
            
            # 📍 ORT / DETAILS extrahieren
            # Neue Struktur 2026: <p class="font-strong text-onSurfaceSubdued">196 m² · 5,5 Zi.</p>
            location = "Ort unbekannt"
            
            # Versuch 1: Spezifische Klasse für Details/Ort
            details_elem = article.find("p", class_="font-strong")
            
            # Versuch 2: Fallback - Alte Klasse oder Text nach Preis
            if not details_elem:
                details_elem = article.find("span", class_="aditem-addon")
                
            if details_elem:
                location = details_elem.get_text(strip=True)
            else:
                # Versuch 3: Letzter Ausweg - Text direkt nach dem Preis-Element
                if price_elem and hasattr(price_elem, 'find_next_sibling'):
                    next_elem = price_elem.find_next_sibling()
                    if next_elem:
                        location = next_elem.get_text(strip=True)
            
            # 📦 Anzeige zur Ergebnis-Liste hinzufügen
            new_ads.append({
                "id": ad_id,
                "title": title,
                "price": price,
                "location": location,  # Enthält z.B. "196 m² · 5,5 Zi." oder Stadtname
                "url": link,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M")
            })
            
            # 🐛 Debug-Log für die erste gefundene Anzeige (nur zur Entwicklung)
            if i == 0:
                log_message(f"📋 Erste Anzeige extrahiert:")
                log_message(f"   - ID: {ad_id}")
                log_message(f"   - Titel: {title[:50]}...")
                log_message(f"   - Preis: {price}")
                log_message(f"   - Details/Ort: {location}")
                log_message(f"   - URL: {link}")
            
        except Exception as e:
            log_message(f"⚠️ Fehler beim Parsen einer Anzeige: {e}", "WARNING")
            # Continue: Nächste Anzeige versuchen, statt gesamten Run abzubrechen
            continue
    
    return new_ads


def send_telegram_message(ad):
    """
    Sendet eine Benachrichtigung per Telegram über die neue Anzeige.
    
    Nutzt die Telegram Bot API direkt via HTTP-Request (synchron, einfach).
    
    Args:
        ad (dict): Dict mit Anzeigen-Daten (title, price, location, url, etc.)
    
    Returns:
        bool: True bei Erfolg, False bei Fehler
    """
    # Nachricht im HTML-Format für Telegram zusammenbauen
    # HTML-Tags: <b> für fett, <a> für Link, parse_mode="HTML" aktivieren
    message = f"""🏠 <b>Neue Immobilie in Freiburg!</b>

<b>{escape_html(ad['title'])}</b>
💰 {escape_html(ad['price'])}
📍 {escape_html(ad['location'])}
🕐 {ad['timestamp']}

🔗 <a href="{ad['url']}">Zur Anzeige</a>"""
    
    # Telegram Bot API Endpoint
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    # Payload für die API-Anfrage
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",  # Ermöglicht <b>, <a>, etc.
        "disable_web_page_preview": True  # Link-Vorschau deaktivieren (spart Platz)
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        result = response.json()
        
        if result.get("ok"):
            log_message(f"✅ Telegram-Nachricht gesendet: {ad['title'][:50]}...")
            return True
        else:
            error_desc = result.get("description", "Unbekannter Fehler")
            log_message(f"❌ Telegram-API-Fehler: {error_desc}", "ERROR")
            return False
            
    except requests.RequestException as e:
        log_message(f"❌ Telegram-Request-Fehler: {e}", "ERROR")
        return False
    except Exception as e:
        log_message(f"❌ Unerwarteter Fehler beim Senden: {e}", "ERROR")
        return False


def main():
    """
    Hauptfunktion: Führt einen kompletten Prüfdurchlauf durch.
    
    Ablauf:
    1. Prüft, ob Telegram-Token gesetzt sind (aus Secrets oder .env)
    2. Lädt bekannte Anzeigen-IDs aus Cache (seen_ads.json)
    3. Holt aktuelle Seite von Kleinanzeigen
    4. Parst neue Anzeigen & filtert bekannte IDs
    5. Sendet Benachrichtigungen für neue Anzeigen
    6. Speichert aktualisierte Liste → wird via Cache automatisch persistiert
    """
    # 🔐 WICHTIG: Prüfen, ob Secrets geladen wurden
    # Funktioniert sowohl auf GitHub Actions (Secrets) als auch lokal (.env)
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ ERROR: Telegram credentials not found!")
        print(f"   TELEGRAM_TOKEN: {'✅ SET' if TELEGRAM_TOKEN else '❌ MISSING'}")
        print(f"   TELEGRAM_CHAT_ID: {'✅ SET' if TELEGRAM_CHAT_ID else '❌ MISSING'}")
        print("\n💡 Lösung:")
        print("   - Lokal: Erstelle .env Datei mit TELEGRAM_TOKEN und TELEGRAM_CHAT_ID")
        print("   - GitHub Actions: Trage Secrets unter Settings → Secrets → Actions ein")
        sys.exit(1)
    
    # Logging-Header für besseren Überblick in den Logs
    log_message("=" * 60)
    log_message(f"🚀 Kleinanzeigen-Scraper gestartet um {datetime.now()}")
    log_message("=" * 60)
    log_message(f"📍 URL: {KLEINANZEIGEN_URL[:80]}...")
    
    # Schritt 1: Bekannte Anzeigen-IDs laden (aus Cache!)
    seen_ids = load_seen_ads()
    
    # Schritt 2: HTML-Inhalt von Kleinanzeigen holen
    html_content = fetch_listings(KLEINANZEIGEN_URL)
    if not html_content:
        log_message("⚠️ Konnte Seite nicht laden, breche ab", "WARNING")
        sys.exit(0)  # Kein harter Fehler, nur Warning → GitHub Actions markiert als "success"
    
    # Schritt 3: Neue Anzeigen extrahieren & bekannte filtern
    new_ads = parse_new_ads(html_content, seen_ids)
    
    # Schritt 4: Benachrichtigungen für neue Anzeigen senden
    if new_ads:
        log_message(f"🎉 {len(new_ads)} neue Anzeige(n) gefunden!")
        
        for ad in new_ads:
            # Benachrichtigung senden
            success = send_telegram_message(ad)
            if success:
                # Nur bei Erfolg zur "gesehen"-Liste hinzufügen
                # Falls Telegram fehlschlägt: Anzeige beim nächsten Run nochmal versuchen
                seen_ids.add(ad["id"])
        
        # Schritt 5: Aktualisierte Liste speichern → wird via Cache persistiert!
        save_seen_ads(seen_ids)
    else:
        log_message("ℹ️ Keine neuen Anzeigen gefunden")
    
    # Abschluss-Logging
    log_message("✅ Scraper-Durchlauf erfolgreich abgeschlossen")
    log_message("=" * 60)


# =============================================================================
# Programm-Einstiegspunkt
# =============================================================================
if __name__ == "__main__":
    try:
        main()
        sys.exit(0)  # ✅ Erfolg → GitHub Actions: grüner Haken
    except Exception as e:
        print(f"❌ Unhandled exception: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)  # ❌ Fehler → GitHub Actions: rotes X + Stacktrace in Logs