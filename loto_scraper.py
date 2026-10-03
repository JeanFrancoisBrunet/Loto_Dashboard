"""
loto_scraper.py
================

Récupère l'historique COMPLET des tirages du Loto français (FDJ) depuis 1976
et le structure dans un DataFrame pandas, exporté en CSV + SQLite.

SOURCE UTILISÉE : les archives officielles ZIP/CSV de la FDJ, disponibles sur
    https://www.fdj.fr/jeux-de-tirage/loto/historique

C'est la source la plus fiable : c'est l'opérateur du jeu lui-même. Les portails
type data.gouv.fr / OpenDataSoft ne font en général que republier ces mêmes
fichiers, avec un temps de latence et une URL moins stable dans le temps.

⚠️ Particularité technique importante :
Les liens de téléchargement ZIP sur la page FDJ sont générés dynamiquement
avec un identifiant (UUID) qui change au fil du temps. Il est donc IMPOSSIBLE
de coder ces URLs en dur dans un script durable : ce script scrape la page
d'archives à chaque exécution pour récupérer les liens courants, puis
télécharge et parse chaque archive.

⚠️ Parsing des dates :
Le format des CSV FDJ n'est pas homogène entre 1976 et aujourd'hui (colonnes
renommées, encodages numériques différents selon les périodes). Un premier
essai naïf (chercher une colonne "date_de_tirage" ou repli sur la première
colonne commençant par "date") produisait, sur l'archive 1976-2008, des
tirages fantômes datés du 01/01/1970 : la colonne retenue par défaut était en
réalité numérique, et pandas l'interprétait comme un timestamp Unix. Ce script
teste maintenant PLUSIEURS colonnes candidates avec PLUSIEURS stratégies de
parsing, et retient la combinaison qui produit le plus de dates plausibles
(entre 1976 et aujourd'hui) — voir _try_parse_dates() / normalize().

Dépendances :
    pip install requests pandas beautifulsoup4 lxml

Usage :
    python loto_scraper.py
"""

from __future__ import annotations

import io
import re
import sqlite3
import logging
import zipfile
from pathlib import Path
from dataclasses import dataclass

import requests
import pandas as pd
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("loto")

FDJ_HISTORIQUE_URL = "https://www.fdj.fr/jeux-de-tirage/loto/historique"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

OUTPUT_DIR = Path("loto_data")
OUTPUT_DIR.mkdir(exist_ok=True)

# Bornes de sanité utilisées pour valider un parsing de date : le Loto FDJ
# existe depuis mai 1976, toute date hors de cette plage est un artefact.
DATE_MIN = pd.Timestamp("1976-01-01")


@dataclass
class ArchiveLink:
    label: str
    url: str


# --------------------------------------------------------------------------- #
# 1. Récupération des liens d'archives (scraping de la page FDJ)
# --------------------------------------------------------------------------- #

def get_archive_links() -> list[ArchiveLink]:
    """
    Scrape la page d'historique FDJ et retourne uniquement les liens de la
    section "Historique Loto" (on exclut volontairement Super Loto,
    Grand Loto de Noël, etc. qui sont des jeux distincts).
    """
    resp = requests.get(FDJ_HISTORIQUE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links: list[ArchiveLink] = []
    for a in soup.find_all("a", href=True):
        title = a.get("title", "") or a.get_text(" ", strip=True)
        if "Historique Loto" in title and "Super" not in title and "Grand" not in title:
            links.append(ArchiveLink(label=title.strip(), url=a["href"]))

    if not links:
        raise RuntimeError(
            "Aucun lien d'archive trouvé. La structure HTML de la page FDJ a "
            "peut-être changé (site redesigné) ou l'accès a été bloqué "
            "(essayez de mettre à jour le User-Agent)."
        )

    log.info("Liens d'archive Loto trouvés : %d", len(links))
    for link in links:
        log.info("  - %s", link.label)
    return links


# --------------------------------------------------------------------------- #
# 2. Téléchargement + lecture d'une archive ZIP -> DataFrame brut
# --------------------------------------------------------------------------- #

def download_and_read_csv(url: str) -> pd.DataFrame:
    """Télécharge une archive ZIP et retourne le CSV qu'elle contient sous
    forme de DataFrame (colonnes en minuscules, non normalisées)."""
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not csv_names:
            raise RuntimeError(f"Aucun fichier CSV/TXT trouvé dans l'archive : {url}")
        with zf.open(csv_names[0]) as f:
            raw = f.read()

    # Les fichiers FDJ sont historiquement encodés en latin-1 avec séparateur ';'
    text = raw.decode("latin-1", errors="replace")
    df = pd.read_csv(io.StringIO(text), sep=";", engine="python")
    df.columns = [c.strip().lower() for c in df.columns]
    return df


# --------------------------------------------------------------------------- #
# 3. Normalisation : le format des colonnes FDJ a changé plusieurs fois
#    depuis 1976. On ramène chaque période vers un schéma commun.
# --------------------------------------------------------------------------- #

def _try_parse_dates(series: pd.Series) -> list[tuple[pd.Series, float]]:
    """Essaie plusieurs stratégies de parsing de date sur une colonne, et
    retourne pour chacune (série parsée, score = proportion de résultats
    plausibles). Voir la note en tête de fichier sur le bug des dates 1970."""
    valid_max = pd.Timestamp.now() + pd.Timedelta(days=2)

    def score_of(parsed: pd.Series) -> float:
        if len(parsed) == 0:
            return 0.0
        return float(parsed.between(DATE_MIN, valid_max).mean())

    candidates = []

    # 1) Chaîne classique jour/mois/année (format le plus courant)
    try:
        p1 = pd.to_datetime(series, dayfirst=True, errors="coerce")
        candidates.append((p1, score_of(p1)))
    except Exception:
        pass

    # 2) Entier AAAAMMJJ (ex. 20081112), rencontré sur certains exports anciens
    try:
        p2 = pd.to_datetime(series.astype(str).str.strip(), format="%Y%m%d", errors="coerce")
        candidates.append((p2, score_of(p2)))
    except Exception:
        pass

    # 3) Numéro de série type Excel (jours depuis 1899-12-30)
    try:
        numeric = pd.to_numeric(series, errors="coerce")
        p3 = pd.to_datetime(numeric, unit="D", origin="1899-12-30", errors="coerce")
        candidates.append((p3, score_of(p3)))
    except Exception:
        pass

    return candidates


def normalize(df: pd.DataFrame, source_label: str) -> tuple[pd.DataFrame, dict]:
    """Ramène un CSV FDJ vers un schéma commun. Teste toutes les colonnes
    contenant "date" avec plusieurs stratégies de parsing et retient la
    meilleure combinaison, plutôt que de deviner une seule colonne par son
    nom (voir la note en tête de fichier).

    Retourne (DataFrame normalisé, diagnostic) pour que l'appelant puisse
    signaler le taux de lignes effectivement conservées.
    """
    boule_cols = sorted(c for c in df.columns if re.fullmatch(r"boule_?\d", c))
    if not boule_cols:
        log.warning("Colonnes 'boule_X' introuvables pour '%s' — fichier ignoré.", source_label)
        return pd.DataFrame(), {"raw": len(df), "kept": 0, "date_col": None, "score": 0.0}

    date_candidate_cols = [c for c in df.columns if "date" in c or c == "jour_de_tirage"]
    if not date_candidate_cols:
        date_candidate_cols = list(df.columns)  # dernier recours : on teste tout

    best_col, best_parsed, best_score = None, None, -1.0
    for c in date_candidate_cols:
        for parsed, score in _try_parse_dates(df[c]):
            if score > best_score:
                best_col, best_parsed, best_score = c, parsed, score

    if best_parsed is None or best_score <= 0:
        log.warning("Aucune colonne date exploitable pour '%s' — fichier ignoré.", source_label)
        return pd.DataFrame(), {"raw": len(df), "kept": 0, "date_col": best_col, "score": 0.0}

    comp_col = next(
        (c for c in df.columns if "complementaire" in c or "numero_chance" in c),
        None,
    )
    jackpot_col = next((c for c in df.columns if "rapport_du_rang1" in c or ("gain" in c and "rang1" in c)), None)

    out = pd.DataFrame()
    out["date_tirage"] = best_parsed
    for i, c in enumerate(boule_cols[:5], start=1):
        out[f"boule_{i}"] = pd.to_numeric(df[c], errors="coerce")
    out["numero_complementaire"] = pd.to_numeric(df[comp_col], errors="coerce") if comp_col else pd.NA
    out["gain_rang1"] = pd.to_numeric(df[jackpot_col], errors="coerce") if jackpot_col else pd.NA
    out["source"] = source_label

    valid_max = pd.Timestamp.now() + pd.Timedelta(days=2)
    out = out[out["date_tirage"].between(DATE_MIN, valid_max)]
    out = out.dropna(subset=["date_tirage", "boule_1"]).reset_index(drop=True)

    diag = {"raw": len(df), "kept": len(out), "date_col": best_col, "score": round(best_score, 2)}
    return out, diag


# --------------------------------------------------------------------------- #
# 4. Orchestration : télécharge toutes les périodes et fusionne
# --------------------------------------------------------------------------- #

def build_full_history() -> pd.DataFrame:
    frames = []
    for link in get_archive_links():
        log.info("Téléchargement : %s", link.label)
        try:
            raw_df = download_and_read_csv(link.url)
            norm_df, diag = normalize(raw_df, link.label)
            log.info(
                "  -> %d/%d lignes conservées (colonne date « %s », fiabilité %.0f%%)",
                diag["kept"], diag["raw"], diag.get("date_col"), diag.get("score", 0) * 100,
            )
            if diag["raw"] > 0 and diag["kept"] / diag["raw"] < 0.5:
                log.warning(
                    "  ⚠ Taux de conservation faible pour '%s' — vérifiez le format de "
                    "cette archive (colonne détectée : « %s »).", link.label, diag.get("date_col"),
                )
            if not norm_df.empty:
                frames.append(norm_df)
        except Exception as exc:
            log.warning("Échec pour '%s' : %s", link.label, exc)

    if not frames:
        raise RuntimeError("Aucune archive n'a pu être récupérée ni parsée.")

    full = pd.concat(frames, ignore_index=True)
    full = (
        full.drop_duplicates(subset=["date_tirage", "boule_1", "boule_2", "boule_3", "boule_4", "boule_5"])
        .sort_values("date_tirage")
        .reset_index(drop=True)
    )
    return full


# --------------------------------------------------------------------------- #
# 5. Sauvegarde
# --------------------------------------------------------------------------- #

def save(df: pd.DataFrame) -> None:
    csv_path = OUTPUT_DIR / "loto_historique.csv"
    db_path = OUTPUT_DIR / "loto_historique.sqlite3"

    df.to_csv(csv_path, index=False, encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        df.to_sql("tirages", conn, if_exists="replace", index=False)

    log.info("Fichier CSV sauvegardé   : %s (%d lignes)", csv_path, len(df))
    log.info("Base SQLite sauvegardée  : %s (table 'tirages')", db_path)


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    df = build_full_history()

    print("\n--- Aperçu ---")
    print(df.head())
    print(df.tail())
    print(
        f"\n{len(df)} tirages récupérés, "
        f"du {df['date_tirage'].min().date()} au {df['date_tirage'].max().date()}."
    )

    save(df)
