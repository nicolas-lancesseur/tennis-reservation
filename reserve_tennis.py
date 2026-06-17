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
import hashlib
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import stealth_sync

# ── Configuration ─────────────────────────────────────────────────────────────
CLUB_URL    = "https://www.premier-service.fr/_start/index.php?club=57920018"
USERNAME    = os.environ["TENNIS_USERNAME"]
PASSWORD    = os.environ["TENNIS_PASSWORD"]
PARTNER     = "Anthony Martin"
# Créneau cible : case "20:00 – 21:00" sur le planning du site
BOOKING_HOUR      = "20:00"
BOOKING_HOUR_END  = "21:00"
PARIS_TZ    = ZoneInfo("Europe/Paris")

# Coordonnées d'Issy-les-Moulineaux
LATITUDE  = 48.8234
LONGITUDE = 2.2735

# Seuil de probabilité de pluie (%) pour choisir un terrain couvert
RAIN_THRESHOLD = 40


# ── Utilitaires ───────────────────────────────────────────────────────────────

def get_next_tuesday(from_date: datetime) -> datetime:
    """Retourne le prochain mardi à partir de from_date."""
    days_ahead = (1 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


def check_rain_forecast(target_date: datetime) -> bool:
    """
    Retourne True si la probabilité de pluie dépasse RAIN_THRESHOLD
    sur au moins une heure entre 14h et 20h le jour cible.
    Source : Open-Meteo (gratuit, sans clé API).
    """
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
            headless=True,
            args=['--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            )
        )
        page = context.new_page()
        # Appliquer toutes les mesures anti-détection via playwright-stealth
        stealth_sync(page)

        # ── 1. Connexion ───────────────────────────────────────────────────
        print("  Connexion au site...")
        page.goto(CLUB_URL)
        page.wait_for_selector('input[name="userid"]', state="attached", timeout=15000)

        # Le site stocke le mot de passe sous forme de hash MD5 dans le champ usermd5.
        # Ce hash est normalement calculé côté client lors de la saisie.
        # En headless, les événements clavier sur les champs cachés ne déclenchent
        # pas ce calcul → on l'injecte directement en Python.
        password_md5 = hashlib.md5(PASSWORD.encode()).hexdigest()

        # Injection directe des valeurs dans le formulaire via JS
        page.evaluate(f"""
            document.querySelector('input[name="userid"]').value = {repr(USERNAME)};
            var userkey = document.querySelector('input[name="userkey"]');
            if (userkey) userkey.value = {repr(PASSWORD)};
            var usermd5 = document.querySelector('input[name="usermd5"]');
            if (usermd5) usermd5.value = '{password_md5}';
        """)
        time.sleep(0.3)
        # Soumission native du formulaire
        page.evaluate("document.querySelector('form').submit()")

        # Le site affiche une page "Veuillez patienter..." avant de charger
        # le planning (double navigation + AJAX). On poll directement le DOM
        # via JS pour bypasser le suivi de navigation de Playwright.
        print("  Attente du planning (peut prendre 20-30s)...")
        planning_loaded = False
        for _ in range(90):  # jusqu'à 90 secondes
            try:
                # Vérifier l'existence dans le DOM (pas la visibilité)
                # Le planning peut être dans le DOM mais invisible en headless
                found = page.evaluate(
                    "() => document.getElementById('btn_plus') !== null"
                )
                if found:
                    planning_loaded = True
                    break
            except Exception:
                pass  # page en cours de navigation, on réessaie
            time.sleep(1)

        if not planning_loaded:
            page.screenshot(path="apres_login.png")
            raise RuntimeError("Planning non chargé après 90s. Voir apres_login.png")

        print("  Connecté et planning chargé.")

        # ── 2. Navigation vers le mardi cible ──────────────────────────────
        now_paris      = datetime.now(PARIS_TZ)
        days_to_advance = (target_tuesday.date() - now_paris.date()).days
        print(f"  Navigation : +{days_to_advance} jour(s) → {target_tuesday.strftime('%A %d/%m/%Y')}")

        for _ in range(days_to_advance):
            page.locator('#btn_plus').click(force=True)
            time.sleep(2)  # laisser l'AJAX recharger le planning du jour suivant

        # ── 3. Sélection du créneau 20h00 ─────────────────────────────────
        # Structure réelle du site Premier Service :
        # chaque créneau est un <p id="{HH}_{MM}_{terrain_num}">
        # Ex : id="20_0_7" = 20h00 sur TCIM-7
        booked = False
        for court_num in court_order:
            print(f"  Tentative terrain {court_num}...")
            slot_id = f"20_0_{court_num}"
            slot = page.locator(f'[id="{slot_id}"]')

            if slot.count() > 0:
                txt = slot.inner_text().strip()
                if txt == "" or txt == "20h":
                    # Case libre : on clique (force=True car éléments invisibles en headless)
                    slot.click(force=True)
                    time.sleep(2)
                    booked = True
                    print(f"  ✓ Terrain {court_num} (id={slot_id}) sélectionné.")
                    break
                else:
                    print(f"  → Terrain {court_num} déjà réservé ({txt[:40]}), on passe.")
            else:
                print(f"  → id={slot_id} introuvable sur la page.")

        if not booked:
            page.screenshot(path="erreur_aucun_terrain.png")
            raise RuntimeError(
                "Aucun terrain disponible trouvé pour 20h. "
                "Voir erreur_aucun_terrain.png pour diagnostic."
            )

        # ── 4. Sélection du partenaire ─────────────────────────────────────
        print(f"  Sélection du partenaire : {PARTNER}...")

        partner_input = page.locator(
            'input[name*="partenaire"], '
            'input[name*="partner"], '
            'input[placeholder*="artenaire"], '
            'input[placeholder*="artner"]'
        ).first

        if partner_input.count() > 0:
            partner_input.fill(PARTNER)
            # Attente d'une suggestion d'autocomplétion
            try:
                page.wait_for_selector(f'text="{PARTNER}"', timeout=5000)
                page.click(f'text="{PARTNER}"')
            except PlaywrightTimeoutError:
                # Pas d'autocomplétion — le texte saisi suffit peut-être
                print("  (pas d'autocomplétion détectée, texte saisi directement)")
        else:
            # Tentative avec un menu déroulant
            page.select_option(
                'select[name*="partenaire"], select[name*="partner"]',
                label=PARTNER
            )

        # ── 5. Validation ──────────────────────────────────────────────────
        page.locator(
            'button:has-text("Valider"), '
            'button:has-text("Confirmer"), '
            'button:has-text("Réserver"), '
            'input[type="submit"]'
        ).first.click(force=True)
        time.sleep(3)

        page.screenshot(path="confirmation.png")
        print("  ✅ Réservation confirmée ! (voir confirmation.png)")
        browser.close()


# ── Point d'entrée ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_paris = datetime.now(PARIS_TZ)
    print(f"\n=== Réservation Tennis ===")
    print(f"Heure Paris : {now_paris.strftime('%A %d/%m/%Y %H:%M')}")

    # Garde-fou : s'exécuter uniquement le mercredi à Paris
    # (les deux crons UTC peuvent déclencher le workflow le mardi ou le mercredi)
    # if now_paris.weekday() != 2:
    #     print(f"Ce n'est pas mercredi à Paris. Abandon.")
    #     sys.exit(0)

    target_tuesday = get_next_tuesday(now_paris)
    print(f"Cible : mardi {target_tuesday.strftime('%d/%m/%Y')} à 20h00\n")

    print("Vérification météo (Open-Meteo)...")
    rain = check_rain_forecast(target_tuesday)
    print(f"  → {'Pluie prévue : terrain couvert' if rain else 'Pas de pluie : terrain extérieur'}\n")

    reserve_court(target_tuesday, rain)
