"""
AliExpress price tracker for GitHub Actions.
Reads watchlist.json, checks each product once, sends ntfy notifications on price drops,
and persists state.json between workflow runs via GitHub Actions artifacts.
"""
import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_URL = "https://ntfy.sh"
LOCALE = "fr-FR"
WATCHLIST = Path("watchlist.json")
STATE = Path("state.json")
LOG_FILE = "price_tracker.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("price_tracker")


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("JSON ILLISIBLE | %s | %s", path, exc)
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_watchlist():
    data = load_json(WATCHLIST, [])
    if not isinstance(data, list):
        log.error("CONFIGURATION INVALIDE | watchlist.json doit contenir une liste.")
        return []
    items = []
    for item in data:
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            log.warning("Entrée ignorée : produit invalide.")
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            items.append(item)
        else:
            log.warning("Entrée ignorée : URL absente ou invalide.")
    return items


def parse_price(text):
    if not text:
        return None
    match = re.search(r"\d[\d\s\u00a0\u202f.,]*", text)
    if not match:
        return None
    value = re.sub(r"[\s\u00a0\u202f]", "", match.group())
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    try:
        result = float(value)
        return result if result >= 0 else None
    except ValueError:
        return None


def extract_price(page, custom_selector=""):
    if custom_selector:
        try:
            el = page.query_selector(custom_selector)
            if el:
                price = parse_price(el.inner_text())
                if price is not None:
                    return price
        except Exception as exc:
            log.warning("Sélecteur personnalisé inutilisable : %s", exc)

    for script in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.inner_text())
            objects = data if isinstance(data, list) else [data]
            for obj in objects:
                if not isinstance(obj, dict):
                    continue
                candidates = [obj]
                graph = obj.get("@graph", [])
                if isinstance(graph, list):
                    candidates.extend(x for x in graph if isinstance(x, dict))
                for candidate in candidates:
                    offers = candidate.get("offers")
                    if isinstance(offers, list):
                        offers = offers[0] if offers else None
                    if isinstance(offers, dict):
                        for key in ("price", "lowPrice"):
                            price = parse_price(str(offers.get(key, "")))
                            if price is not None:
                                return price
        except (ValueError, TypeError, AttributeError):
            continue

    meta = page.query_selector(
        'meta[property="product:price:amount"], meta[property="og:price:amount"]'
    )
    if meta:
        price = parse_price(meta.get_attribute("content"))
        if price is not None:
            return price

    for selector in (
        '[class*="price-default--current"]',
        '[class*="price--current"]',
        '[class*="es--wrap"]',
        '[class*="Price"] [class*="current"]',
    ):
        try:
            for el in page.query_selector_all(selector):
                price = parse_price(el.inner_text())
                if price is not None:
                    return price
        except Exception:
            continue
    return None


def notify(title, message, url):
    if not NTFY_TOPIC:
        log.error("NTFY NON CONFIGURÉ | Ajoute le secret NTFY_TOPIC dans GitHub.")
        return False
    try:
        response = requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            json={
                "title": title,
                "message": message,
                "click": url,
                "priority": 4,
                "tags": ["chart_with_downwards_trend"],
            },
            timeout=15,
        )
        if response.ok:
            log.info("NOTIFICATION ENVOYÉE | %s", title)
            return True
        log.error("NTFY EN ÉCHEC | HTTP %s | %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        log.error("NTFY INJOIGNABLE | %s", exc)
    return False


def diagnose_page(page, name, status):
    try:
        content = (page.title() + "\n" + page.locator("body").inner_text(timeout=5000)[:10000]).lower()
    except Exception:
        content = ""
    if status == 403:
        reason = "HTTP 403 : accès refusé par le site."
    elif status == 429:
        reason = "HTTP 429 : trop de requêtes, laisse passer du temps avant de réessayer."
    elif status and status >= 500:
        reason = f"HTTP {status} : erreur du serveur distant."
    elif status and status >= 400:
        reason = f"HTTP {status} : erreur lors du chargement."
    elif any(x in content for x in ("captcha", "verify you are human", "security verification", "verification required")):
        reason = "VÉRIFICATION REQUISE : CAPTCHA ou contrôle d'accès probable."
    elif any(x in content for x in ("access denied", "request blocked", "unusual traffic")):
        reason = "ACCÈS BLOQUÉ : le site semble avoir refusé la requête."
    else:
        reason = "PRIX INTROUVABLE : page chargée, mais aucun prix exploitable. Vérifie la page et le sélecteur."
    log.warning("[%s] %s", name, reason)


def main():
    state = load_json(STATE, {})
    if not isinstance(state, dict):
        log.warning("state.json invalide : réinitialisation en mémoire.")
        state = {}

    items = load_watchlist()
    if not items:
        log.error("Aucun produit à suivre. Vérifie watchlist.json.")
        return

    log.info("Démarrage d'un cycle | %d produit(s)", len(items))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(locale=LOCALE)
            page = context.new_page()
            page.set_default_timeout(10000)

            for item in items:
                url = item["url"]
                name = item.get("name") or url[-30:]
                custom_selector = item.get("price_selector", "")
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    status = response.status if response else None
                    if status and status >= 400:
                        diagnose_page(page, name, status)
                        continue
                    # Attendre le chargement des données dynamiques
                page.wait_for_timeout(10000)
                
                # Réessayer la recherche du prix pendant 30 secondes maximum
                price = None
                
                for attempt in range(6):
                    price = extract_price(page, custom_selector)
                
                    if price is not None:
                        log.info(
                            "[%s] Prix trouvé à la tentative %d/6",
                            name,
                            attempt + 1
                        )
                        break
                
                    if attempt < 5:
                        log.warning(
                            "[%s] Prix introuvable, tentative %d/6. "
                            "Nouvelle tentative dans 5 secondes.",
                            name,
                            attempt + 1
                        )
                        page.wait_for_timeout(5000)
                
                if price is None:
                    diagnose_page(page, name, status)
                    continue
                except PlaywrightTimeoutError:
                    log.error("[%s] DÉLAI DÉPASSÉ | La page n'a pas chargé à temps.", name)
                    continue
                except Exception:
                    log.exception("[%s] ERREUR NAVIGATION", name)
                    continue

                previous = state.get(url, {}).get("price")
                log.info("[%s] Prix : %.2f € | Précédent : %s", name, price,
                         f"{previous:.2f} €" if isinstance(previous, (int, float)) else "aucun")

                if isinstance(previous, (int, float)) and price < previous - 0.001:
                    drop = previous - price
                    percent = drop / previous * 100 if previous > 0 else 0
                    notify(
                        f"Baisse de prix : {name}",
                        f"Ancien prix : {previous:.2f} €\n"
                        f"Nouveau prix : {price:.2f} €\n"
                        f"Baisse : {drop:.2f} € ({percent:.1f} %)",
                        url,
                    )

                history = state.get(url, {}).get("history", [])
                if not isinstance(history, list):
                    history = []
                history.append([int(time.time()), price])
                state[url] = {"price": price, "history": history[-500:]}

        finally:
            browser.close()

    try:
        save_json(STATE, state)
        log.info("État sauvegardé | %d produit(s) mémorisé(s)", len(state))
    except OSError:
        log.exception("SAUVEGARDE IMPOSSIBLE | state.json")


if __name__ == "__main__":
    main()
