"""Veille des prix de billets Ryanair avec notification Telegram.

Aucune clé API, aucun compte : utilise les adresses publiques de Ryanair.
Conçu pour tourner sur GitHub Actions une fois par jour.
Notifie uniquement quand un prix passe SOUS le seuil ET qu'il est meilleur
que le dernier prix signalé (pas de spam).
"""

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

# ============================================================
#  CONFIGURATION — c'est ici que tu modifies tes destinations
# ============================================================
#
#  destination   : code IATA de l'aéroport d'arrivée
#  origins       : aéroports de départ à comparer
#  mois          : mois précis à surveiller (format AAAA-MM)
#  mois_devant   : OU mois calculés automatiquement (2 = dans 2 mois)
#  nuits_min/max : durée de séjour acceptable
#  seuil         : en euros. Alerte si l'aller-retour descend en dessous
#
#  Aéroports Ryanair pratiques depuis Nancy / la France :
#    BVA = Paris-Beauvais      CRL = Charleroi (≈3 h de Nancy)
#    LYS = Lyon                MRS = Marseille
#    BOD = Bordeaux            TLS = Toulouse
#    HHN = Francfort-Hahn      NTE = Nantes
# ============================================================

ROUTES = [
    {
        "nom": "🇲🇹 Malte",
        "destination": "MLA",
        "origins": ["BVA", "CRL", "MRS", "LYS"],
        "mois": ["2026-12"],
        "nuits_min": 4,
        "nuits_max": 10,
        "seuil": 90,
    },
    {
        "nom": "🇮🇹 Italie (Rome Ciampino)",
        "destination": "CIA",
        "origins": ["BVA", "CRL", "MRS"],
        "mois_devant": [2, 3, 4],
        "nuits_min": 3,
        "nuits_max": 7,
        "seuil": 80,
    },
    {
        "nom": "🇮🇹 Italie (Milan Bergame)",
        "destination": "BGY",
        "origins": ["BVA", "CRL", "MRS"],
        "mois_devant": [2, 3, 4],
        "nuits_min": 3,
        "nuits_max": 7,
        "seuil": 80,
    },
    {
        "nom": "🇪🇸 Espagne (Barcelone)",
        "destination": "BCN",
        "origins": ["BVA", "CRL", "MRS"],
        "mois_devant": [2, 3, 4],
        "nuits_min": 3,
        "nuits_max": 7,
        "seuil": 80,
    },
    {
        "nom": "🇪🇸 Espagne (Madrid)",
        "destination": "MAD",
        "origins": ["BVA", "CRL", "MRS"],
        "mois_devant": [2, 3, 4],
        "nuits_min": 3,
        "nuits_max": 7,
        "seuil": 90,
    },
    {
        "nom": "🇬🇷 Mykonos",
        "destination": "JMK",
        "origins": ["BVA", "CRL", "MRS", "BGY"],
        "mois_devant": [8, 9, 10, 11],
        "nuits_min": 5,
        "nuits_max": 10,
        "seuil": 200,
    },
]

# Petite pause entre les appels, par politesse envers le serveur
PAUSE = 1.0

# ============================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

API = "https://www.ryanair.com/api/farfnd/v4/roundTripFares"
STATE_FILE = Path("state_vols.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


def send_telegram(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def mois_a_verifier(route: dict) -> list:
    if "mois" in route:
        return [m + "-01" for m in route["mois"]]
    aujourdhui = date.today().replace(day=1)
    resultat = []
    for n in route.get("mois_devant", [2, 3]):
        annee = aujourdhui.year + (aujourdhui.month - 1 + n) // 12
        mois = (aujourdhui.month - 1 + n) % 12 + 1
        resultat.append(date(annee, mois, 1).isoformat())
    return resultat


def extraire_tarifs(bloc: dict) -> dict:
    """Transforme le bloc outbound/inbound en {date: prix}."""
    tarifs = {}
    for fare in (bloc or {}).get("fares", []):
        if fare.get("soldOut") or fare.get("unavailable"):
            continue
        prix = (fare.get("price") or {}).get("value")
        jour = fare.get("day") or (fare.get("departureDate") or "")[:10]
        if prix is not None and jour:
            tarifs[jour] = float(prix)
    return tarifs


def interroger(origin: str, dest: str, mois: str):
    """Renvoie (aller {date: prix}, retour {date: prix}) ou None."""
    url = f"{API}/{origin}/{dest}/cheapestPerDay"
    try:
        r = requests.get(
            url,
            headers=HEADERS,
            params={
                "outboundMonthOfDate": mois,
                "inboundMonthOfDate": mois,
                "currency": "EUR",
            },
            timeout=45,
        )
    except Exception as exc:
        print(f"    ⚠ {origin}→{dest} {mois[:7]} : {exc}")
        return None

    if r.status_code != 200:
        print(f"    ⚠ {origin}→{dest} {mois[:7]} : HTTP {r.status_code}")
        return None

    try:
        data = r.json()
    except Exception:
        print(f"    ⚠ {origin}→{dest} : réponse illisible → {r.text[:200]}")
        return None

    aller = extraire_tarifs(data.get("outbound"))
    retour = extraire_tarifs(data.get("inbound"))
    if not aller and not retour and data:
        print(f"    ℹ structure inattendue : {json.dumps(data)[:300]}")
    return aller, retour


def meilleure_combinaison(aller: dict, retour: dict, nmin: int, nmax: int):
    """Cherche le meilleur couple aller/retour respectant la durée."""
    meilleur = None
    for jour_a, prix_a in aller.items():
        d_a = date.fromisoformat(jour_a)
        for jour_r, prix_r in retour.items():
            nuits = (date.fromisoformat(jour_r) - d_a).days
            if nmin <= nuits <= nmax:
                total = prix_a + prix_r
                if meilleur is None or total < meilleur[0]:
                    meilleur = (total, jour_a, jour_r, nuits)
    return meilleur


def charger_etat() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def main() -> None:
    etat = charger_etat()
    nouvel_etat = {}
    alertes = []
    appels = 0

    for route in ROUTES:
        nom, dest, seuil = route["nom"], route["destination"], route["seuil"]
        cle = f"{dest}"
        print(f"\n{nom} — seuil {seuil} €")

        meilleur = None  # (prix, origin, jour_aller, jour_retour, nuits)

        for mois in mois_a_verifier(route):
            for origin in route["origins"]:
                if origin == dest:
                    continue
                resultat = interroger(origin, dest, mois)
                appels += 1
                time.sleep(PAUSE)
                if not resultat:
                    continue

                aller, retour = resultat
                combi = meilleure_combinaison(
                    aller, retour, route["nuits_min"], route["nuits_max"]
                )
                if combi:
                    total, ja, jr, nuits = combi
                    print(
                        f"    {origin}→{dest} {mois[:7]} : "
                        f"{total:.0f} € ({ja} → {jr}, {nuits} nuits)"
                    )
                    if meilleur is None or total < meilleur[0]:
                        meilleur = (total, origin, ja, jr, nuits)

        if meilleur is None:
            print("  (aucun vol trouvé — Ryanair ne dessert peut-être pas cette route)")
            if cle in etat:
                nouvel_etat[cle] = etat[cle]
            continue

        prix, origin, ja, jr, nuits = meilleur
        nouvel_etat[cle] = prix
        ancien = etat.get(cle)
        print(f"  → meilleur : {prix:.0f} € depuis {origin}")

        if prix <= seuil and (ancien is None or prix < ancien):
            baisse = f" · avant : {ancien:.0f} €" if ancien else ""
            lien = (
                f"https://www.ryanair.com/fr/fr/trip/flights/select"
                f"?adults=1&dateOut={ja}&dateIn={jr}&isReturn=true"
                f"&originIata={origin}&destinationIata={dest}"
            )
            alertes.append(
                f"<b>{nom} — {prix:.0f} €</b>{baisse}\n"
                f"{origin} → {dest} · {ja} au {jr} ({nuits} nuits)\n"
                f'<a href="{lien}">Réserver sur Ryanair</a>'
            )

    if alertes:
        send_telegram("✈️ <b>Bon plan billets !</b>\n\n" + "\n\n".join(alertes))
        print(f"\n✅ Notification envoyée ({len(alertes)} alerte(s))")
    else:
        print("\nRien sous les seuils aujourd'hui.")

    STATE_FILE.write_text(json.dumps(nouvel_etat, indent=2))
    print(f"Total : {appels} appels.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
