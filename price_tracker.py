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
    # Affichage des informations générales de la page reçue
    try:
        page_url = page.url
        page_title = page.title()
        log.info("--- Analyse de la page ---")
        log.info("URL actuelle : %s", page_url)
        log.info("Titre de la page : %s", page_title)
    except Exception as e:
        log.warning("Impossible de récupérer l'URL ou le titre de la page : %s", e)

    # 1. Test du sélecteur personnalisé
    if custom_selector:
        try:
            log.info("Tentative avec le sélecteur personnalisé : %s", custom_selector)
            el = page.query_selector(custom_selector)
            if el:
                text = el.inner_text()
                log.info("Élément trouvé pour le sélecteur personnalisé. Texte brut : '%s'", text.strip())
                price = parse_price(text)
                if price is not None:
                    log.info("Prix extrait avec succès via le sélecteur personnalisé : %s", price)
                    return price
                else:
                    log.warning("Impossible de parser le prix à partir du texte : '%s'", text.strip())
            else:
                log.info("Aucun élément trouvé pour le sélecteur personnalisé : %s", custom_selector)
        except Exception as exc:
            log.warning("Sélecteur personnalisé inutilisable : %s", exc)

    # 2. Recherche dans les scripts JSON-LD
    log.info("Recherche du prix dans les scripts JSON-LD...")
    json_scripts = page.query_selector_all('script[type="application/ld+json"]')
    log.info("Nombre de scripts JSON-LD trouvés dans la page : %d", len(json_scripts))
    
    for i, script in enumerate(json_scripts):
        try:
            script_text = script.inner_text()
            data = json.loads(script_text)
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
                            raw_val = offers.get(key)
                            if raw_val is not None:
                                price = parse_price(str(raw_val))
                                if price is not None:
                                    log.info("Prix trouvé via JSON-LD (script #%d, clé '%s') : %s", i, key, price)
                                    return price
        except (ValueError, TypeError, AttributeError):
            continue

    # 3. Recherche dans les balises meta OpenGraph / Product
    log.info("Recherche du prix dans les balises meta...")
    meta = page.query_selector(
        'meta[property="product:price:amount"], meta[property="og:price:amount"]'
    )
    if meta:
        prop = meta.get_attribute("property")
        content = meta.get_attribute("content")
        log.info("Balise meta trouvée (property='%s'). Contenu : '%s'", prop, content)
        price = parse_price(content)
        if price is not None:
            log.info("Prix extrait avec succès via la balise meta : %s", price)
            return price
    else:
        log.info("Aucune balise meta de prix trouvée.")

    # 4. Recherche via les sélecteurs de secours (fallback)
    log.info("Recherche du prix via les sélecteurs de secours...")
    for selector in (
        '[class*="price-default--current"]',
        '[class*="price--current"]',
        '[class*="es--wrap"]',
        '[class*="Price"] [class*="current"]',
    ):
        try:
            elements = page.query_selector_all(selector)
            log.info("Sélecteur de secours '%s' : %d élément(s) trouvé(s)", selector, len(elements))
            for el in elements:
                text = el.inner_text()
                log.info(" -> Test du texte de l'élément : '%s'", text.strip())
                price = parse_price(text)
                if price is not None:
                    log.info("Prix extrait avec succès via le sélecteur '%s' : %s", selector, price)
                    return price
        except Exception as exc:
            log.debug("Erreur lors de l'évaluation du sélecteur '%s' : %s", selector, exc)
            continue

    log.warning("Aucun prix n'a pu être extrait de la page par l'ensemble des méthodes.")
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
            context = browser.new_context(
                locale=LOCALE,
                timezone_id="Europe/Paris",
                geolocation={"latitude": 48.8566, "longitude": 2.3522},
                permissions=["geolocation"],
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
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
                    page.wait_for_timeout(2500)
                    price = extract_price(page, custom_selector)
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
