#!/usr/bin/env python3
"""
maj_tirage.py — recupere le dernier tirage EuroMillions sur fdj.fr,
puis regenere draws_vXXX.json et latest.json.
 
Concu pour tourner dans GitHub Actions, mais utilisable a la main :
    python3 tools/maj_tirage.py            # tirage attendu = le dernier passe
    python3 tools/maj_tirage.py 2026-07-31 # force une date precise
    python3 tools/maj_tirage.py --dry-run  # n'ecrit rien, affiche seulement
 
Sort avec le code 0 si tout va bien OU s'il n'y a rien a faire (tirage deja
present). Sort avec 1 si une donnee est invalide ou introuvable : dans ce cas
AUCUN fichier n'est modifie, et l'Action echoue de facon visible.
"""
 
import hashlib
import html as html_mod
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
 
BASE = "https://www.fdj.fr/jeux-de-tirage/euromillions-my-million/resultats"
ARRONDI = 1_000_000
JOURS_TIRAGE = {1, 4}          # 0=lundi … 1=mardi, 4=vendredi
HEURE_PARIS = 21
 
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "fevrier", "mars", "avril", "mai", "juin",
        "juillet", "aout", "septembre", "octobre", "novembre", "decembre"]
MOIS_ACC = ["janvier", "février", "mars", "avril", "mai", "juin",
            "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
 
# Bornes de vraisemblance : au-dela, on refuse de publier.
JACKPOT_MIN, JACKPOT_MAX = 15_000_000, 300_000_000
 
 
# ── Calendrier (memes regles que next_draw.dart) ──────────────────
 
def _dernier_dimanche(annee, mois):
    d = datetime(annee, mois + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d.replace(hour=1, minute=0, second=0, microsecond=0)
 
 
def _decalage_paris(utc):
    return 2 if _dernier_dimanche(utc.year, 3) <= utc < _dernier_dimanche(utc.year, 10) else 1
 
 
def dernier_tirage(maintenant=None):
    """Date du dernier tirage dont l'heure (21h00 Paris) est deja passee."""
    now = maintenant or datetime.now(timezone.utc)
    paris = now + timedelta(hours=_decalage_paris(now))
    for i in range(8):
        jour = (paris - timedelta(days=i)).date()
        if jour.weekday() not in JOURS_TIRAGE:
            continue
        instant = datetime(jour.year, jour.month, jour.day, HEURE_PARIS,
                           tzinfo=timezone.utc) - timedelta(hours=_decalage_paris(now))
        if instant < now:
            return jour
    raise SystemExit("aucun tirage passe trouve")
 
 
def prochain_tirage(apres):
    d = apres
    for _ in range(8):
        d += timedelta(days=1)
        if d.weekday() in JOURS_TIRAGE:
            return d
    raise SystemExit("aucun tirage suivant trouve")
 
 
def urls_candidates(d):
    """L'URL utilise des libelles francais. On tente sans accent puis avec."""
    jour = JOURS[d.weekday()]
    vues, sortie = set(), []
    for u in [
        f"{BASE}/{jour}-{d.day}-{MOIS[d.month - 1]}-{d.year}",
        f"{BASE}/{jour}-{d.day}-{MOIS_ACC[d.month - 1]}-{d.year}",
        f"{BASE}/{jour}-{d.day:02d}-{MOIS[d.month - 1]}-{d.year}",
    ]:
        if u not in vues:
            vues.add(u)
            sortie.append(u)
    return sortie
 
 
# ── Recuperation et extraction ────────────────────────────────────
 
def telecharger(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "euro-ai-data/1.0 (+https://github.com/delecluseimmo-rgb/euro-ai-data)",
        "Accept": "text/html",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")
 
 
def en_texte(html):
    """HTML -> texte lisible, espaces normalises."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html_mod.unescape(t)          # &eacute; &nbsp; &rsquo; …
    t = (t.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2019", "'"))
    return re.sub(r"\s+", " ", t)
 
 
def sans_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")
 
 
def extraire_boules(texte):
    """Depuis la phrase redactionnelle, la plus stable de la page."""
    m = re.search(
        r"num[ée]ros\s+(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})"
        r".{0,60}?[ée]toiles?,?\s*le\s+(\d{1,2})\s+et\s+le\s+(\d{1,2})",
        texte, re.I)
    if not m:
        return None, None
    v = [int(x) for x in m.groups()]
    return sorted(v[:5]), sorted(v[5:])
 
 
def extraire_prochaine_cagnotte(texte):
    """« Prochain tirage … de 89 millions d'€ », sinon « Près de 89 millions € »."""
    plat = sans_accents(texte)
    for motif in (r"Prochain tirage.{0,80}?de\s+([\d]+(?:[.,]\d+)?)\s*millions",
                  r"Pres de\s+([\d]+(?:[.,]\d+)?)\s*millions"):
        m = re.search(motif, plat, re.I)
        if m:
            return int(round(float(m.group(1).replace(",", ".")) * 1_000_000))
    return None
 
 
def date_du_tirage(texte):
    """Date annoncee par le titre de la page (« Tirage du mardi 28 juillet 2026 »).
 
    On ne se contente pas de chercher la date attendue quelque part : la page
    mentionne aussi celle du PROCHAIN tirage, ce qui validerait a tort.
    """
    plat = sans_accents(texte).lower()
    m = re.search(r"tirage du (\w+) (\d{1,2}) (\w+) (\d{4})", plat)
    if not m:
        return None
    jour, num, mois, annee = m.groups()
    if jour not in JOURS or mois not in MOIS:
        return None
    from datetime import date as _date
    return _date(int(annee), MOIS.index(mois) + 1, int(num))
 
 
# ── Ecriture ──────────────────────────────────────────────────────
 
def nom_suivant(actuel):
    m = re.search(r"(\d+)(?=\.json$)", actuel)
    if not m:
        raise SystemExit(f"nom de fichier inattendu : {actuel}")
    return actuel[:m.start()] + str(int(m.group(1)) + 1).zfill(len(m.group(1))) + ".json"
 
 
def main():
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
 
    manifeste = json.loads(Path("latest.json").read_text(encoding="utf-8"))
    dataset = json.loads(Path(manifeste["dataFile"]).read_text(encoding="utf-8"))
    rows = dataset["rows"]
 
    cible = (datetime.strptime(argv[0], "%Y-%m-%d").date() if argv
             else dernier_tirage())
    cible_num = int(cible.strftime("%Y%m%d"))
    print(f"tirage vise : {cible} ({cible_num})")
 
    # Idempotence : deja present, on ne fait rien et on sort proprement.
    if any(r[1] == cible_num for r in rows):
        print("deja dans le dataset — rien a faire")
        return 0
 
    # Continuite : on refuse de sauter un tirage.
    dernier = max(r[1] for r in rows)
    attendu = prochain_tirage(datetime.strptime(str(dernier), "%Y%m%d").date())
    if attendu != cible:
        print(f"ECHEC continuite : apres {dernier}, le tirage attendu est "
              f"{attendu}, pas {cible}")
        return 1
 
    html = None
    for url in urls_candidates(cible):
        try:
            html = telecharger(url)
            print(f"page recuperee : {url}")
            break
        except urllib.error.HTTPError as e:
            print(f"  {e.code} sur {url}")
        except Exception as e:
            print(f"  echec sur {url} : {e}")
    if html is None:
        print("ECHEC : aucune URL n'a repondu")
        return 1
 
    texte = en_texte(html)
 
    if re.search(r"access is temporarily restricted|unusual activity", texte, re.I):
        print("ECHEC : page anti-robot renvoyee")
        return 1
 
    lue = date_du_tirage(texte)
    if lue != cible:
        print(f"ECHEC : la page annonce le tirage du {lue}, pas du {cible}")
        return 1
 
    boules, etoiles = extraire_boules(texte)
    if not boules:
        print("ECHEC : combinaison introuvable (la page a peut-etre change)")
        return 1
 
    suivante = extraire_prochaine_cagnotte(texte)
    if suivante is None:
        print("ECHEC : cagnotte du prochain tirage introuvable")
        return 1
 
    # Le jackpot de CE tirage est celui qui etait annonce avant : nextJackpot.
    jackpot = int(manifeste.get("nextJackpot") or 0)
 
    # ── Controles ────────────────────────────────────────────────
    erreurs = []
    if len(set(boules)) != 5 or not all(1 <= b <= 50 for b in boules):
        erreurs.append(f"boules invalides : {boules}")
    if len(set(etoiles)) != 2 or not all(1 <= s <= 12 for s in etoiles):
        erreurs.append(f"etoiles invalides : {etoiles}")
    if not JACKPOT_MIN <= suivante <= JACKPOT_MAX:
        erreurs.append(f"cagnotte suivante hors bornes : {suivante}")
    if jackpot and not JACKPOT_MIN <= jackpot <= JACKPOT_MAX:
        erreurs.append(f"jackpot du tirage hors bornes : {jackpot}")
    if erreurs:
        for e in erreurs:
            print("ECHEC :", e)
        return 1
 
    print(f"  boules   : {boules}")
    print(f"  etoiles  : {etoiles}")
    print(f"  jackpot  : {jackpot:,}".replace(",", " "))
    print(f"  suivante : {suivante:,}".replace(",", " "))
 
    # ── Ecriture ─────────────────────────────────────────────────
    nouveau_id = max(r[0] for r in rows) + 1
    ligne = [nouveau_id, cible_num] + boules + etoiles + \
            [int(round(jackpot / ARRONDI) * ARRONDI)]
    dataset["rows"] = [ligne] + rows
    dataset["rowCount"] = len(dataset["rows"])
 
    nom = nom_suivant(manifeste["dataFile"])
    octets = json.dumps(dataset, ensure_ascii=False,
                        separators=(", ", ": ")).encode("utf-8")
    sha = hashlib.sha256(octets).hexdigest()
    prochaine = prochain_tirage(cible)
 
    manifeste.update({
        "version": manifeste["version"] + 1,
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "drawCount": dataset["rowCount"],
        "latestDrawDate": cible.isoformat(),
        "nextExpectedDraw": prochaine.isoformat(),
        "dataFile": nom,
        "sha256": sha,
        "nextJackpot": int(round(suivante / ARRONDI) * ARRONDI),
    })
 
    if dry:
        print(f"\n[dry-run] aurait ecrit {nom} et latest.json v{manifeste['version']}")
        print("[dry-run] ligne :", ligne)
        return 0
 
    Path(nom).write_bytes(octets)
    Path("latest.json").write_text(
        json.dumps(manifeste, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
 
    print(f"\necrit : {nom} ({dataset['rowCount']} tirages)")
    print(f"latest.json -> version {manifeste['version']}, sha {sha[:12]}…")
    return 0
 
 
if __name__ == "__main__":
    sys.exit(main())
 







