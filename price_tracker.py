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



def extract_price(page):
    """Détecte le prix et journalise les sources pour diagnostiquer AliExpress."""
    import json
    import re

    candidates = []

    def add_candidate(source, raw_value, priority):
        if raw_value is None:
            return

        raw_value = str(raw_value).strip()
        if not raw_value:
            return

        price = parse_price(raw_value)
        log.info(
            "[PRIX CANDIDAT] source=%s | valeur=%r | prix_parsé=%r",
            source,
            raw_value[:500],
            price,
        )

        if price is not None and price > 0:
            candidates.append({
                "source": source,
                "raw": raw_value,
                "price": price,
                "priority": priority,
            })

    log.info("=" * 60)
    log.info("--- DIAGNOSTIC COMPLET DE LA PAGE ---")

    try:
        log.info("URL actuelle : %s", page.url)
        log.info("Titre de la page : %s", page.title())
    except Exception as exc:
        log.warning("Impossible de lire les informations de page : %s", exc)

    # 1. Texte visible et détection d'une éventuelle page de contrôle
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        log.info("Longueur du texte visible : %s caractères", len(body_text))
        log.info("Début du texte visible : %r", body_text[:4000])

        access_keywords = [
            "captcha",
            "verify you are human",
            "security verification",
            "unusual traffic",
            "access denied",
            "robot check",
        ]

        found_keywords = [
            word for word in access_keywords
            if word in body_text.lower()
        ]

        if found_keywords:
            log.warning(
                "Mots-clés de contrôle d'accès détectés : %s",
                found_keywords,
            )
    except Exception as exc:
        log.warning("Lecture du texte visible impossible : %s", exc)

    # 2. Sélecteur personnalisé, s'il existe dans la configuration
    custom_selector = globals().get("CUSTOM_PRICE_SELECTOR", "")

    if custom_selector:
        log.info("Sélecteur personnalisé configuré : %r", custom_selector)

        try:
            elements = page.locator(custom_selector)
            count = elements.count()
            log.info("Éléments trouvés avec le sélecteur personnalisé : %s", count)

            for i in range(min(count, 20)):
                element = elements.nth(i)
                try:
                    log.info(
                        "[CUSTOM #%s] visible=%s | texte=%r | HTML=%s",
                        i,
                        element.is_visible(),
                        element.inner_text(timeout=1500)[:300],
                        element.evaluate("(e) => e.outerHTML")[:700],
                    )
                    if element.is_visible():
                        add_candidate(
                            f"custom_selector_{i}",
                            element.inner_text(timeout=1500),
                            100,
                        )
                except Exception as exc:
                    log.debug("Lecture élément personnalisé #%s : %s", i, exc)
        except Exception as exc:
            log.warning("Sélecteur personnalisé inutilisable : %s", exc)
    else:
        log.info("Aucun sélecteur personnalisé configuré.")

    # 3. JSON-LD : collecter les prix sans retourner prématurément
    scripts = page.locator('script[type="application/ld+json"]')
    try:
        script_count = scripts.count()
    except Exception:
        script_count = 0

    log.info("Nombre de scripts JSON-LD : %s", script_count)

    def inspect_json(data, location):
        if isinstance(data, list):
            for index, item in enumerate(data):
                inspect_json(item, f"{location}[{index}]")
            return

        if not isinstance(data, dict):
            return

        log.debug(
            "Objet JSON-LD %s : %s",
            location,
            json.dumps(data, ensure_ascii=False)[:3000],
        )

        offers = data.get("offers")

        if isinstance(offers, dict):
            offers = [offers]

        if isinstance(offers, list):
            for offer_index, offer in enumerate(offers):
                if not isinstance(offer, dict):
                    continue

                log.info(
                    "[JSON-LD OFFER] %s | devise=%r | price=%r | "
                    "lowPrice=%r | highPrice=%r | availability=%r",
                    location,
                    offer.get("priceCurrency"),
                    offer.get("price"),
                    offer.get("lowPrice"),
                    offer.get("highPrice"),
                    offer.get("availability"),
                )

                for key in ("price", "lowPrice", "highPrice"):
                    value = offer.get(key)
                    if value is not None:
                        add_candidate(
                            f"jsonld_{location}_offer{offer_index}_{key}",
                            value,
                            10 if key == "price" else 5,
                        )

        # Certaines pages placent les offres dans @graph.
        graph = data.get("@graph")
        if graph is not None:
            inspect_json(graph, f"{location}.@graph")

    for i in range(script_count):
        try:
            raw_json = scripts.nth(i).inner_text(timeout=3000)
            log.info(
                "JSON-LD script #%s | longueur=%s",
                i,
                len(raw_json),
            )
            log.info(
                "JSON-LD script #%s complet (limité à 12000 caractères) : %s",
                i,
                raw_json[:12000],
            )

            data = json.loads(raw_json)
            inspect_json(data, f"script{i}")
        except Exception as exc:
            log.warning("JSON-LD #%s illisible : %s", i, exc)

    # 4. Métadonnées de prix
    meta_selectors = [
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[property="product:price:currency"]',
        'meta[property="og:price:currency"]',
        'meta[itemprop="price"]',
        'meta[name="price"]',
    ]

    for selector in meta_selectors:
        try:
            elements = page.locator(selector)
            count = elements.count()

            for i in range(min(count, 10)):
                element = elements.nth(i)
                content = element.get_attribute("content")
                log.info(
                    "[META] sélecteur=%s | content=%r",
                    selector,
                    content,
                )

                if "currency" not in selector:
                    add_candidate(f"meta_{selector}_{i}", content, 20)
        except Exception as exc:
            log.debug("Lecture métadonnée %s : %s", selector, exc)

    # 5. Éléments DOM susceptibles de contenir le prix
    price_selectors = [
        '[class*="price-default--current"]',
        '[class*="price--current"]',
        '[class*="es--wrap"]',
        '[class*="Price"] [class*="current"]',
        '[class*="price"]',
        '[class*="Price"]',
        '[class*="amount"]',
        '[class*="sale"]',
        '[data-spm*="price"]',
        '[itemprop="price"]',
    ]

    seen = set()

    for selector in price_selectors:
        try:
            elements = page.locator(selector)
            count = elements.count()
            log.info("[DOM] sélecteur=%s | nombre=%s", selector, count)

            for i in range(min(count, 40)):
                element = elements.nth(i)

                try:
                    visible = element.is_visible()
                    text_value = element.inner_text(timeout=1000).strip()
                    html = element.evaluate("(e) => e.outerHTML")

                    # Éviter de journaliser plusieurs fois le même élément.
                    signature = (selector, html[:250])
                    if signature in seen:
                        continue
                    seen.add(signature)

                    log.info(
                        "[DOM CANDIDAT] sélecteur=%s | index=%s | "
                        "visible=%s | texte=%r | HTML=%s",
                        selector,
                        i,
                        visible,
                        text_value[:300],
                        html[:800],
                    )

                    if visible and text_value:
                        add_candidate(
                            f"dom_{selector}_{i}",
                            text_value,
                            80,
                        )

                    # L'attribut content peut porter un prix non visible.
                    content = element.get_attribute("content")
                    if content and not text_value:
                        add_candidate(
                            f"dom_content_{selector}_{i}",
                            content,
                            30,
                        )

                except Exception as exc:
                    log.debug(
                        "Erreur lecture DOM %s #%s : %s",
                        selector,
                        i,
                        exc,
                    )
        except Exception as exc:
            log.debug("Sélecteur DOM %s ignoré : %s", selector, exc)

    # 6. Lignes du texte visible qui ressemblent à des prix
    try:
        for line_number, line in enumerate(body_text.splitlines()):
            line = line.strip()
            if not line:
                continue

            if re.search(
                r"(€|EUR|US\$|\$|£|price|prix|sale|promotion)",
                line,
                re.IGNORECASE,
            ):
                log.info(
                    "[LIGNE PRIX] ligne=%s | texte=%r",
                    line_number,
                    line[:500],
                )
    except Exception as exc:
        log.debug("Analyse des lignes visibles impossible : %s", exc)

    # 7. Résumé des candidats
    log.info("--- RÉSUMÉ DES PRIX CANDIDATS ---")

    for candidate in candidates:
        log.info(
            "source=%s | prix=%s | priorité=%s | texte=%r",
            candidate["source"],
            candidate["price"],
            candidate["priority"],
            candidate["raw"][:200],
        )

    if not candidates:
        log.error("AUCUN PRIX TROUVÉ dans les sources analysées.")
        return None

    # Priorité au prix d'un élément DOM visible, puis au sélecteur
    # personnalisé, aux métadonnées et enfin au JSON-LD.
    dom_candidates = [
        c for c in candidates if c["source"].startswith("dom_")
    ]

    if dom_candidates:
        # Ne choisir que les éléments dont le texte contient un prix.
        # On ne peut pas garantir automatiquement qu'il s'agit de la
        # variante sélectionnée : les logs permettent de le vérifier.
        selected = dom_candidates[0]
    else:
        selected = max(candidates, key=lambda c: c["priority"])

    log.info(
        "PRIX RETENU : %s | source=%s | texte=%r",
        selected["price"],
        selected["source"],
        selected["raw"][:300],
    )

    return selected["price"]


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
