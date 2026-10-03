#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Tableau de bord Loto — Historique · Statistiques · Tirages
#  Raspberry Pi 5 (16 Go RAM, SSD NVMe 256 Go, OS Bookworm)
#
#  Source : archives officielles https://www.fdj.fr/jeux-de-tirage/loto/historique
#
#  Dépendances : pip install requests pandas beautifulsoup4 lxml matplotlib
#
#  Auteur : Jean-François BRUNET – JFBConseils – Juillet 2026
# =============================================================================

import io
import os
import re
import json
import time
import random
import sqlite3
import zipfile
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass

import tkinter as tk
from tkinter import ttk, messagebox

import requests
import pandas as pd
from bs4 import BeautifulSoup

import warnings
import matplotlib
matplotlib.use("TkAgg")
warnings.filterwarnings("ignore", message="Unable to import Axes3D",
                         category=UserWarning, module="matplotlib")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ─── CONFIGURATION ───

FDJ_HISTORIQUE_URL = "https://www.fdj.fr/jeux-de-tirage/loto/historique"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
}

# Les tirages Loto ont lieu lun/mer/sam soir : pas besoin de scruter toutes les minutes.
# 24h suffit largement, et évite de solliciter le site FDJ inutilement.
AUTO_REFRESH_INTERVAL_MS = 24 * 60 * 60 * 1000
SPLASH_DURATION_MS       = 2500
HTTP_TIMEOUT              = 30

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_FILE     = os.path.join(SCRIPT_DIR, "loto_historique.sqlite3")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "loto_config.json")

# Icône du splash — dossier dédié du projet et non SCRIPT_DIR/icons.
ICON_DIR  = os.path.expanduser("~/Projects/Tirages_Loto/icons")
SPLASH_IMG = os.path.join(ICON_DIR, "loto.png")

# ─── COULEURS & STYLE ───

BG_MAIN      = "#0d0f14"
BG_PANEL     = "#13161e"
BG_HEADER    = "#1a1e2a"
BG_ROW_ODD   = "#161922"
BG_ROW_EVEN  = "#13161e"

COLOR_GOLD   = "#f0c040"
COLOR_BLUE   = "#4fc3f7"
COLOR_GREEN  = "#4ade80"
COLOR_RED    = "#f87171"
COLOR_WHITE  = "#e8eaf0"
COLOR_GRAY   = "#6b7280"
COLOR_ORANGE = "#fb923c"

FONT_TITLE  = ("Courier New", 17, "bold")
FONT_HEADER = ("Courier New", 12, "bold")
FONT_DATA   = ("Courier New", 13)
FONT_DATA_B = ("Courier New", 13, "bold")
FONT_SMALL  = ("Courier New", 11)
FONT_STATUS = ("Courier New", 11)

BOULE_NUMBERS = list(range(1, 50))    # boules 1 à 49
CHANCE_NUMBERS = list(range(1, 11))   # numéro chance 1 à 10

# =============================================================================
#  COUCHE DONNÉES — scraping FDJ + normalisation + SQLite
# =============================================================================

@dataclass
class ArchiveLink:
    label: str
    url: str

def get_archive_links() -> list[ArchiveLink]:
    """Scrape la page d'archives FDJ. Les URLs contiennent un UUID qui change
    au fil du temps : on ne peut PAS les coder en dur, on les relit à chaque appel."""
    resp = requests.get(FDJ_HISTORIQUE_URL, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        title = a.get("title", "") or a.get_text(" ", strip=True)
        if "Historique Loto" in title and "Super" not in title and "Grand" not in title:
            links.append(ArchiveLink(label=title.strip(), url=a["href"]))
    if not links:
        raise RuntimeError("Aucun lien d'archive Loto trouvé sur la page FDJ "
                            "(structure de page modifiée ?).")
    return links

def download_and_read_csv(url: str) -> pd.DataFrame:
    resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith((".csv", ".txt"))]
        if not csv_names:
            raise RuntimeError(f"Aucun CSV dans l'archive : {url}")
        with zf.open(csv_names[0]) as f:
            raw = f.read()
    text = raw.decode("latin-1", errors="replace")
    df = pd.read_csv(io.StringIO(text), sep=";", engine="python")
    df.columns = [c.strip().lower() for c in df.columns]
    return df

def _try_parse_dates(series: pd.Series) -> list[tuple[pd.Series, float]]:
    """Essaie plusieurs stratégies de parsing de date sur une colonne, et
    retourne pour chacune (série parsée, score = proportion de résultats
    plausibles). Nécessaire car le format des CSV FDJ n'est pas homogène
    entre 1976 et aujourd'hui — certaines archives anciennes encodent la
    date différemment (numérique, format non prévu, etc.)."""
    valid_min = pd.Timestamp("1976-01-01")   # date de création du Loto FDJ
    valid_max = pd.Timestamp.now() + pd.Timedelta(days=2)

    def score_of(parsed: pd.Series) -> float:
        if len(parsed) == 0:
            return 0.0
        in_range = parsed.between(valid_min, valid_max)
        return float(in_range.mean())

    candidates = []

    # 1) Chaîne classique jour/mois/année (format le plus courant)
    try:
        p1 = pd.to_datetime(series, dayfirst=True, errors="coerce")
        candidates.append((p1, score_of(p1)))
    except Exception:
        pass

    # 2) Entier AAAAMMJJ (ex. 20081112), rencontré sur certains exports FDJ anciens
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


def normalize(df: pd.DataFrame, label: str) -> tuple[pd.DataFrame, dict]:
    """Ramène les différents formats historiques FDJ vers un schéma commun.
    Teste TOUTES les colonnes contenant "date" avec plusieurs stratégies de
    parsing et retient la meilleure combinaison (score = proportion de dates
    plausibles entre 1976 et aujourd'hui).
    Retourne (DataFrame normalisé, diagnostic) pour que l'appelant puisse
    signaler à l'utilisateur le taux de lignes effectivement conservées."""
    boule_cols = sorted(c for c in df.columns if re.fullmatch(r"boule_?\d", c))
    if not boule_cols:
        return pd.DataFrame(), {"raw": len(df), "kept": 0, "date_col": None,
                                 "score": 0.0, "note": "colonnes boule_X introuvables"}

    date_candidate_cols = [c for c in df.columns if "date" in c or c == "jour_de_tirage"]
    if not date_candidate_cols:
        date_candidate_cols = list(df.columns)  # dernier recours : on teste tout

    best_col, best_parsed, best_score = None, None, -1.0
    for c in date_candidate_cols:
        for parsed, score in _try_parse_dates(df[c]):
            if score > best_score:
                best_col, best_parsed, best_score = c, parsed, score

    if best_parsed is None or best_score <= 0:
        return pd.DataFrame(), {"raw": len(df), "kept": 0, "date_col": best_col,
                                 "score": 0.0, "note": "aucune colonne date exploitable"}

    comp_col = next((c for c in df.columns
                      if "complementaire" in c or "numero_chance" in c), None)

    out = pd.DataFrame()
    out["date_tirage"] = best_parsed
    for i, c in enumerate(boule_cols[:5], start=1):
        out[f"boule_{i}"] = pd.to_numeric(df[c], errors="coerce")
    out["numero_complementaire"] = pd.to_numeric(df[comp_col], errors="coerce") if comp_col else pd.NA
    out["source"] = label

    valid_min = pd.Timestamp("1976-01-01")
    valid_max = pd.Timestamp.now() + pd.Timedelta(days=2)
    out = out[out["date_tirage"].between(valid_min, valid_max)]
    out = out.dropna(subset=["date_tirage", "boule_1"]).reset_index(drop=True)

    diag = {"raw": len(df), "kept": len(out), "date_col": best_col, "score": round(best_score, 2)}
    return out, diag

_TIRAGES_SCHEMA = """
    CREATE TABLE tirages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_tirage TEXT NOT NULL,
        boule_1 INTEGER, boule_2 INTEGER, boule_3 INTEGER,
        boule_4 INTEGER, boule_5 INTEGER,
        numero_complementaire INTEGER,
        source TEXT,
        UNIQUE(date_tirage, boule_1, boule_2, boule_3, boule_4, boule_5)
    )
"""

def init_db():
    """Crée la table si absente. 
    Système basé sur : clé composite date + les 5 boules, car avant octobre 2008, 
    le Loto FDJ avait parfois DEUX tirages le même jour (le "second tirage")."""
    with sqlite3.connect(DB_FILE) as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tirages'"
        ).fetchone() is not None

        if table_exists:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(tirages)").fetchall()]
            if "id" not in cols:
                conn.execute("ALTER TABLE tirages RENAME TO tirages_legacy")
                conn.execute(_TIRAGES_SCHEMA)
                conn.execute("""
                    INSERT INTO tirages (date_tirage, boule_1, boule_2, boule_3, boule_4,
                                          boule_5, numero_complementaire, source)
                    SELECT date_tirage, boule_1, boule_2, boule_3, boule_4,
                           boule_5, numero_complementaire, source
                    FROM tirages_legacy
                """)
                conn.execute("DROP TABLE tirages_legacy")
                conn.commit()
                print("[db] Migration du schéma effectuée (clé date -> clé date+boules) "
                      "— relancez un « Réimporter tout l'historique » pour récupérer les "
                      "tirages des jours à deux tirages, avant octobre 2008.")
        else:
            conn.execute(_TIRAGES_SCHEMA)
            conn.commit()

def upsert_draws(df: pd.DataFrame) -> int:
    """Insère les tirages absents de la base (déduplication sur date + les 5
    boules, pas sur la date seule — voir init_db). Retourne le nombre de lignes ajoutées."""
    if df.empty:
        return 0
    with sqlite3.connect(DB_FILE) as conn:
        before = conn.execute("SELECT COUNT(*) FROM tirages").fetchone()[0]
        rows = [
            (r.date_tirage.strftime("%Y-%m-%d"), int(r.boule_1), int(r.boule_2),
             int(r.boule_3), int(r.boule_4), int(r.boule_5),
             int(r.numero_complementaire) if pd.notna(r.numero_complementaire) else None,
             r.source)
            for r in df.itertuples(index=False)
        ]
        conn.executemany("""
            INSERT OR IGNORE INTO tirages
            (date_tirage, boule_1, boule_2, boule_3, boule_4, boule_5,
             numero_complementaire, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM tirages").fetchone()[0]
    return after - before

def load_all_draws() -> pd.DataFrame:
    with sqlite3.connect(DB_FILE) as conn:
        df = pd.read_sql("SELECT * FROM tirages ORDER BY date_tirage, id", conn)
    if not df.empty:
        df["date_tirage"] = pd.to_datetime(df["date_tirage"])
    return df

def load_config() -> dict:
    defaults = {"full_history_loaded": False, "last_update": None}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**defaults, **data}
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[config] Impossible d'écrire {CONFIG_FILE} : {e}")

def update_history(stop_event: threading.Event | None = None) -> tuple[int, list[str]]:
    """Mise à jour incrémentale :
      - 1er lancement (base vide) : télécharge TOUTES les archives (1976 -> auj.)
      - lancements suivants : ne retélécharge que l'archive la plus récente
        (celle qui va jusqu'à "aujourd'hui"), largement suffisant puisque les
        autres périodes ne changent jamais a posteriori.
    Retourne (nb_nouveaux_tirages, liste_erreurs)."""
    init_db()
    cfg = load_config()
    errors: list[str] = []

    try:
        links = get_archive_links()
    except Exception as e:
        return 0, [f"Impossible de récupérer la liste des archives FDJ : {e}"]

    is_full_backfill = not cfg.get("full_history_loaded")
    if is_full_backfill:
        targets = links                     # backfill complet
    else:
        targets = links[:1]                 # seule la période la plus récente

    total_new = 0
    for link in targets:
        if stop_event and stop_event.is_set():
            break
        try:
            raw = download_and_read_csv(link.url)
            norm, diag = normalize(raw, link.label)
            total_new += upsert_draws(norm)
            if diag["raw"] > 0 and diag["kept"] / diag["raw"] < 0.5:
                errors.append(
                    f"{link.label} : seulement {diag['kept']}/{diag['raw']} lignes conservées "
                    f"— vérifiez le format de cette archive (colonne détectée : « {diag.get('date_col')} »)."
                )
        except Exception as e:
            errors.append(f"{link.label} : {e}")

    if not errors:
        cfg["full_history_loaded"] = True
    cfg["last_update"] = datetime.now().isoformat(timespec="seconds")
    save_config(cfg)
    return total_new, errors


def purge_and_reset_history() -> int:
    """Purge les éventuels artefacts de dates aberrantes (ex. 01/01/1970) 
    et force un ré-import complet au prochain rafraîchissement. 
    Retourne le nombre de lignes purgées."""
    init_db()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("DELETE FROM tirages WHERE date_tirage < '1976-01-01'")
        purged = cur.rowcount
        conn.commit()
    cfg = load_config()
    cfg["full_history_loaded"] = False
    save_config(cfg)
    return purged

# =============================================================================
#  STATISTIQUES
# =============================================================================

def compute_frequencies(df: pd.DataFrame) -> tuple[dict, dict]:
    """Retourne (fréquence des boules 1-49, fréquence des numéros chance 1-10)."""
    boule_freq = {n: 0 for n in BOULE_NUMBERS}
    for col in ["boule_1", "boule_2", "boule_3", "boule_4", "boule_5"]:
        for v in df[col].dropna().astype(int):
            if v in boule_freq:
                boule_freq[v] += 1

    chance_freq = {n: 0 for n in CHANCE_NUMBERS}
    if "numero_complementaire" in df.columns:
        for v in df["numero_complementaire"].dropna().astype(int):
            if v in chance_freq:
                chance_freq[v] += 1
    return boule_freq, chance_freq

def compute_ecarts(df: pd.DataFrame) -> dict:
    """Pour chaque numéro 1-49, nombre de tirages écoulés depuis sa dernière
    apparition (0 = sorti au dernier tirage). Classique en analyse Loto,
    bien que — rappel utile — cela ne prédit rien statistiquement."""
    ecarts = {n: None for n in BOULE_NUMBERS}
    df_sorted = df.sort_values("date_tirage", ascending=False).reset_index(drop=True)
    for idx, row in df_sorted.iterrows():
        for col in ["boule_1", "boule_2", "boule_3", "boule_4", "boule_5"]:
            v = row[col]
            if pd.notna(v) and ecarts.get(int(v)) is None:
                ecarts[int(v)] = idx
        if all(v is not None for v in ecarts.values()):
            break
    # Numéros jamais vus dans l'historique chargé (improbable, mais on gère le cas)
    max_idx = len(df_sorted)
    return {n: (v if v is not None else max_idx) for n, v in ecarts.items()}

def compute_ecarts_chance(df: pd.DataFrame) -> dict:
    """Identique à compute_ecarts, mais pour le numéro chance (1-10)."""
    ecarts = {n: None for n in CHANCE_NUMBERS}
    df_sorted = df.sort_values("date_tirage", ascending=False).reset_index(drop=True)
    for idx, row in df_sorted.iterrows():
        v = row.get("numero_complementaire")
        if pd.notna(v) and ecarts.get(int(v)) is None:
            ecarts[int(v)] = idx
        if all(val is not None for val in ecarts.values()):
            break
    max_idx = len(df_sorted)
    return {n: (v if v is not None else max_idx) for n, v in ecarts.items()}

def split_en_retard(ecarts: dict) -> tuple[set, set]:
    """Sépare un dict {numéro: écart} en (numéros EN RETARD, numéros normaux)
    selon la médiane des écarts. Cette définition est PARTAGÉE entre l'onglet
    "Numéros en retard" et le "Générateur de grilles", afin que les deux
    restent cohérents entre eux (un numéro classé "en retard" ici l'est aussi
    dans l'autre onglet, sur la même période d'analyse)."""
    if not ecarts:
        return set(), set()
    values = sorted(ecarts.values())
    median = values[len(values) // 2]
    en_retard = {n for n, e in ecarts.items() if e >= median}
    pas_en_retard = {n for n, e in ecarts.items() if e < median}
    if not en_retard or not pas_en_retard:
        # Garde-fou en cas d'égalité totale des écarts (période trop courte, etc.)
        ordered = sorted(ecarts.items(), key=lambda kv: kv[1])
        half = len(ordered) // 2
        pas_en_retard = {n for n, _ in ordered[:half]}
        en_retard = {n for n, _ in ordered[half:]}
    return en_retard, pas_en_retard

# =============================================================================
#  SÉLECTEUR DE PÉRIODE (réutilisé par 3 onglets)
# =============================================================================

class PeriodSelector(tk.Frame):
    """Sélecteur Début / Fin (granularité mois), INSTANCE UNIQUE partagée par
    tout le dashboard (placée au-dessus des onglets) : une modification de
    période se répercute sur les 4 onglets (Derniers tirages, Statistiques,
    Numéros en retard, Générateur de grilles).

    La borne "Fin" est toujours réinitialisée sur le dernier tirage connu à
    chaque rafraîchissement des données (jamais figée sur une ancienne
    valeur). La borne "Début" peut être restaurée depuis la configuration
    persistée (voir Dashboard._on_period_change / cfg["period_start"])."""

    def __init__(self, parent, on_apply):
        super().__init__(parent, bg=BG_PANEL)
        self._on_apply = on_apply
        self._all_periods: list[str] = []

        tk.Label(self, text="Période :", font=FONT_DATA, fg=COLOR_WHITE,
                 bg=BG_PANEL).pack(side="left", padx=(10, 8), pady=8)
        tk.Label(self, text="Du", font=FONT_DATA, fg=COLOR_GRAY, bg=BG_PANEL).pack(side="left")
        self._start_var = tk.StringVar()
        self._start_combo = ttk.Combobox(self, textvariable=self._start_var, state="readonly",
                                          width=9, font=FONT_DATA)
        self._start_combo.pack(side="left", padx=6)

        tk.Label(self, text="au", font=FONT_DATA, fg=COLOR_GRAY, bg=BG_PANEL).pack(side="left")
        self._end_var = tk.StringVar()
        self._end_combo = ttk.Combobox(self, textvariable=self._end_var, state="readonly",
                                        width=9, font=FONT_DATA)
        self._end_combo.pack(side="left", padx=6)

        tk.Button(self, text="Appliquer", font=FONT_SMALL, fg=COLOR_GOLD, bg="#252a38",
                  activeforeground=COLOR_WHITE, activebackground="#353a50",
                  relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._apply).pack(side="left", padx=(12, 4), pady=6)
        tk.Button(self, text="Tout l'historique", font=FONT_SMALL, fg=COLOR_GOLD, bg="#252a38",
                  activeforeground=COLOR_WHITE, activebackground="#353a50",
                  relief="flat", bd=0, padx=12, pady=4, cursor="hand2",
                  command=self._reset).pack(side="left", padx=4)

    def set_bounds(self, min_date: pd.Timestamp, max_date: pd.Timestamp,
                    default_start: str | None = None, keep_start: bool = False):
        """Repeuple les listes déroulantes. La fin est TOUJOURS remise sur le
        dernier mois disponible. Le début : conservé si `keep_start` (cas
        d'un simple rafraîchissement en cours de session), sinon `default_start`
        si fourni et valide (valeur persistée au démarrage), sinon le tout
        début de l'historique."""
        months = pd.period_range(min_date, max_date, freq="M").astype(str).tolist()
        if not months:
            return
        self._all_periods = months
        self._start_combo["values"] = months
        self._end_combo["values"] = months

        self._end_var.set(months[-1])

        if keep_start and self._start_var.get() in months:
            pass
        elif default_start and default_start in months:
            self._start_var.set(default_start)
        else:
            self._start_var.set(months[0])

    def get_selection(self) -> tuple:
        """Retourne (début, fin) d'après les comboboxes actuels, sans passer
        par le bouton Appliquer — utilisé par le générateur de grilles."""
        try:
            start = pd.Period(self._start_var.get()).start_time
            end = pd.Period(self._end_var.get()).end_time
            return start, end
        except Exception:
            return None, None

    def _apply(self):
        start, end = self.get_selection()
        if start is None:
            return
        if start > end:
            messagebox.showinfo("Période", "La date de début doit précéder la date de fin.")
            return
        self._on_apply(start, end)

    def _reset(self):
        if self._all_periods:
            self._start_var.set(self._all_periods[0])
            self._end_var.set(self._all_periods[-1])
            self._apply()

# =============================================================================
#  SPLASH SCREEN
# =============================================================================

class SplashScreen(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.overrideredirect(True)
        self.configure(bg=BG_HEADER)
        self.attributes("-topmost", True)
        self._img_ref = None
        try:
            from PIL import Image, ImageTk
            img = Image.open(SPLASH_IMG)
            img.thumbnail((600, 400), Image.LANCZOS)
            self._img_ref = ImageTk.PhotoImage(img)
            tk.Label(self, image=self._img_ref, bg=BG_HEADER).pack()
        except Exception:
            tk.Label(self, text="🎱  LOTO  ◆  Tableau de Bord",
                     font=("Courier New", 28, "bold"),
                     fg=COLOR_GOLD, bg=BG_HEADER, pady=30, padx=60).pack()
        tk.Label(self, text="Tableau de Bord Loto",
                 font=("Courier New", 15, "bold"),
                 fg=COLOR_BLUE, bg=BG_HEADER).pack(pady=(4, 2))
        tk.Label(self, text="Historique · Statistiques · Numéros en retard",
                 font=FONT_SMALL, fg=COLOR_GRAY, bg=BG_HEADER).pack(pady=(0, 8))
        tk.Label(self, text="Source : FDJ officielle — chargement en cours…",
                 font=FONT_SMALL, fg=COLOR_GRAY, bg=BG_HEADER).pack(pady=(0, 12))
        self._center()

    def _center(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h   = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{(sw - w)//2}+{(sh - h)//2}")

# =============================================================================
#  ONGLET — DERNIERS TIRAGES
# =============================================================================

class DerniersTiragesTab(tk.Frame):
    """Liste des tirages sur la période choisie, dans un ttk.Treeview
    (ascenseur natif, et bien plus performant qu'une pile de Frame/Label
    quand la période sélectionnée couvre plusieurs milliers de tirages)."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG_MAIN)
        self._df_full = pd.DataFrame()
        self._build()

    def _build(self):
        style = ttk.Style()
        style.configure("Loto.Treeview", background=BG_ROW_EVEN, fieldbackground=BG_ROW_EVEN,
                         foreground=COLOR_WHITE, font=FONT_DATA, rowheight=32, borderwidth=0)
        style.configure("Loto.Treeview.Heading", background=BG_HEADER, foreground=COLOR_GOLD,
                         font=FONT_HEADER, relief="flat")
        style.map("Loto.Treeview", background=[("selected", "#2a3040")])

        tree_frame = tk.Frame(self, bg=BG_MAIN)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        columns = ("date", "boules", "chance")
        self._tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                   style="Loto.Treeview")
        self._tree.heading("date", text="Date")
        self._tree.heading("boules", text="Boules")
        self._tree.heading("chance", text="Chance")
        self._tree.column("date", width=260, anchor="w")
        self._tree.column("boules", width=300, anchor="w")
        self._tree.column("chance", width=110, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(side="left", fill="both", expand=True)

        self._count_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._count_var, font=FONT_SMALL, fg=COLOR_GRAY,
                 bg=BG_MAIN).pack(pady=6)

    def set_data(self, df: pd.DataFrame):
        self._df_full = df

    def apply_period(self, start, end):
        subset = self._df_full[(self._df_full["date_tirage"] >= start) &
                                (self._df_full["date_tirage"] <= end)]
        self._render(subset)

    def _render(self, df: pd.DataFrame):
        self._tree.delete(*self._tree.get_children())
        ordered = df.sort_values("date_tirage", ascending=False)
        for row in ordered.itertuples():
            date_str = row.date_tirage.strftime("%A %d/%m/%Y").capitalize()
            boules = " · ".join(f"{int(getattr(row, f'boule_{k}')):02d}" for k in range(1, 6))
            chance = row.numero_complementaire
            chance_str = f"{int(chance):02d}" if pd.notna(chance) else "--"
            self._tree.insert("", "end", values=(date_str, boules, chance_str))
        self._count_var.set(f"{len(df)} tirage(s) affiché(s) sur la période sélectionnée")

# =============================================================================
#  ONGLET — STATISTIQUES (fréquence des numéros)
# =============================================================================

class StatistiquesTab(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG_MAIN)
        self._df_full = pd.DataFrame()

        self._fig = None
        self._canvas = None
        tk.Label(self, text="Ceci mesure le nombre TOTAL de sorties sur la période.\n"
                             "Pour la récence de la dernière sortie, voir l'onglet « Numéros en retard ».",
                 font=FONT_SMALL, fg=COLOR_BLUE, bg=BG_MAIN, wraplength=900,
                 justify="center").pack(pady=(6, 0))
        self._chart_area = tk.Frame(self, bg=BG_PANEL)
        self._chart_area.pack(fill="both", expand=True, padx=6, pady=6)
        self._info_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._info_var, font=FONT_SMALL,
                 fg=COLOR_GRAY, bg=BG_MAIN).pack(pady=(0, 6))

    def set_data(self, df: pd.DataFrame):
        self._df_full = df

    def apply_period(self, start, end):
        subset = self._df_full[(self._df_full["date_tirage"] >= start) &
                                (self._df_full["date_tirage"] <= end)]
        self._render(subset)

    def _render(self, df: pd.DataFrame):
        boule_freq, _ = compute_frequencies(df)
        n_tirages = len(df)
        self._info_var.set(
            f"{n_tirages} tirages analysés sur la période sélectionnée · "
            f"fréquence moyenne théorique par numéro : {n_tirages * 5 / 49:.1f}"
        )
        self._draw(boule_freq, n_tirages)

    def _draw(self, boule_freq: dict, n_tirages: int):
        for w in self._chart_area.winfo_children():
            w.destroy()
        if self._fig is not None:
            import matplotlib.pyplot as plt
            plt.close(self._fig)

        BG_FIG, BG_AX = "#0d0f14", "#13161e"
        TXT, GRID = "#9ca3af", "#1f2535"
        avg = n_tirages * 5 / 49 if n_tirages else 0

        fig = Figure(figsize=(10, 4.6), dpi=100)
        fig.patch.set_facecolor(BG_FIG)
        ax = fig.add_axes([0.045, 0.18, 0.94, 0.76], facecolor=BG_AX)

        numbers = list(boule_freq.keys())
        counts = list(boule_freq.values())
        colors = [COLOR_GREEN if c >= avg else COLOR_RED for c in counts]

        ax.bar(numbers, counts, color=colors, width=0.7, zorder=3)
        ax.axhline(avg, color=COLOR_GOLD, linewidth=1.2, linestyle="--", zorder=2,
                    label=f"Moyenne théorique ({avg:.1f})")
        ax.set_xticks(numbers)
        ax.set_xticklabels(numbers, rotation=90, fontsize=9)
        ax.tick_params(axis="y", colors=TXT, labelsize=10)
        ax.tick_params(axis="x", colors=TXT)
        ax.set_xlim(0.3, 49.7)
        ax.grid(True, axis="y", color=GRID, linewidth=0.5, linestyle="--", zorder=1)
        for sp in ax.spines.values():
            sp.set_edgecolor(GRID)
        ax.set_title("Fréquence de sortie de chaque numéro (boules 1 à 49)",
                      color=COLOR_GOLD, fontsize=13, fontweight="bold")
        ax.legend(facecolor=BG_AX, edgecolor=GRID, labelcolor=TXT, fontsize=10, loc="upper right")

        canvas = FigureCanvasTkAgg(fig, master=self._chart_area)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self._fig, self._canvas = fig, canvas

# =============================================================================
#  ONGLET — NUMÉROS EN RETARD
# =============================================================================

class RetardTab(tk.Frame):
    """Le statut "En retard" / "En avance" utilise la même définition (split par
    médiane, cf. split_en_retard) que le mode "fréquences faibles" du
    générateur de grilles — les deux onglets restent donc cohérents entre eux
    tant qu'ils analysent la même période."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG_MAIN)
        self._df_full = pd.DataFrame()

        tk.Label(self, text="Numéros triés par nombre de tirages depuis leur dernière sortie, "
                             "sur la période sélectionnée",
                 font=FONT_SMALL, fg=COLOR_GRAY, bg=BG_MAIN).pack(pady=(8, 2))
        tk.Label(self,
                 text="Un numéro est « en retard » quand l'écart dépasse la médiane des "
                      "écarts des 49 numéros sur la période choisie (ainsi la moitié "
                      "des numéros sont classés « en retard » à tout instant).",
                 font=FONT_SMALL, fg=COLOR_BLUE, bg=BG_MAIN, wraplength=900,
                 justify="center").pack(pady=(0, 8))

        header = tk.Frame(self, bg=BG_HEADER)
        header.pack(fill="x", padx=40)
        for txt, w in [("Numéro", 10), ("Écart (tirages)", 18), ("Statut", 16)]:
            tk.Label(header, text=txt, font=FONT_HEADER, fg=COLOR_GOLD,
                     bg=BG_HEADER, width=w, anchor="w").pack(side="left", padx=4, pady=6)

        outer = tk.Frame(self, bg=BG_MAIN)
        outer.pack(fill="both", expand=True, padx=40)
        canvas = tk.Canvas(outer, bg=BG_MAIN, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self._rows_frame = tk.Frame(canvas, bg=BG_MAIN)
        win_id = canvas.create_window((0, 0), window=self._rows_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        self._rows_frame.bind("<Configure>",
                               lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        self._canvas = canvas

    def set_data(self, df: pd.DataFrame):
        self._df_full = df

    def apply_period(self, start, end):
        subset = self._df_full[(self._df_full["date_tirage"] >= start) &
                                (self._df_full["date_tirage"] <= end)]
        self._render(subset)

    def _render(self, df: pd.DataFrame):
        for w in self._rows_frame.winfo_children():
            w.destroy()
        if df.empty:
            return
        ecarts = compute_ecarts(df)
        retard_set, _ = split_en_retard(ecarts)
        ordered = sorted(ecarts.items(), key=lambda kv: kv[1], reverse=True)
        for i, (num, ecart) in enumerate(ordered):
            bg = BG_ROW_ODD if i % 2 else BG_ROW_EVEN
            line = tk.Frame(self._rows_frame, bg=bg)
            line.pack(fill="x")
            en_retard = num in retard_set
            color = COLOR_RED if en_retard else COLOR_GREEN
            statut = "⚠ En retard" if en_retard else "✓ En avance"
            tk.Label(line, text=f"{num:02d}", font=FONT_DATA_B, fg=COLOR_BLUE,
                     bg=bg, width=10, anchor="w").pack(side="left", padx=4, pady=4)
            tk.Label(line, text=str(ecart), font=FONT_DATA, fg=color,
                     bg=bg, width=18, anchor="w").pack(side="left", padx=4)
            tk.Label(line, text=statut, font=FONT_DATA, fg=color,
                     bg=bg, width=16, anchor="w").pack(side="left", padx=4)

# =============================================================================
#  ONGLET — GÉNÉRATEUR DE GRILLES
# =============================================================================

class GeneratorTab(tk.Frame):
    """Deux modes :
      1) Aléatoire pur          -> tirage équiprobable parmi 1-49 / 1-10
      2) Basé sur l'historique  -> fréquences ET écarts calculés sur la MÊME
         période (le même sélecteur Début/Fin que les onglets Statistiques et
         Numéros en retard, pour rester directement comparable), avec
         pondération au choix vers les numéros les plus sortis (hors retard)
         ou au contraire les numéros en retard uniquement.
    Rappel statistique assumé dans l'UI : chaque tirage étant indépendant,
    ceci reste un outil ludique, pas un outil de prédiction."""

    MIN_TIRAGES = 20  # en dessous, fréquences/écarts ne sont pas assez significatifs

    def __init__(self, parent):
        super().__init__(parent, bg=BG_MAIN)
        self._df = pd.DataFrame()
        self._period_start = None
        self._period_end = None
        self._hist_widgets: list[tk.Widget] = []
        self._build()

    def set_data(self, df: pd.DataFrame):
        self._df = df

    def apply_period(self, start, end):
        """Le générateur ne se re-rend pas automatiquement (il n'agit qu'au
        clic sur « Générer »), mais retient la période courante — partagée
        avec les autres onglets — pour la prochaine génération."""
        self._period_start, self._period_end = start, end

    # ── construction UI ──

    def _build(self):
        tk.Label(self, text="Mode de génération", font=FONT_HEADER,
                  fg=COLOR_GOLD, bg=BG_MAIN).pack(anchor="w", padx=20, pady=(16, 4))

        self._mode_var = tk.StringVar(value="aleatoire")
        mode_frame = tk.Frame(self, bg=BG_MAIN)
        mode_frame.pack(fill="x", padx=20)
        tk.Radiobutton(
            mode_frame, text="① Aléatoire pur (chaque numéro équiprobable)",
            variable=self._mode_var, value="aleatoire", command=self._on_mode_change,
            font=FONT_DATA, fg=COLOR_WHITE, bg=BG_MAIN, selectcolor=BG_PANEL,
            activebackground=BG_MAIN, activeforeground=COLOR_WHITE,
        ).pack(anchor="w", pady=2)
        tk.Radiobutton(
            mode_frame, text="② Basé sur l'historique des tirages (période définie en haut de fenêtre)",
            variable=self._mode_var, value="historique", command=self._on_mode_change,
            font=FONT_DATA, fg=COLOR_WHITE, bg=BG_MAIN, selectcolor=BG_PANEL,
            activebackground=BG_MAIN, activeforeground=COLOR_WHITE,
        ).pack(anchor="w", pady=2)

        # -- Panneau d'options historique --
        hist_panel = tk.Frame(self, bg=BG_PANEL)
        hist_panel.pack(fill="x", padx=20, pady=10)

        row2 = tk.Frame(hist_panel, bg=BG_PANEL)
        row2.pack(fill="x", padx=14, pady=12)
        lbl2 = tk.Label(row2, text="Numéros à privilégier :", font=FONT_DATA,
                         fg=COLOR_WHITE, bg=BG_PANEL)
        lbl2.pack(side="left")
        self._freq_var = tk.StringVar(value="elevees")
        TXT_HIGH = "Fréquences élevées (hors numéros en retard)"
        TXT_LOW = "Fréquences faibles (numéros en retard uniquement)"
        RB_WIDTH = max(len(TXT_HIGH), len(TXT_LOW)) + 4  # marge de sécurité - taille du cadre
        rb_high = tk.Radiobutton(
            row2, text=TXT_HIGH,
            variable=self._freq_var, value="elevees", font=FONT_DATA,
            fg=COLOR_GREEN, bg=BG_PANEL, selectcolor=BG_HEADER,
            activebackground=BG_PANEL, activeforeground=COLOR_GREEN, state="disabled",
            width=RB_WIDTH, anchor="w", justify="left",
        )
        rb_high.pack(anchor="w", pady=1)
        rb_low = tk.Radiobutton(
            row2, text=TXT_LOW,
            variable=self._freq_var, value="faibles", font=FONT_DATA,
            fg=COLOR_RED, bg=BG_PANEL, selectcolor=BG_HEADER,
            activebackground=BG_PANEL, activeforeground=COLOR_RED, state="disabled",
            width=RB_WIDTH, anchor="w", justify="left",
        )
        rb_low.pack(anchor="w", pady=1)

        self._hist_widgets = [lbl2, rb_high, rb_low]

        tk.Button(
            self, text="🎲 Générer une grille", font=FONT_HEADER, fg="#0d0f14",
            bg=COLOR_GOLD, activebackground="#ffd666", relief="flat", bd=0,
            padx=16, pady=8, cursor="hand2", command=self._generate,
        ).pack(pady=16)

        self._result_canvas = tk.Canvas(self, bg=BG_MAIN, height=90, highlightthickness=0)
        self._result_canvas.pack(fill="x", padx=20)

        self._info_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._info_var, font=FONT_SMALL, fg=COLOR_GRAY,
                  bg=BG_MAIN, wraplength=900, justify="center").pack(pady=6)

        tk.Label(
            self,
            text="⚠ Le Loto est un jeu de hasard pur : chaque tirage est indépendant des précédents "
                 "et aucune méthode ne peut prédire un résultat. \n"
                 "Ceci est un générateur ludique, pas un outil de prédiction.",
            font=FONT_SMALL, fg=COLOR_ORANGE, bg=BG_MAIN, wraplength=700, justify="center",
        ).pack(pady=(10, 4))

        self._on_mode_change()

    def _on_mode_change(self):
        active = self._mode_var.get() == "historique"
        for w in self._hist_widgets:
            self._set_state(w, active)

    @staticmethod
    def _set_state(widget, active: bool):
        try:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state=("readonly" if active else "disabled"))
            else:
                widget.configure(state=("normal" if active else "disabled"))
        except tk.TclError:
            pass

    # ── génération ──

    def _generate(self):
        if self._mode_var.get() == "aleatoire":
            boules = sorted(random.sample(BOULE_NUMBERS, 5))
            chance = random.choice(CHANCE_NUMBERS)
            self._info_var.set("Grille générée aléatoirement : chaque numéro a une probabilité égale.")
            self._draw_balls(boules, chance)
            return

        if self._df.empty:
            messagebox.showinfo(
                "Générateur",
                "Aucun historique chargé pour l'instant — actualisez d'abord les données "
                "(onglet principal, bouton « Actualiser »).",
            )
            return

        start, end = self._period_start, self._period_end
        if start is None:
            messagebox.showinfo("Générateur", "Sélectionnez une période valide en haut de la fenêtre.")
            return
        subset = self._df[(self._df["date_tirage"] >= start) & (self._df["date_tirage"] <= end)]
        if len(subset) < self.MIN_TIRAGES:
            messagebox.showinfo(
                "Générateur",
                f"Seulement {len(subset)} tirage(s) sur cette période : c'est trop peu pour un "
                f"calcul de fréquence/écart fiable (minimum recommandé : {self.MIN_TIRAGES}). "
                "Choisissez une période plus longue.",
            )
            return

        boule_freq, chance_freq = compute_frequencies(subset)

        # Écarts calculés sur la MÊME période que les fréquences — et sur la
        # MÊME période, partagée avec l'onglet "Numéros en retard", pour que
        # les deux restent directement comparables.
        boule_ecarts = compute_ecarts(subset)
        chance_ecarts = compute_ecarts_chance(subset)
        retard_boules, non_retard_boules = split_en_retard(boule_ecarts)
        retard_chance, non_retard_chance = split_en_retard(chance_ecarts)

        favor_high = self._freq_var.get() == "elevees"
        if favor_high:
            # Fréquences élevées : on écarte les numéros actuellement en retard,
            # même s'ils sont historiquement fréquents.
            pool_boules = {n: f for n, f in boule_freq.items() if n in non_retard_boules}
            pool_chance = {n: f for n, f in chance_freq.items() if n in non_retard_chance}
        else:
            # Fréquences faibles : on ne pioche QUE parmi les numéros en retard.
            pool_boules = {n: f for n, f in boule_freq.items() if n in retard_boules}
            pool_chance = {n: f for n, f in chance_freq.items() if n in retard_chance}

        # Garde-fou : si jamais le pool est trop petit (période très courte),
        # on l'élargit — mais on ne le fait pas en silence :
        # l'utilisateur en est explicitement informé dans le message ci-dessous.
        relaxed_boules = relaxed_chance = False
        if len(pool_boules) < 5:
            pool_boules = boule_freq
            relaxed_boules = True
        if not pool_chance:
            pool_chance = chance_freq
            relaxed_chance = True

        boules = sorted(self._weighted_pick(pool_boules, 5, favor_high))
        chance = self._weighted_pick(pool_chance, 1, favor_high)[0]

        label = "élevées" if favor_high else "faibles"
        note = ("en excluant les numéros en retard" if favor_high
                else "en ne gardant que les numéros en retard")
        period_txt = f"{start.strftime('%m/%Y')} → {end.strftime('%m/%Y')}"
        msg = (f"{len(subset)} tirages analysés du {period_txt} — "
               f"numéros à fréquences {label} privilégiés,\n {note}.")
        if relaxed_boules or relaxed_chance:
            cible = "boules ET numéro chance" if (relaxed_boules and relaxed_chance) else (
                "boules" if relaxed_boules else "numéro chance")
            msg += (f" ⚠ Trop peu de candidats sur cette période pour respecter la contrainte "
                    f"« en retard » ({cible}) — contrainte assouplie pour ce tirage.")
        self._info_var.set(msg)
        self._draw_balls(boules, chance)

    @staticmethod
    def _weighted_pick(freq: dict, k: int, favor_high: bool) -> list:
        """Tirage pondéré sans remise. favor_high=True -> les numéros les plus
        fréquents ont plus de chances d'être choisis ; False -> l'inverse."""
        max_freq = max(freq.values()) if freq else 0
        if favor_high:
            weights = {n: f + 1 for n, f in freq.items()}
        else:
            weights = {n: (max_freq - f) + 1 for n, f in freq.items()}

        items = list(weights.keys())
        w = [float(weights[i]) for i in items]
        chosen = []
        for _ in range(min(k, len(items))):
            total = sum(w)
            r = random.uniform(0, total)
            upto = 0.0
            for idx, (it, wt) in enumerate(zip(items, w)):
                upto += wt
                if upto >= r:
                    chosen.append(it)
                    del items[idx]
                    del w[idx]
                    break
        return chosen

    def _draw_balls(self, boules: list, chance: int):
        c = self._result_canvas
        c.delete("all")
        c.update_idletasks()
        width = max(c.winfo_width(), 560)
        r, gap = 26, 18
        n = len(boules) + 1
        total_w = n * (2 * r) + (n - 1) * gap
        x = (width - total_w) / 2 + r
        y = 45
        for b in boules:
            c.create_oval(x - r, y - r, x + r, y + r, fill=COLOR_BLUE,
                           outline=COLOR_WHITE, width=2)
            c.create_text(x, y, text=f"{b:02d}", font=("Courier New", 14, "bold"), fill="#0d0f14")
            x += 2 * r + gap
        c.create_oval(x - r, y - r, x + r, y + r, fill=COLOR_ORANGE,
                       outline=COLOR_WHITE, width=2)
        c.create_text(x, y, text=f"{chance:02d}", font=("Courier New", 14, "bold"), fill="#0d0f14")

# =============================================================================
#  FENÊTRE PRINCIPALE
# =============================================================================

class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title("Tableau de Bord - Loto FDJ")
        self.geometry("1150x780")
        self.minsize(1000, 650)
        self.configure(bg=BG_MAIN)

        self._df = pd.DataFrame()
        self._stop_event = threading.Event()
        self._period_initialized = False

        splash = SplashScreen(self)
        self.after(SPLASH_DURATION_MS, lambda: self._start(splash))

    # ── Démarrage ──

    def _start(self, splash):
        splash.destroy()
        self.deiconify()
        self._build_ui()
        self._manual_refresh()
        self._schedule_auto_refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        top = tk.Frame(self, bg=BG_HEADER)
        top.pack(fill="x")
        tk.Label(top, text="🎱 LOTO — Tableau de Bord", font=FONT_TITLE,
                 fg=COLOR_GOLD, bg=BG_HEADER, pady=8, padx=10).pack(side="left")
        self._btn_refresh = tk.Button(
            top, text="⟳ Actualiser", font=FONT_SMALL, fg=COLOR_GOLD, bg="#252a38",
            activeforeground=COLOR_WHITE, activebackground="#353a50",
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
            command=self._manual_refresh,
        )
        self._btn_refresh.pack(side="right", padx=(0, 10), pady=6)

        self._btn_reimport = tk.Button(
            top, text="⟲ Réimporter tout l'historique", font=FONT_SMALL, fg=COLOR_ORANGE,
            bg="#252a38", activeforeground=COLOR_WHITE, activebackground="#353a50",
            relief="flat", bd=0, padx=10, pady=4, cursor="hand2",
            command=self._force_full_reimport,
        )
        self._btn_reimport.pack(side="right", padx=(0, 6), pady=6)

        self._status_var = tk.StringVar(value="")

        self._period_selector = PeriodSelector(self, on_apply=self._on_period_change)
        self._period_selector.pack(fill="x")

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background=BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_HEADER, foreground=COLOR_WHITE,
                         padding=[14, 6], font=FONT_HEADER)
        style.map("TNotebook.Tab", background=[("selected", BG_PANEL)],
                   foreground=[("selected", COLOR_GOLD)])

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True)

        self._tab_derniers  = DerniersTiragesTab(self._nb)
        self._tab_stats     = StatistiquesTab(self._nb)
        self._tab_retard    = RetardTab(self._nb)
        self._tab_generator = GeneratorTab(self._nb)

        self._nb.add(self._tab_derniers,  text="Derniers tirages")
        self._nb.add(self._tab_stats,     text="Statistiques")
        self._nb.add(self._tab_retard,    text="Numéros en retard")
        self._nb.add(self._tab_generator, text="Générateur de grilles")

        status_bar = tk.Frame(self, bg=BG_HEADER)
        status_bar.pack(fill="x", side="bottom")
        tk.Label(status_bar, textvariable=self._status_var, font=FONT_STATUS,
                 fg=COLOR_GRAY, bg=BG_HEADER, anchor="w").pack(fill="x", padx=10, pady=3)

    def _force_full_reimport(self):
        if not messagebox.askyesno(
            "Réimporter tout l'historique ",
            "depuis la FDJ (1976 → aujourd'hui). "
            "Cela peut prendre quelques dizaines de secondes. Continuer ?",
        ):
            return
        purged = purge_and_reset_history()
        if purged:
            self._status_var.set(f"🧹 {purged} ligne(s) suspecte(s) purgée(s) — réimport en cours…")
        self._manual_refresh()

    def _on_period_change(self, start, end):
        """Appelé quand l'utilisateur clique Appliquer / Tout l'historique
        sur le sélecteur partagé. On persiste le DÉBUT choisi (pas la fin,
        toujours resynchronisée sur le dernier tirage connu à chaque
        rafraîchissement), puis on répercute sur tous les onglets."""
        cfg = load_config()
        cfg["period_start"] = start.strftime("%Y-%m")
        save_config(cfg)
        self._apply_period_everywhere(start, end)

    def _apply_period_everywhere(self, start, end):
        self._tab_derniers.apply_period(start, end)
        self._tab_stats.apply_period(start, end)
        self._tab_retard.apply_period(start, end)
        self._tab_generator.apply_period(start, end)

    # ── Rafraîchissement ──

    def _manual_refresh(self):
        self._btn_refresh.config(state="disabled")
        self._status_var.set("⏳ Mise à jour depuis fdj.fr…")

        def worker():
            new_count, errors = update_history(self._stop_event)
            df = load_all_draws()
            self.after(0, lambda: self._on_refresh_done(new_count, errors, df))

        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_done(self, new_count, errors, df):
        self._btn_refresh.config(state="normal")
        self._df = df

        if df.empty:
            self._status_var.set("⚠ Aucune donnée disponible.")
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            msg = f"✓ {len(df)} tirages en base"
            if new_count:
                msg += f"  ({new_count} nouveau(x))"
            msg += f"  ·  màj {ts}"
            self._status_var.set(msg)
            self._tab_derniers.set_data(df)
            self._tab_stats.set_data(df)
            self._tab_retard.set_data(df)
            self._tab_generator.set_data(df)

            persisted_start = None
            if not self._period_initialized:
                persisted_start = load_config().get("period_start")
            self._period_selector.set_bounds(
                df["date_tirage"].min(), df["date_tirage"].max(),
                default_start=persisted_start, keep_start=self._period_initialized,
            )
            self._period_initialized = True
            start, end = self._period_selector.get_selection()
            if start is not None:
                self._apply_period_everywhere(start, end)

        if errors:
            messagebox.showwarning(
                "Avertissements",
                "Certaines archives n'ont pas pu être récupérées :\n\n" + "\n".join(errors),
            )

    def _schedule_auto_refresh(self):
        def tick():
            self._manual_refresh()
            self.after(AUTO_REFRESH_INTERVAL_MS, tick)
        self.after(AUTO_REFRESH_INTERVAL_MS, tick)

    def _on_close(self):
        self._stop_event.set()
        self.destroy()

# ─── POINT D'ENTRÉE ───

if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
