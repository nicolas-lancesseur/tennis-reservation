#!/usr/bin/env python3
"""
Reservation automatique de terrain de tennis
Tennis Club Issy-les-Moulineaux -- plateforme Premier Service
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_sync

CLUB_URL = "https://www.premier-service.fr/_start/index.php?club=57920018"
USERNAME = os.environ["TENNIS_USERNAME"]
PASSWORD = os.environ["TENNIS_PASSWORD"]
PARTNER = "Anthony Martin"
PARIS_TZ = ZoneInfo("Europe/Paris")
LATITUDE = 48.8234
LONGITUDE = 2.2735
RAIN_THRESHOLD = 40
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/125.0.0.0 Safari/537.36'
)

def get_next_tuesday(from_date):
    days_ahead = (1 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)

def check_rain_forecast(target_date):
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE, "longitude": LONGITUDE,
        "hourly": "precipitation_probability",
        "timezone": "Europe/Paris", "forecast_days": 14,
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

def reserve_court(target_tuesday, rain_expected):
    court_order = [1, 2, 3, 4] if rain_expected else [7, 8, 5, 6, 9]
    court_type = "couvert" if rain_expected else "exterieur"
    print(f"  Type de terrain : {court_type} - ordre : {court_order}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-setuid-sandbox']
        )
        context = browser.new_context(viewport={"width": 1366, "height": 768}, user_agent=USER_AGENT)
        page = context.new_page()
        stealth_sync(page)

        # Intercepter POST pour confirmer la soumission
        def handle_route(route, request):
            if request.method == 'POST':
                print(f"  INTERCEPT POST: {request.url}")
                try:
                    print(f"  POST data: {request.post_data}")
                except Exception as e:
                    print(f"  (post_data err: {e})")
            route.continue_()
        page.route("**/*", handle_route)

        # 1. Chargement
        print("  Chargement de la page de connexion...")
        page.goto(CLUB_URL, wait_until="load", timeout=30000)
        try:
            page.wait_for_url("**/ics.php**", timeout=20000)
            print(f"  Redirige vers : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  URL : {page.url}")
        try:
            page.wait_for_load_state("load", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_selector('input[name="userid"]', state="attached", timeout=15000)
        print("  Formulaire detecte.")

        # 2. Login : remplir largeur/hauteur ecran + fsmd5 + clic
        print("  Login avec screen dimensions + fsmd5 + click...")
        diag = page.evaluate(f"""
            (function() {{
                var f = document.querySelector('form');
                var result = {{}};

                // Remplir les dimensions ecran (check anti-bot probable)
                var le = f.querySelector('[name="largeur_ecran"]');
                var he = f.querySelector('[name="hauteur_ecran"]');
                if (le) {{ le.value = '1366'; result.leSet = true; }}
                if (he) {{ he.value = '768'; result.heSet = true; }}

                // Override form.submit pour capturer l'appel
                var origSubmit = HTMLFormElement.prototype.submit;
                result.submitCalled = false;
                HTMLFormElement.prototype.submit = function() {{
                    result.submitCalled = true;
                    var sf = {{}};
                    var els = this.querySelectorAll('input');
                    for (var i = 0; i < els.length; i++) {{
                        sf[els[i].name] = els[i].value.substring(0, 40);
                    }}
                    result.submitFields = sf;
                    origSubmit.call(this);
                }};

                // isTrusted spoof
                Object.defineProperty(Event.prototype, 'isTrusted', {{
                    get: function() {{ return true; }},
                    configurable: true
                }});

                // Remplir username (champ non-honeypot)
                var inputs = document.querySelectorAll('input[type="text"]');
                for (var i = 0; i < inputs.length; i++) {{
                    if (inputs[i].name !== 'userid') {{
                        inputs[i].value = '{USERNAME}';
                        result.userField = inputs[i].name;
                        break;
                    }}
                }}

                // Remplir password (champ non-honeypot)
                var pinputs = document.querySelectorAll('input[type="password"]');
                for (var i = 0; i < pinputs.length; i++) {{
                    if (pinputs[i].name !== 'userkey') {{
                        pinputs[i].value = '{PASSWORD}';
                        result.passField = pinputs[i].name;
                        break;
                    }}
                }}

                // Appeler fsmd5() manuellement avant le clic
                var usermd5Field = f.querySelector('[name="usermd5"]');
                result.usermd5Before = usermd5Field ? usermd5Field.value : 'n/a';
                try {{
                    fsmd5();
                    result.fsmd5Ok = true;
                }} catch(e) {{
                    result.fsmd5Err = e.message;
                }}
                result.usermd5After = usermd5Field ? usermd5Field.value.substring(0, 40) : 'n/a';

                // Lire le mot de passe apres fsmd5 (est-il transforme ?)
                if (result.passField) {{
                    var pf = f.querySelector('[name="' + result.passField + '"]');
                    result.passAfter = pf ? pf.value.substring(0, 8) : 'n/a';
                }}

                // Cliquer le bouton Entrer
                var btns = document.querySelectorAll('button');
                result.btnClicked = false;
                for (var i = 0; i < btns.length; i++) {{
                    if (btns[i].innerText && btns[i].innerText.indexOf('Entrer') >= 0) {{
                        btns[i].click();
                        result.btnClicked = true;
                        break;
                    }}
                }}

                return JSON.stringify(result);
            }})();
        """)
        print(f"  DIAG: {diag}")

        print("  Login soumis.")
        time.sleep(2)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
            print(f"  Post-login URL : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  (networkidle timeout) URL : {page.url}")

        # 3. Attente du planning
        print("  Attente du planning...")
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

        # 4. Navigation vers le mardi cible
        now_paris = datetime.now(PARIS_TZ)
        days_to_advance = (target_tuesday.date() - now_paris.date()).days
        print(f"  Navigation : +{days_to_advance} jour(s) vers {target_tuesday.strftime('%A %d/%m/%Y')}")
        for _ in range(days_to_advance):
            page.locator('#btn_plus').click(force=True)
            time.sleep(2)

        # 5. Selection creneau 20h00
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

        # 6. Formulaire de reservation
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

        # 7. Partenaire
        print("  Recherche du champ partenaire...")
        partner_fields = page.locator(
            'input[placeholder*="artenaire"], input[placeholder*="artner"], '
            'input[name*="artenaire"], input[name*="artner"]'
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

        # 8. Confirmation
        if confirm_btn:
            print("  Clic confirmation...")
            confirm_btn.click(force=True)
            time.sleep(3)
        else:
            fallback = page.locator(
                'button:has-text("Oui"), button:has-text("Valider"), '
                'button:has-text("Confirmer"), button:has-text("Reserver")'
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
