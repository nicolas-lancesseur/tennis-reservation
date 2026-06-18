#!/usr/bin/env python3
"""
Réservation automatique de terrain de tennis
Tennis Club Issy-les-Moulineaux — plateforme Premier Service
Logique :
  - Lance le mercredi à 00h01 heure de Paris
  - Réserve un terrain pour le mardi suivant à 20h00
  - Si pluie prévue l'après-midi -> terrain couvert (1-4)
  - Si pas de pluie -> terrain extérieur, préférence 7 ou 8
  - Partenaire : Anthony Martin
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_sync

# Configuration
CLUB_URL  = "https://www.premier-service.fr/_start/index.php?club=57920018"
USERNAME  = os.environ["TENNIS_USERNAME"]
PASSWORD  = os.environ["TENNIS_PASSWORD"]
PARTNER   = "Anthony Martin"
PARIS_TZ  = ZoneInfo("Europe/Paris")

LATITUDE       = 48.8234
LONGITUDE      = 2.2735
RAIN_THRESHOLD = 40

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/125.0.0.0 Safari/537.36'
)


def get_next_tuesday(from_date: datetime) -> datetime:
    days_ahead = (1 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


def check_rain_forecast(target_date: datetime) -> bool:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": "precipitation_probability",
        "timezone": "Europe/Paris",
        "forecast_days": 14,
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    target_str = target_date.strftime("%Y-%m-%d")
    afternoon = {f"{target_str}T{h:02d}:00" for h in range(14, 21)}

    max_prob = 0
    for t, p in zip(data["hourly"]["time"], data["hourly"]["precipitation_probability"]):
        if t in afternoon:
            max_prob = max(max_prob, p)

    print(f"  Probabilite de pluie maximale (14h-20h) : {max_prob}%")
    return max_prob >= RAIN_THRESHOLD


def reserve_court(target_tuesday: datetime, rain_expected: bool) -> None:
    court_order = [1, 2, 3, 4] if rain_expected else [7, 8, 5, 6, 9]
    court_type  = "couvert" if rain_expected else "exterieur"
    print(f"  Type de terrain : {court_type} - ordre : {court_order}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
            ]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        stealth_sync(page)

        # 1. Connexion
        print("  Chargement de la page de connexion...")
        page.goto(CLUB_URL, wait_until="load", timeout=30000)

        try:
            page.wait_for_url("**/ics.php**", timeout=20000)
            print(f"  Redirige vers : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  URL : {page.url}")

        # Attendre que la page soit completement chargee avant de remplir les champs.
        # Sans cela, Playwright detecte une navigation en cours et timeout sur fill().
        try:
            page.wait_for_load_state("load", timeout=15000)
            print(f"  Page chargee - URL : {page.url}")
        except PlaywrightTimeoutError:
            print("  (load timeout, on continue)")

        page.wait_for_selector('input[name="userid"]', state="attached", timeout=15000)
        print("  Formulaire detecte.")

        # Spoofing Event.prototype.isTrusted pour que fs() passe son check anti-bot.
        # fs() verifie event.isTrusted ; on override le getter avant le click.
        print("  Login atomique (isTrusted spoof + click Entrer)...")
        page.evaluate(f"""
            (function() {{
                Object.defineProperty(Event.prototype, 'isTrusted', {{
                    get: function() {{ return true; }},
                    configurable: true
                }});
                var inputs = document.querySelectorAll('input[type="text"]');
                for (var i = 0; i < inputs.length; i++) {{
                    if (inputs[i].name !== 'userid') {{ inputs[i].value = '{USERNAME}'; break; }}
                }}
                var pinputs = document.querySelectorAll('input[type="password"]');
                for (var i = 0; i < pinputs.length; i++) {{
                    if (pinputs[i].name !== 'userkey') {{ pinputs[i].value = '{PASSWORD}'; break; }}
                }}
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {{
                    if (btns[i].innerText && btns[i].innerText.indexOf('Entrer') >= 0) {{
                        btns[i].click();
                        break;
                    }}
                }}
            }})();
        """)
        print("  Login soumis.")
        time.sleep(2)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            print(f"  Post-login URL : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  (networkidle timeout) URL : {page.url}")

                # 2. Attente du planning
        print("  Attente du planning (peut prendre 20-30s)...")
        planning_loaded = False
        for i in range(90):
            if i % 15 == 0:
                try:
                    print(f"  t+{i}s URL={page.url} title={page.title()}")
                except Exception:
                    pass
            try:
                found = page.evaluate("() => document.getElementById('btn_plus') !== null")
                if found:
                    planning_loaded = True
                    break
            except Exception:
                pass
            time.sleep(1)

        if not planning_loaded:
            page.screenshot(path="apres_login.png")
            print(f"  URL erreur : {page.url}")
            try:
                print(f"  Titre : {page.title()}")
                body_text = page.locator('body').inner_text(timeout=3000)
                print(f"  Body (500 chars) : {repr(body_text[:500])}")
            except Exception as e:
                print(f"  (impossible de lire body : {e})")
            raise RuntimeError("Planning non charge apres 90s. Voir apres_login.png")

        print("  Connecte et planning charge.")

        # 3. Navigation vers le mardi cible
        now_paris       = datetime.now(PARIS_TZ)
        days_to_advance = (target_tuesday.date() - now_paris.date()).days
        print(f"  Navigation : +{days_to_advance} jour(s) vers {target_tuesday.strftime('%A %d/%m/%Y')}")

        for _ in range(days_to_advance):
            page.locator('#btn_plus').click(force=True)
            time.sleep(2)

        # 4. Selection du creneau 20h00
        booked = False
        for court_num in court_order:
            print(f"  Tentative terrain {court_num}...")
            slot_id = f"20_0_{court_num}"
            slot = page.locator(f'[id="{slot_id}"]')

            if slot.count() == 0:
                print(f"  -> id={slot_id} introuvable.")
                continue

            txt = slot.inner_text().strip()
            if txt not in ("", "20h"):
                print(f"  -> Terrain {court_num} deja reserve ({txt[:40]}), on passe.")
                continue

            print(f"  -> Terrain {court_num} disponible, clic...")
            slot.click(force=True)
            time.sleep(1)
            page.screenshot(path="apres_clic_terrain.png")
            booked = True
            print(f"  Terrain {court_num} clique.")
            break

        if not booked:
            page.screenshot(path="erreur_aucun_terrain.png")
            raise RuntimeError("Aucun terrain disponible pour 20h.")

        # 5. Formulaire de reservation
        print("  Attente du formulaire...")
        confirm_btn = None

        for i in range(10):
            time.sleep(1)
            btn = page.locator(
                '#modal_inscription button:has-text("Oui"), '
                'button:has-text("Oui, je reserve"), '
                'button:has-text("Valider"), '
                'button:has-text("Confirmer"), '
                'button:has-text("Reserver")'
            )
            if btn.count() > 0 and btn.first.is_visible():
                confirm_btn = btn.first
                print(f"  -> Bouton confirmation trouve a t+{i+1}s.")
                break

        page.screenshot(path="formulaire_resa.png")

        # 5b. Saisie du partenaire
        print("  Recherche du champ partenaire...")
        partner_fields = page.locator(
            'input[placeholder*="artenaire"], '
            'input[placeholder*="artner"], '
            'input[name*="artenaire"], '
            'input[name*="artner"]'
        )
        if partner_fields.count() == 0:
            all_text_inputs = page.locator('input[type="text"]')
            for idx in range(all_text_inputs.count()):
                inp = all_text_inputs.nth(idx)
                if inp.is_visible() and inp.get_attribute("id") != "CHAMP_SELECTEUR_JOUR":
                    partner_fields = inp
                    break

        if hasattr(partner_fields, 'count') and partner_fields.count() > 0:
            pf = partner_fields.first if hasattr(partner_fields, 'first') else partner_fields
            if pf.is_visible():
                print(f"  -> Saisie partenaire : {PARTNER}")
                pf.fill(PARTNER)
                time.sleep(1)
                try:
                    suggestion = page.locator(
                        f'li:has-text("{PARTNER}"), '
                        f'[class*="autocomplete"] :has-text("{PARTNER}"), '
                        f'[class*="suggest"] :has-text("{PARTNER}")'
                    ).first
                    if suggestion.is_visible():
                        suggestion.click(force=True)
                        print("  -> Partenaire selectionne via autocompletion.")
                except Exception:
                    print("  (pas d'autocompletion)")
        else:
            print("  (aucun champ partenaire detecte)")

        # 6. Confirmation
        if confirm_btn:
            print("  Clic confirmation...")
            confirm_btn.click(force=True)
            time.sleep(3)
        else:
            fallback = page.locator(
                'button:has-text("Oui"), '
                'button:has-text("Valider"), '
                'button:has-text("Confirmer"), '
                'button:has-text("Reserver")'
            )
            if fallback.count() > 0 and fallback.first.is_visible():
                fallback.first.click(force=True)
                time.sleep(3)
            else:
                print("  Aucun bouton confirmation - le clic terrain a peut-etre suffi.")

        page.screenshot(path="confirmation.png")
        print("  Reservation terminee. Voir confirmation.png")
        browser.close()


if __name__ == "__main__":
    now_paris = datetime.now(PARIS_TZ)
    print(f"\n=== Reservation Tennis ===")
    print(f"Heure Paris : {now_paris.strftime('%A %d/%m/%Y %H:%M')}")

    # if now_paris.weekday() != 2:
    #     print("Ce n'est pas mercredi a Paris. Abandon.")
    #     sys.exit(0)

    target_tuesday = get_next_tuesday(now_paris)
    print(f"Cible : mardi {target_tuesday.strftime('%d/%m/%Y')} a 20h00\n")

    print("Verification meteo (Open-Meteo)...")
    rain = check_rain_forecast(target_tuesday)
    print(f"  -> {'Pluie prevue : terrain couvert' if rain else 'Pas de pluie : terrain exterieur'}\n")

    reserve_court(target_tuesday, rain)
