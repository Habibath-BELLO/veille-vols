"""Veille des prix de billets d'avion avec notification Telegram.

Deux sources complémentaires :
  1. Ryanair — prix en temps réel, mais Europe low-cost uniquement
  2. Travelpayouts — toutes compagnies du monde (Air France, Brussels,
     Royal Air Maroc, Transavia, easyJet...), y compris le long-courrier.
     Prix issus d'un cache : à revérifier avant de réserver.

La plage de dates se calcule toute seule à chaque exécution.
Aucune modification manuelle n'est nécessaire.

Une destination peut avoir une "période" privilégiée (ex. décembre) :
elle est alors suivie EN PLUS du meilleur prix toutes périodes confondues,
avec son propre seuil. Tu reçois donc deux signaux distincts.

Secrets attendus :
  TELEGRAM_BOT_TOKEN   (obligatoire)
  TELEGRAM_CHAT_ID     (obligatoire)
  TRAVELPAYOUTS_TOKEN  (facultatif : active la source 2)

Variable facultative :
  VEILLE_ACTIVE = off  -> met la veille en pause sans rien supprimer
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

# ============================================================
#  CONFIGURATION
# ============================================================

# Fenêtre glissante : nombre de mois surveillés à partir du mois en cours.
# Aucune année n'est écrite en dur, le script ne périme jamais.
# 13 mois correspond à l'horizon de mise en vente des compagnies.
HORIZON_MOIS = 13

ROUTES = [
    {
        "nom": "🇲🇹 Malte",
        "destination": "MLA",
        "origins_ryanair": ["BVA", "CRL", "MRS"],
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 3,
        "nuits_max": 14,
        "seuil": 200,
        # Période privilégiée suivie en parallèle.
        # Pour en ajouter une ailleurs, copie ce bloc dans la destination
        # voulue : "mois" = numéros des mois, "seuil" = seuil propre.
        "periode": {"nom": "décembre", "mois": [12], "seuil": 200},
    },
    {
        "nom": "🇮🇹 Italie (Rome)",
        "destination": "CIA",
        "origins_ryanair": ["BVA", "CRL", "MRS"],
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 2,
        "nuits_max": 10,
        "seuil": 200,
    },
    {
        "nom": "🇮🇹 Italie (Milan)",
        "destination": "BGY",
        "origins_ryanair": ["BVA", "CRL", "MRS"],
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 2,
        "nuits_max": 10,
        "seuil": 200,
    },
    {
        "nom": "🇪🇸 Espagne (Barcelone)",
        "destination": "BCN",
        "origins_ryanair": ["BVA", "CRL", "MRS"],
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 2,
        "nuits_max": 10,
        "seuil": 200,
    },
    {
        "nom": "🇪🇸 Espagne (Madrid)",
        "destination": "MAD",
        "origins_ryanair": ["BVA", "CRL", "MRS"],
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 2,
        "nuits_max": 10,
        "seuil": 200,
    },
    {
        "nom": "🇧🇪 Belgique (Bruxelles)",
        "destination": "BRU",
        "origins_ryanair": [],
        "origins_compare": ["PAR", "LYS"],
        "nuits_min": 1,
        "nuits_max": 10,
        "seuil": 200,
    },
    {
        "nom": "🇬🇷 Mykonos",
        "destination": "JMK",
        "origins_ryanair": ["BVA", "CRL", "MRS", "BGY"],
        "origins_compare": ["PAR", "BRU"],
        "nuits_min": 4,
        "nuits_max": 15,
        "seuil": 600,
    },
    {
        "nom": "🇧🇯 Bénin (Cotonou)",
        "destination": "COO",
        "origins_ryanair": [],
        "origins_compare": ["PAR", "BRU"],
        "nuits_min": 5,
        "nuits_max": 45,
        "seuil": 600,
    },
  {
        "nom": "🇨🇦 Canada (Montréal)",
        "destination": "YMQ",           # code ville : tous les aéroports
        "origins_ryanair": [],          # aucun low-cost ne traverse l'Atlantique
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 2,
        "nuits_max": 6,
        "seuil": 1200,
        "periode": {
            "nom": "20-23 août",
            "mois": [8], "jour_min": 18, "jour_max": 24,
            "seuil": 1200,
        },
    },
    {
        "nom": "🇨🇦 Canada (Toronto)",
        "destination": "YTO",
        "origins_ryanair": [],
        "origins_compare": ["PAR", "BRU", "LYS"],
        "nuits_min": 2,
        "nuits_max": 6,
        "seuil": 1200,
        "periode": {
            "nom": "20-23 août",
            "mois": [8], "jour_min": 18, "jour_max": 24,
            "seuil": 1200,
        },
    },
    {
        "nom": "🇧🇸 Bahamas (Nassau)",
        "destination": "NAS",
        "origins_ryanair": [],
        "origins_compare": ["PAR", "BRU"],
        "nuits_min": 5,
        "nuits_max": 30,
        "seuil": 600,
    },
]

PAUSE = 0.5  # pause entre deux requêtes, par politesse

# Deuxième déclencheur d'alerte, indépendant du seuil : toute baisse d'au
# moins ce pourcentage par rapport au dernier prix connu. Garantit qu'une
# destination reste vivante même si son seuil est mal calibré.
BAISSE_MINI = 0.15  # 15 %

# Filtre anti-bruit : sous le seuil, un nouveau record ne déclenche l'alerte
# que s'il améliore l'ancien d'au moins ce pourcentage. Évite d'être
# notifiée pour 1 € de moins quand les seuils sont larges.
AMELIORATION_MINI = 0.03  # 3 %

# Récapitulatif hebdomadaire : le plus bas prix de CHAQUE destination,
# envoyé même sans baisse. 0 = lundi, 1 = mardi ... 6 = dimanche.
# Mets RECAP_HEBDO = False pour le désactiver.
RECAP_HEBDO = True
JOUR_RECAP = 0

# ============================================================

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()

# Interrupteur pause : mets la variable VEILLE_ACTIVE à "off" pour suspendre
EN_PAUSE = os.environ.get("VEILLE_ACTIVE", "on").strip().lower() in (
    "off", "non", "no", "pause", "0", "false"
)

RYANAIR_API = "https://www.ryanair.com/api/farfnd/v4/roundTripFares"
TP_MONTHLY = "https://api.travelpayouts.com/v1/prices/monthly"
TP_DATES = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
STATE_FILE = Path("state_vols.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

COMPAGNIES = {
    "AF": "Air France", "SN": "Brussels Airlines", "AT": "Royal Air Maroc",
    "TK": "Turkish", "ET": "Ethiopian", "KQ": "Kenya Airways",
    "KP": "ASKY", "HF": "Air Côte d'Ivoire", "FR": "Ryanair",
    "U2": "easyJet", "TO": "Transavia", "VY": "Vueling", "W6": "Wizz Air",
    "IB": "Iberia", "LH": "Lufthansa", "KL": "KLM", "BA": "British Airways",
    "A3": "Aegean", "DL": "Delta", "AA": "American", "UA": "United",
}


def nom_compagnie(code: str) -> str:
    return COMPAGNIES.get(code, code or "?")


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


def mois_de_la_plage() -> list:
    """Les HORIZON_MOIS mois à partir du mois en cours."""
    courant = date.today().replace(day=1)
    mois = []
    while len(mois) < HORIZON_MOIS:
        mois.append(courant.isoformat())
        annee = courant.year + (1 if courant.month == 12 else 0)
        m = 1 if courant.month == 12 else courant.month + 1
        courant = date(annee, m, 1)
    return mois


def nuits_ok(depart: str, retour: str, route: dict) -> bool:
    if not retour:
        return False
    try:
        n = (date.fromisoformat(retour) - date.fromisoformat(depart)).days
    except ValueError:
        return False
    return route["nuits_min"] <= n <= route["nuits_max"]


# ---------------------- source 1 : Ryanair ----------------------

def extraire_tarifs(bloc: dict) -> dict:
    tarifs = {}
    for fare in (bloc or {}).get("fares", []):
        if fare.get("soldOut") or fare.get("unavailable"):
            continue
        prix = (fare.get("price") or {}).get("value")
        jour = fare.get("day") or (fare.get("departureDate") or "")[:10]
        if prix is not None and jour:
            tarifs[jour] = float(prix)
    return tarifs


def chercher_ryanair(route: dict, mois_liste: list) -> list:
    """Renvoie la meilleure offre de chaque couple mois/aéroport."""
    dest = route["destination"]
    offres = []
    for mois in mois_liste:
        for origin in route.get("origins_ryanair", []):
            if origin == dest:
                continue
            try:
                r = requests.get(
                    f"{RYANAIR_API}/{origin}/{dest}/cheapestPerDay",
                    headers=HEADERS,
                    params={
                        "outboundMonthOfDate": mois,
                        "inboundMonthOfDate": mois,
                        "currency": "EUR",
                    },
                    timeout=45,
                )
                data = r.json() if r.status_code == 200 else {}
            except Exception:
                data = {}
            finally:
                time.sleep(PAUSE)

            local = None
            for ja, pa in extraire_tarifs(data.get("outbound")).items():
                for jr, pr in extraire_tarifs(data.get("inbound")).items():
                    if not nuits_ok(ja, jr, route):
                        continue
                    total = pa + pr
                    if local is None or total < local[0]:
                        local = (total, origin, ja, jr, "Ryanair")
            if local:
                offres.append(local)
    return offres


# ------------------ source 2 : Travelpayouts ------------------

def offres_travelpayouts(origin: str, dest: str, mois: str = None) -> list:
    """Renvoie une liste d'offres normalisées."""
    if mois:
        url, params = TP_DATES, {
            "origin": origin, "destination": dest,
            "departure_at": mois[:7], "currency": "eur",
            "sorting": "price", "direct": "false", "one_way": "false",
            "limit": 30, "page": 1, "market": "fr", "token": TP_TOKEN,
        }
    else:
        url, params = TP_MONTHLY, {
            "origin": origin, "destination": dest,
            "currency": "eur", "market": "fr", "token": TP_TOKEN,
        }

    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=45)
        if r.status_code != 200:
            print(f"    ⚠ comparateur {origin}→{dest} : HTTP {r.status_code}")
            return []
        brut = r.json().get("data")
    except Exception as exc:
        print(f"    ⚠ comparateur {origin}→{dest} : {exc}")
        return []
    finally:
        time.sleep(PAUSE)

    # v1 renvoie un dictionnaire, v3 une liste
    items = list(brut.values()) if isinstance(brut, dict) else (brut or [])
    offres = []
    for o in items:
        if not isinstance(o, dict):
            continue
        try:
            offres.append({
                "prix": float(o["price"]),
                "depart": (o.get("departure_at") or "")[:10],
                "retour": (o.get("return_at") or "")[:10],
                "compagnie": o.get("airline") or "",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return offres


def chercher_comparateur(route: dict, mois_liste: list) -> list:
    """Renvoie toutes les offres valides trouvées par le comparateur."""
    if not TP_TOKEN:
        return []

    dest = route["destination"]
    debut, fin = mois_liste[0][:7], mois_liste[-1][:7]
    resultats = []   # accumulateur, tous aéroports confondus

    for origin in route.get("origins_compare", []):
        if origin == dest:
            continue

        brut = offres_travelpayouts(origin, dest)

        # Si le survol mensuel ne donne rien, on sonde quelques mois précis
        if not brut:
            indices = {0, len(mois_liste) // 2, len(mois_liste) - 1}
            for i in sorted(indices):
                brut.extend(offres_travelpayouts(origin, dest, mois_liste[i]))

        for o in brut:
            if not o["depart"] or not (debut <= o["depart"][:7] <= fin):
                continue
            if not nuits_ok(o["depart"], o["retour"], route):
                continue
            resultats.append((o["prix"], origin, o["depart"], o["retour"],
                              nom_compagnie(o["compagnie"])))
    return resultats


# ----------------------------------------------------------------

def charger_etat() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def lien(origin: str, dest: str, ja: str, jr: str, compagnie: str) -> str:
    if compagnie == "Ryanair":
        return (f"https://www.ryanair.com/fr/fr/trip/flights/select"
                f"?adults=1&dateOut={ja}&dateIn={jr}&isReturn=true"
                f"&originIata={origin}&destinationIata={dest}")
    return (f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}"
            f"%20to%20{dest}%20on%20{ja}%20through%20{jr}")


def traiter(libelle: str, cle: str, offres: list, seuil: float,
            etat: dict, nouvel_etat: dict, alertes: list, recap: list,
            dest: str, exclure=None, digest: list = None):
    """Retient la meilleure offre d'un lot et déclenche l'alerte si besoin."""
    if not offres:
        print(f"  {libelle} : aucune offre trouvée")
        if cle in etat:
            nouvel_etat[cle] = etat[cle]
        recap.append(f"{libelle} : —")
        if digest is not None:
            digest.append(f"{libelle} — <i>aucune offre</i>")
        return None

    prix, origin, ja, jr, compagnie = min(offres, key=lambda x: x[0])
    nouvel_etat[cle] = prix
    ancien = etat.get(cle)
    print(f"  {libelle} : {prix:.0f} € depuis {origin} ({compagnie}, {ja} → {jr})")
    recap.append(f"{libelle} : {prix:.0f} € ({origin}, {compagnie}, {ja})")
    if digest is not None:
        digest.append(
            f'<a href="{lien(origin, dest, ja, jr, compagnie)}">'
            f"<b>{libelle} — {prix:.0f} €</b></a>\n"
            f"    {origin} · {ja} · {compagnie}"
        )

    # évite de signaler deux fois exactement la même offre
    if exclure and (prix, ja, jr) == exclure:
        return (prix, ja, jr)

    # sous le seuil : il faut un vrai nouveau record, pas 1 € de moins
    sous_seuil = prix <= seuil and (
        ancien is None or prix <= ancien * (1 - AMELIORATION_MINI)
    )
    chute = ancien is not None and prix <= ancien * (1 - BAISSE_MINI)

    if sous_seuil or chute:
        if ancien:
            pct = (ancien - prix) / ancien * 100
            baisse = f" · avant : {ancien:.0f} € (−{pct:.0f} %)"
        else:
            baisse = ""
        alertes.append(
            f"<b>{libelle} — {prix:.0f} €</b>{baisse}\n"
            f"{origin} → {dest} · {ja} au {jr} · {compagnie}\n"
            f'<a href="{lien(origin, dest, ja, jr, compagnie)}">Voir l\'offre</a>'
        )
    return (prix, ja, jr)


def main() -> None:
    if EN_PAUSE:
        print("⏸ Veille en pause (VEILLE_ACTIVE = off). Aucune recherche lancée.")
        print("   Pour reprendre : Settings → Secrets and variables → Actions")
        print("   → onglet Variables → VEILLE_ACTIVE = on")
        return

    mois_liste = mois_de_la_plage()
    print(f"Plage surveillée : {mois_liste[0][:7]} → {mois_liste[-1][:7]}")
    print(f"Comparateur toutes compagnies : "
          f"{'ACTIVÉ' if TP_TOKEN else 'désactivé (token absent)'}\n")

    etat = charger_etat()
    nouvel_etat = {}
    alertes = []
    recap = []
    digest = []

    for route in ROUTES:
        nom, dest = route["nom"], route["destination"]
        print(f"{nom}")

        offres = (chercher_ryanair(route, mois_liste)
                  + chercher_comparateur(route, mois_liste))

        # 1) meilleur prix toutes périodes confondues
        retenu = traiter(nom, dest, offres, route["seuil"],
                         etat, nouvel_etat, alertes, recap, dest,
                         digest=digest)

        # 2) meilleur prix sur la période privilégiée, s'il y en a une
        periode = route.get("periode")
        if periode:
            sous = [o for o in offres
                    if len(o[2]) >= 7 and int(o[2][5:7]) in periode["mois"]]
            traiter(f"{nom} · {periode['nom']}",
                    f"{dest}-{periode['nom']}", sous, periode["seuil"],
                    etat, nouvel_etat, alertes, recap, dest, exclure=retenu,
                    digest=digest)
        print()

    print("--- Récapitulatif ---")
    for ligne in recap:
        print(" ", ligne)

    if alertes:
        send_telegram("✈️ <b>Bon plan billets !</b>\n\n" + "\n\n".join(alertes))
        print(f"\n✅ Alerte envoyée ({len(alertes)} baisse(s))")
    else:
        print("\nAucune baisse significative aujourd'hui.")

    # Récapitulatif hebdomadaire : tous les plus bas prix, même sans baisse
    if RECAP_HEBDO and date.today().weekday() == JOUR_RECAP and digest:
        send_telegram(
            "📊 <b>Les plus bas prix de la semaine</b>\n\n"
            + "\n\n".join(digest)
        )
        print("📊 Récapitulatif hebdomadaire envoyé")

    STATE_FILE.write_text(json.dumps(nouvel_etat, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
