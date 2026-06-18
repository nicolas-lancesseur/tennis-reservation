#!/usr/bin/env python3
"""
Réservation automatique de terrain de tennis
Tennis Club Issy-les-Moulineaux — plateforme Premier Service
Logique :
  - Lance le mercredi à 00h01 heure de Paris
  - Réserve un terrain pour le mardi suivant à 20h00
  - Si pluie prévue l'après-midi → terrain couvert (1-4)
  - Si pas de pluie → terrain extérieur, préférence 7 ou 8
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

# ── Configuration ─────────────────────────────────────────────────────────────
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


# ── Utilitaires ───────────────────────────────────────────────────────────────

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

    print(f"  Probabilité de pluie maximale (14h–20h) : {max_prob}%")
    return max_prob >= RAIN_THRESHOLD


# ── Réservation ───────────────────────────────────────────────────────────────

def reserve_court(target_tuesday: datetime, rain_expected: bool) -> None:
    court_order = [1, 2, 3, 4] if rain_expected else [7, 8, 5, 6, 9]
    court_type  = "couvert" if rain_expected else "extérieur"
    print(f"  Type de terrain : {court_type} — ordre de préférence : {court_order}")

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

        # ── 1. Connexion ───────────────────────────────────────────────────
        # Formulaire anti-bot : champs leurres (userid/userkey) + vrais champs obfusqués.
        # Stratégie : override window.fs() (appelée par le bouton Entrer) pour
        # supprimer les vérifications anti-bot, puis clic JS direct sur le bouton.
        print("  Chargement de la page de connexion...")
        page.goto(CLUB_URL, wait_until="load", timeout=30000)

        try:
            page.wait_for_url("**/ics.php**", timeout=20000)
            print(f"  Redirigé vers : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  URL : {page.url}")

        page.wait_for_selector('input[name="userid"]', state="attached", timeout=15000)
        print("  Formulaire détecté — remplissage des vrais champs...")

        # Remplir les vrais champs (non-leurres)
        real_user = page.locator('input[type="text"]:not([name="userid"])')
        real_user.fill(USERNAME)

        real_pass = page.locator('input[type="password"]:not([name="userkey"])')
        real_pass.fill(PASSWORD)

        time.sleep(0.5)

        # Remplir aussi le leurre userkey par sécurité (fsmd5 peut le lire)
        page.evaluate(f"document.querySelector('input[name=\"userkey\"]').value = '{PASSWORD}'")

        time.sleep(0.3)

        # Override window.fs() : retire les vérifications anti-bot.
        # Le bouton Entrer a onclick qui appelle fs() — notre version bypasse les checks.
        # idact et les champs fingerprinting sont déjà remplis par le site au chargement.
        page.evaluate("""
            window.fs = function() {
                var f = document.forms[0];
                if (!f) return;
                if (typeof fsmd5 === 'function') { try { fsmd5(); } catch(e) {} }
                f.submit();
            };
        """)

        # Clic JS direct sur le bouton contenant "Entrer"
        # (évite les problèmes de locator Playwright dans GitHub Actions)
        page.evaluate("""
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                if (btns[i].innerText && btns[i].innerText.indexOf('Entrer') >= 0) {
                    btns[i].click();
                    break;
                }
            }
        """)
        print("  Bouton Entrer cliqué (fs() overridée, clic JS).")

        # Laisser la navigation post-login se stabiliser
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f"  Navigation stable — URL : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  (networkidle timeout) URL : {page.url}")

        # ── 2. Attente du planning ─────────────────────────────────────────
        print("  Attente du planning (peut prendre 20-30s)...")
        planning_loaded = False
        for _ in range(90):
            try:
                found = page.evaluate(
                    "() => document.getElementById('btn_plus') !== null"
                )
                if found:
                    planning_loaded = True
                    break
            except Exception:
                pass
            time.sleep(1)

        if not planning_loaded:
            page.screenshot(path="apres_login.png")
            print(f"  URL au moment de l'erreur : {page.url}")
            try:
                print(f"  Titre : {page.title()}")
                body_text = page.locator('body').inner_text(timeout=3000)
                print(f"  Contenu page (500 chars) : {repr(body_text[:500])}")
            except Exception as e:
                print(f"  (impossible de lire body : {e})")
            raise RuntimeError("Planning non chargé après 90s. Voir apres_login.png")

        print("  Connecté et planning chargé.")

        # ── 3. Navigation vers le mardi cible ─────────────────────────────
        now_paris       = datetime.now(PARIS_TZ)
        days_to_advance = (target_tuesday.date() - now_paris.date()).days
        print(f"  Navigation : +{days_to_advance} jour(s) vers {target_tuesday.strftime('%A %d/%m/%Y')}")

        for _ in range(days_to_advance):
            page.locator('#btn_plus').click(force=True)
            time.sleep(2)

        # ── 4. Sélection du créneau 20h00 ─────────────────────────────────────
        booked = False
        for court_num in court_order:
            print(f"  Tentative terrain {court_num}...")
            slot_id = f"20_0_{court_num}"
            slot = page.locator(f'[id="{slot_id}"]')

            if slot.count() == 0:
                print(f"  → id={slot_id} introuvable.")
                continue

            txt = slot.inner_text().strip()
            if txt not in ("", "20h"):
                print(f"  → Terrain {court_num} déjà réservé ({txt[:40]}), on passe.")
                continue

            print(f"  → Terrain {court_num} disponible, clic...")
            slot.click(force=True)
            time.sleep(1)
            page.screenshot(path="apres_clic_terrain.png")
            booked = True
            print(f"  ✓ Terrain {court_num} (id={slot_id}) cliqué.")
            break

        if not booked:
            page.screenshot(path="erreur_aucun_terrain.png")
            raise RuntimeError(
                "Aucun terrain disponible pour 20h. "
                "Voir erreur_aucun_terrain.png pour diagnostic."
            )

        # ── 5. Attente du formulaire de réservation ────────────────────────────
        print("  Attente du formulaire de réservation...")
        confirm_btn = None

        for i in range(10):
            time.sleep(1)

            btn = page.locator(
                '#modal_inscription button:has-text("Oui"), '
                'button:has-text("Oui, je reserve"), '
                'button:has-text("Valider"), '
                'button:has-text("Confirmer"), '
                'button:has-text("Réserver")'
            )
            if btn.count() > 0 and btn.first.is_visible():
                confirm_btn = btn.first
                print(f"  → Bouton de confirmation trouvé à t+{i+1}s.")
                break

        page.screenshot(path="formulaire_resa.png")
        print("  Screenshot formulaire : formulaire_resa.png")

        # ── 5b. Saisie du partenaire ────────────────────────────────────────
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
                print(f"  → Champ partenaire trouvé, saisie : {PARTNER}")
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
                        print("  → Partenaire sélectionné via autocomplétion.")
                except Exception:
                    print("  (pas d'autocomplétion, texte saisi directement)")
            else:
                print("  (champ partenaire non visible)")
        else:
            print("  (aucun champ partenaire détecté)")

        # ── 6. Confirmation ────────────────────────────────────────────────────
        if confirm_btn:
            print("  Clic sur le bouton de confirmation...")
            confirm_btn.click(force=True)
            time.sleep(3)
        else:
            fallback = page.locator(
                'button:has-text("Oui"), '
                'button:has-text("Valider"), '
                'button:has-text("Confirmer"), '
                'button:has-text("Réserver"), '
                'button:has-text("reserve")'
            )
            if fallback.count() > 0 and fallback.first.is_visible():
                print("  Fallback : bouton de confirmation trouvé.")
                fallback.first.click(force=True)
                time.sleep(3)
            else:
                print("  ⚠️  Aucun bouton de confirmation — le clic terrain a peut-être suffi.")

        page.screenshot(path="confirmation.png")
        print("  ✅ Réservation terminée (voir confirmation.png)")
        browser.close()


# ── Point d'entrée ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_paris = datetime.now(PARIS_TZ)
    print(f"\n=== Réservation Tennis ===")
    print(f"Heure Paris : {now_paris.strftime('%A %d/%m/%Y %H:%M')}")

    # if now_paris.weekday() != 2:
    #     print(f"Ce n'est pas mercredi à Paris. Abandon.")
    #     sys.exit(0)

    target_tuesday = get_next_tuesday(now_paris)
    print(f"Cible : mardi {target_tuesday.strftime('%d/%m/%Y')} à 20h00\n")

    print("Vérification météo (Open-Meteo)...")
    rain = check_rain_forecast(target_tuesday)
    rain_str = "Pluie prévue : terrain couvert" if rain else "Pas de pluie : terrain extérieur"
    print(f"  → {rain_str}\n")

    reserve_court(target_tuesday, rain)
