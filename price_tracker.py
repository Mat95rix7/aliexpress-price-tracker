
"""
AliExpress Price Tracker — GitHub Actions
- Preserve les URL originales de watchlist.json.
- Journalise les navigations, redirections et erreurs.
- Bloque les redirections de navigation vers aliexpress.us
  si l'URL originale ne pointe pas déjà vers ce domaine.
- Extrait les prix et leur devise lorsqu'elle est disponible.
- Sauvegarde l'historique dans state.json.
- Envoie les notifications ntfy en cas de baisse comparable.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

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
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("price_tracker")

def test_original_url(url):
    log.info("=== TEST HTTP SANS SUIVRE LES REDIRECTIONS ===")
    log.info("URL demandée : %s", url)

    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "fr-FR,fr;q=0.9",
            },
            allow_redirects=False,
            timeout=20,
        )

        log.info("Statut HTTP : %s", response.status_code)
        log.info("URL de la réponse : %s", response.url)
        log.info(
            "En-tête Location : %s",
            response.headers.get("Location", "(aucune redirection)"),
        )

    except requests.RequestException as exc:
        log.error("ÉCHEC DU TEST HTTP | %s", exc)


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
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def load_watchlist():
    data = load_json(WATCHLIST, [])

    if not isinstance(data, list):
        log.error("CONFIGURATION INVALIDE | watchlist.json doit être une liste.")
        return []

    items = []

    for item in data:
        if isinstance(item, str):
            item = {"url": item}

        if not isinstance(item, dict):
            log.warning("ENTRÉE IGNORÉE | Produit invalide.")
            continue

        url = item.get("url")

        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            log.warning("ENTRÉE IGNORÉE | URL absente ou invalide.")
            continue

        parsed = urlparse(url)

        if not parsed.hostname:
            log.warning("ENTRÉE IGNORÉE | Domaine invalide : %s", url)
            continue

        if parsed.scheme != "https":
            log.warning("URL HTTP | Une URL HTTPS est préférable : %s", url)

        items.append(item)

    return items


def parse_price(text):
    if not text:
        return None

    match = re.search(r"\d[\d\s\u00a0\u202f.,]*", str(text))

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


def extract_currency(page):
    """Recherche la devise déclarée dans les métadonnées de la page."""

    for selector in (
        'meta[property="product:price:currency"]',
        'meta[property="og:price:currency"]',
        'meta[itemprop="priceCurrency"]',
    ):
        try:
            element = page.query_selector(selector)
            if element:
                value = (
                    element.get_attribute("content")
                    or element.get_attribute("value")
                )
                if value:
                    log.info("DEVISE MÉTA | %s | %s", selector, value)
                    return value.strip().upper()
        except Exception as exc:
            log.debug("Erreur lecture devise meta : %s", exc)

    for script in page.query_selector_all(
        'script[type="application/ld+json"]'
    ):
        try:
            data = json.loads(script.inner_text())
        except (ValueError, TypeError):
            continue

        objects = data if isinstance(data, list) else [data]

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            candidates = [obj]
            graph = obj.get("@graph", [])

            if isinstance(graph, list):
                candidates.extend(
                    entry for entry in graph if isinstance(entry, dict)
                )

            for candidate in candidates:
                offers = candidate.get("offers", [])

                if isinstance(offers, dict):
                    offers = [offers]

                if isinstance(offers, list):
                    for offer in offers:
                        if isinstance(offer, dict):
                            currency = offer.get("priceCurrency")
                            if currency:
                                log.info(
                                    "DEVISE JSON-LD | %s", currency
                                )
                                return str(currency).upper()

    log.warning("DEVISE INCONNUE | Impossible de confirmer la devise.")
    return None


def extract_price(page, custom_selector=""):
    log.info("--- ANALYSE DE LA PAGE ---")
    log.info("URL actuellement affichée : %s", page.url)

    try:
        log.info("Titre : %s", page.title())
    except Exception:
        pass

    if custom_selector:
        try:
            element = page.query_selector(custom_selector)

            if element:
                raw = element.inner_text().strip()
                log.info("PRIX SÉLECTEUR PERSONNALISÉ | %s", raw)

                price = parse_price(raw)
                if price is not None:
                    return price
        except Exception as exc:
            log.warning("SÉLECTEUR PERSONNALISÉ | %s", exc)

    log.info("Recherche du prix dans JSON-LD...")

    for script in page.query_selector_all(
        'script[type="application/ld+json"]'
    ):
        try:
            data = json.loads(script.inner_text())
        except (ValueError, TypeError):
            continue

        objects = data if isinstance(data, list) else [data]

        for obj in objects:
            if not isinstance(obj, dict):
                continue

            candidates = [obj]
            graph = obj.get("@graph", [])

            if isinstance(graph, list):
                candidates.extend(
                    entry for entry in graph if isinstance(entry, dict)
                )

            for candidate in candidates:
                offers = candidate.get("offers", [])

                if isinstance(offers, dict):
                    offers = [offers]

                if not isinstance(offers, list):
                    continue

                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    for key in ("price", "lowPrice"):
                        raw = offer.get(key)

                        if raw is not None:
                            price = parse_price(str(raw))

                            if price is not None:
                                log.info(
                                    "PRIX JSON-LD | clé=%s | valeur=%s",
                                    key,
                                    raw,
                                )
                                return price

    for selector in (
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
    ):
        try:
            element = page.query_selector(selector)

            if element:
                raw = element.get_attribute("content")
                log.info("PRIX META | %s | %s", selector, raw)

                price = parse_price(raw)
                if price is not None:
                    return price
        except Exception:
            pass

    for selector in (
        '[class*="price-default--current"]',
        '[class*="price--current"]',
        '[class*="es--wrap"]',
        '[class*="Price"] [class*="current"]',
    ):
        try:
            elements = page.query_selector_all(selector)
            log.info(
                "SELECTEUR SECOURS | %s | %d élément(s)",
                selector,
                len(elements),
            )

            for element in elements:
                raw = element.inner_text().strip()
                log.info("TEXTE PRIX CANDIDAT | %s", raw[:200])

                price = parse_price(raw)

                if price is not None:
                    return price
        except Exception as exc:
            log.debug("Erreur sélecteur %s : %s", selector, exc)

    log.warning("PRIX INTROUVABLE | Aucune méthode n'a réussi.")
    return None


def notify(title, message, url):
    if not NTFY_TOPIC:
        log.error("NTFY NON CONFIGURÉ | Secret NTFY_TOPIC absent.")
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

        log.error(
            "NTFY EN ÉCHEC | HTTP %s | %s",
            response.status_code,
            response.text[:200],
        )

    except requests.RequestException as exc:
        log.error("NTFY INJOIGNABLE | %s", exc)

    return False


def diagnose_page(page, name, status):
    try:
        content = (
            page.title()
            + "\n"
            + page.locator("body").inner_text(timeout=5000)[:10000]
        ).lower()
    except Exception:
        content = ""

    if status == 403:
        reason = "HTTP 403 : accès refusé."
    elif status == 429:
        reason = "HTTP 429 : trop de requêtes."
    elif status and status >= 500:
        reason = f"HTTP {status} : erreur serveur."
    elif any(
        term in content
        for term in (
            "captcha",
            "verify you are human",
            "security verification",
            "verification required",
        )
    ):
        reason = "CAPTCHA ou vérification de sécurité."
    elif any(
        term in content
        for term in ("access denied", "request blocked", "unusual traffic")
    ):
        reason = "Accès bloqué par le site."
    else:
        reason = "Prix introuvable ou page incomplète."

    log.warning("[%s] DIAGNOSTIC | %s", name, reason)


def main():
    state = load_json(STATE, {})

    if not isinstance(state, dict):
        log.warning("STATE INVALIDE | Réinitialisation en mémoire.")
        state = {}

    items = load_watchlist()

    if not items:
        log.error("Aucun produit valide dans watchlist.json.")
        return

    log.info("=" * 65)
    log.info("DÉMARRAGE | %d produit(s)", len(items))
    log.info("LOCALE | %s", LOCALE)
    log.info("=" * 65)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        try:
            context = browser.new_context(
                locale=LOCALE,
                timezone_id="Europe/Paris",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                },
            )

            page = context.new_page()
            page.set_default_timeout(10000)

            # État actualisé avant chaque navigation.
            guard = {
                "original_url": "",
                "original_host": "",
                "blocked": False,
            }

            def handle_route(route):
                request = route.request
                request_url = request.url

                if request.is_navigation_request():
                    target_host = (urlparse(request_url).hostname or "").lower()
                    original_host = guard["original_host"]

                    log.info(
                        "NAVIGATION DEMANDÉE | type=%s | URL=%s",
                        request.resource_type,
                        request_url,
                    )

                    # Ne bloque que les navigations vers .us,
                    # et seulement si l'URL originale n'était pas déjà .us.
                    if (
                        target_host == "aliexpress.us"
                        or target_host.endswith(".aliexpress.us")
                    ) and not (
                        original_host == "aliexpress.us"
                        or original_host.endswith(".aliexpress.us")
                    ):
                        guard["blocked"] = True
                        log.error(
                            "REDIRECTION BLOQUÉE | originale=%s | cible=%s",
                            guard["original_url"],
                            request_url,
                        )
                        route.abort()
                        return

                route.continue_()

            page.route("**/*", handle_route)

            def on_request_failed(request):
                if request.is_navigation_request():
                    log.warning(
                        "NAVIGATION ÉCHOUÉE | URL=%s | erreur=%s",
                        request.url,
                        request.failure,
                    )

            def on_response(response):
                request = response.request

                if request.is_navigation_request():
                    log.info(
                        "RÉPONSE NAVIGATION | HTTP %s | URL=%s",
                        response.status,
                        response.url,
                    )

                    previous = request.redirected_from

                    if previous:
                        log.warning(
                            "REDIRECTION HTTP | %s -> %s",
                            previous.url,
                            request.url,
                        )

                    try:
                        location = response.headers.get("location")
                        if location:
                            log.info(
                                "EN-TÊTE LOCATION | %s", location
                            )
                    except Exception:
                        pass

            page.on("requestfailed", on_request_failed)
            page.on("response", on_response)

            for index, item in enumerate(items, start=1):
                # URL source immuable pour ce produit.
                original_url = item["url"]
                name = item.get("name") or original_url[-30:]
                custom_selector = item.get("price_selector", "")

                parsed_original = urlparse(original_url)
                original_host = (parsed_original.hostname or "").lower()

                guard["original_url"] = original_url
                guard["original_host"] = original_host
                guard["blocked"] = False

                log.info("")
                log.info("=" * 65)
                log.info("PRODUIT %d/%d | %s", index, len(items), name)
                log.info("URL ORIGINALE WATCHLIST | %s", original_url)
                log.info("DOMAINE ORIGINAL | %s", original_host)
                log.info("=" * 65)

                try:
                    test_original_url(original_url)
                    response = page.goto(
                        original_url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )

                    if guard["blocked"]:
                        log.error(
                            "[%s] ANALYSE ABANDONNÉE | "
                            "Redirection vers .us bloquée.",
                            name,
                        )
                        continue

                    if response is None:
                        log.warning(
                            "[%s] Aucune réponse HTTP exploitable.", name
                        )
                        continue

                    status = response.status
                    final_url = page.url
                    final_host = (
                        urlparse(final_url).hostname or ""
                    ).lower()

                    log.info("URL ORIGINALE | %s", original_url)
                    log.info("URL FINALE VISITÉE | %s", final_url)
                    log.info("DOMAINE FINAL | %s", final_host)
                    log.info("HTTP FINAL | %s", status)

                    if final_url != original_url:
                        log.warning(
                            "URL MODIFIÉE PAR NAVIGATION | originale=%s | finale=%s",
                            original_url,
                            final_url,
                        )
                    else:
                        log.info("URL FINALE IDENTIQUE À L'ORIGINALE.")

                    if status >= 400:
                        diagnose_page(page, name, status)
                        continue

                    # Protection supplémentaire si la navigation a
                    # malgré tout atteint le domaine américain.
                    if (
                        final_host == "aliexpress.us"
                        or final_host.endswith(".aliexpress.us")
                    ) and not (
                        original_host == "aliexpress.us"
                        or original_host.endswith(".aliexpress.us")
                    ):
                        log.error(
                            "[%s] PRIX REFUSÉ | Domaine final américain.",
                            name,
                        )
                        continue

                    page.wait_for_timeout(2500)

                    currency = extract_currency(page)
                    price = extract_price(page, custom_selector)

                    if price is None:
                        diagnose_page(page, name, status)
                        continue

                    log.info(
                        "[%s] PRIX EXTRAIT | %.4f | devise=%s",
                        name,
                        price,
                        currency or "INCONNUE",
                    )

                    # L'état est indexé par l'URL originale, jamais
                    # par l'URL finale redirigée.
                    previous_data = state.get(original_url, {})
                    if not isinstance(previous_data, dict):
                        previous_data = {}

                    previous = previous_data.get("price")
                    previous_currency = previous_data.get("currency")

                    log.info(
                        "[%s] PRIX PRÉCÉDENT | %s | devise=%s",
                        name,
                        previous,
                        previous_currency or "INCONNUE",
                    )

                    same_currency = (
                        currency is not None
                        and previous_currency == currency
                    )

                    if (
                        isinstance(previous, (int, float))
                        and not isinstance(previous, bool)
                        and same_currency
                        and price < previous - 0.001
                    ):
                        drop = previous - price
                        percent = drop / previous * 100 if previous > 0 else 0

                        notify(
                            f"Baisse de prix : {name}",
                            (
                                f"Ancien prix : {previous:.2f} {currency}\n"
                                f"Nouveau prix : {price:.2f} {currency}\n"
                                f"Baisse : {drop:.2f} {currency} "
                                f"({percent:.1f} %)"
                            ),
                            original_url,
                        )

                    elif isinstance(previous, (int, float)) and not same_currency:
                        log.warning(
                            "[%s] COMPARAISON IGNORÉE | "
                            "Devise inconnue ou différente.",
                            name,
                        )

                    history = previous_data.get("history", [])
                    if not isinstance(history, list):
                        history = []

                    history.append(
                        {
                            "timestamp": int(time.time()),
                            "price": price,
                            "currency": currency,
                            "final_url": final_url,
                        }
                    )

                    state[original_url] = {
                        "price": price,
                        "currency": currency,
                        "last_final_url": final_url,
                        "history": history[-500:],
                    }

                    log.info(
                        "[%s] ÉTAT MIS À JOUR | clé originale conservée.",
                        name,
                    )

                except PlaywrightTimeoutError:
                    log.error(
                        "[%s] TIMEOUT | URL originale=%s",
                        name,
                        original_url,
                    )

                except Exception:
                    log.exception(
                        "[%s] ERREUR NAVIGATION | URL originale=%s",
                        name,
                        original_url,
                    )

                finally:
                    log.info(
                        "FIN PRODUIT | originale=%s | bloquée=%s",
                        original_url,
                        guard["blocked"],
                    )

        finally:
            browser.close()

    try:
        save_json(STATE, state)
        log.info(
            "ÉTAT SAUVEGARDÉ | %d produit(s) | fichier=%s",
            len(state),
            STATE,
        )
    except OSError:
        log.exception("SAUVEGARDE IMPOSSIBLE | state.json")

    log.info("FIN DU CYCLE")


if __name__ == "__main__":
    main()
