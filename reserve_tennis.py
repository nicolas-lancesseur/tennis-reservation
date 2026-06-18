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
            headless=False,  # navigateur visible via Xvfb (contourne la détection headless)
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
        # Le site présente un formulaire avec 4 champs visibles :
        #   - userid / userkey  → leurres (cachés via CSS, offsetParent=null)
        #   - [nom_obfusqué_text] / [nom_obfusqué_password] → vrais champs
        # Les noms obfusqués changent à chaque chargement de page.
        # La méthode fiable : cibler les champs dont le nom N'EST PAS
        # "userid" ou "userkey" (les leurres ont toujours ces noms fixes).
        # Le clic sur "Entrer" appelle fsmd5() qui lit le vrai champ password,
        # calcule le MD5, et soumet le formulaire.
        print("  Chargement de la page de connexion...")
        page.goto(CLUB_URL, wait_until="load", timeout=30000)

        # Attendre la redirection JS vers ics.php
        try:
            page.wait_for_url("**/ics.php**", timeout=20000)
            print(f"  Redirigé vers : {page.url}")
        except PlaywrightTimeoutError:
            print(f"  URL : {page.url}")

        # Attendre que le formulaire soit prêt
        page.wait_for_selector('input[name="userid"]', state="attached", timeout=15000)
        print("  Formulaire détecté — remplissage des vrais champs...")

        # Remplir le vrai champ texte (username) — pas le leurre "userid"
        # (les leurres sont cachés via CSS, offsetParent=null)
        real_user = page.locator('input[type="text"]:not([name="userid"])')
        real_user.fill(USERNAME)

        # Remplir le vrai champ password — pas le leurre "userkey"
        real_pass = page.locator('input[type="password"]:not([name="userkey"])')
        real_pass.fill(PASSWORD)

        time.sleep(0.5)

        # Mesure défensive : renseigner aussi "userkey" (leurre) via JS.
        # La source exacte lue par fsmd5() (vrai champ ou leurre) est inconnue ;
        # remplir les deux garantit que fsmd5() dispose du mot de passe.
        page.evaluate(f"document.querySelector('input[name=\"userkey\"]').value = '{PASSWORD}'")

        time.sleep(0.3)

        # Avec headless=False, on peut cliquer le bouton "Entrer" normalement.
        # Cela déclenche onclick → fsmd5() + fs() → le formulaire est soumis
        # avec tous les champs de fingerprinting (hauteur_ecran, pingmax, etc.)
        # que le serveur attend. Bypasser fs() via form.submit() omettrait ces
        # champs et pourrait faire échouer l'authentification côté serveur.
        page.locator('button:has-text("Entrer")').first.click(force=True)
        print("  Bouton Entrer cliqué.")

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
            except Exception:
                pass
            raise RuntimeError("Planning non chargé après 90s. Voir apres_login.png")

        print("  Connecté et planning chargé.")

        # ── 3. Navigation vers le mardi cible ─────────────────────────────
        now_paris       = datetime.now(PARIS_TZ)
        days_to_advance = (target_tuesday.date() - now_paris.date()).days
        print(f"  Navigation : +{days_to_advance} jour(s) → {target_tuesday.strftime('%A %d/%m/%Y')}")

        for _ in range(days_to_advance):
            page.locator('#btn_plus').click(force=True)
            time.sleep(2)

        # ── 4. Sélection du créneau 20h00 ─────────────────────────────────────
        # Les créneaux libres ont le texte "20h" (ou vide).
        # Le clic via Playwright (isTrusted=true) déclenche les handlers jQuery
        # au niveau document, ce qui ouvre le formulaire de réservation.
        booked = False
        for court_num in court_order:
            print(f"  Tentative terrain {court_num}...")
            slot_id = f"20_0_{court_num}"
            slot = page.locator(f'[id="{slot_id}"]')

            if slot.count() == 0:
                print(f"  → id={slot_id} introuvable sur la page.")
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

        # ── 5. Attente et gestion du formulaire post-clic ─────────────────────
        # Après un clic trusté, le site peut :
        #   a) Afficher un formulaire inline dans bloc_reservation
        #   b) Ouvrir un modal jqmWindow (ex: modal_inscription)
        #   c) Recharger la page avec un formulaire de confirmation
        # On attend jusqu'à 10s en cherchant tout bouton de confirmation.
        print("  Attente du formulaire de réservation...")
        confirm_btn = None
        partner_input_found = None

        for i in range(10):
            time.sleep(1)

            # Chercher un bouton de confirmation dans les modals ou la page
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

            # Détecter si un champ partenaire est visible (pour logging)
            partner_check = page.locator('input[placeholder*="artenaire"], input[name*="artenaire"]')
            if partner_check.count() > 0 and partner_check.first.is_visible():
                partner_input_found = True

        page.screenshot(path="formulaire_resa.png")
        print(f"  Screenshot formulaire : formulaire_resa.png")

        # ── 5b. Saisie du partenaire (si un champ est visible) ────────────────
        print(f"  Recherche du champ partenaire...")
        # Cherche les champs partenaire par attributs reconnaissables
        partner_fields = page.locator(
            'input[placeholder*="artenaire"], '
            'input[placeholder*="artner"], '
            'input[name*="artenaire"], '
            'input[name*="artner"]'
        )
        # Fallback : tout input texte visible sauf le sélecteur de date
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
                # Essayer l'autocomplétion
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
            print("  (aucun champ partenaire détecté — la réservation peut ne pas en demander)")

        # ── 6. Clic sur le bouton de confirmation ────────────────────────────
        if confirm_btn:
            print(f"  Clic sur le bouton de confirmation...")
            confirm_btn.click(force=True)
            time.sleep(3)
        else:
            # Dernière tentative : chercher n'importe quel bouton de confirmation
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
                print("  ⚠️  Aucun bouton de confirmation trouvé — le clic sur le terrain a peut-être suffi.")

        page.screenshot(path="confirmation.png")
        print("  ✅ Réservation terminée (voir confirmation.png)")
        browser.close()


# ── Point d'entrée ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    now_paris = datetime.now(PARIS_TZ)
    print(f"\n=== Réservation Tennis ===")
    print(f"Heure Paris : {now_paris.strftime('%A %d/%m/%Y %H:%M')}")

    # Garde-fou : s'exécuter uniquement le mercredi à Paris
    # if now_paris.weekday() != 2:
    #     print(f"Ce n'est pas mercredi à Paris. Abandon.")
    #     sys.exit(0)

    target_tuesday = get_next_tuesday(now_paris)
    print(f"Cible : mardi {target_tuesday.strftime('%d/%m/%Y')} à 20h00\n")

    print("Vérification météo (Open-Meteo)...")
    rain = check_rain_forecast(target_tuesday)
    print(f"  → {'Pluie prévue : terrain couvert' if rain else 'Pas de pluie : terrain extérieur'}\n")

    reserve_court(target_tuesday, rain)
