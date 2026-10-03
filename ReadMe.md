# Loto Dashboard 🎱

Tableau de bord Python (Tkinter) tournant en local sur **Raspberry Pi 5**, qui suit l'historique complet des tirages du **Loto FDJ**, calcule des statistiques de fréquence et d'écart, et génère des grilles — thème sombre, architecture thread de fond + rafraîchissement auto.

Le projet tient dans un seul fichier :
- **`loto_dashboard.py`** — l'application complète : scraping FDJ, base SQLite, interface graphique à 4 onglets synchronisés, générateur de grilles

Un second script autonome est disponible pour un usage en ligne de commande / cron, sans interface :
- **`loto_scraper.py`** — récupère et exporte l'historique complet en CSV + SQLite (pas d'interface, pas de dashboard)

Les deux scripts partagent la même logique de récupération et de parsing (voir ci-dessous).

## Architecture générale
```
┌────────────────────────────────────────────────────────────────┐
│                       loto_dashboard.py                        │
│                                                                │
│  ┌──────────────┐  ┌─────────────┐  ┌──────────────────────┐   │
│  │Scraper FDJ   │  │Normalisation│  │Base SQLite           │   │
│  │liens ZIP     │  │multi-       │  │loto_historique       │   │
│  │dynamiques    │  │stratégies   │  │.sqlite3 (incrémental)│   │
│  └──────────────┘  └─────────────┘  └──────────────────────┘   │
│                 ┌────────────────────────────────┐             │
│                 │  Sélecteur de période PARTAGÉ  │             │
│                 │  (Début / Fin, un seul pour    │             │
│                 │  les 4 onglets)                │             │
│                 └────────────────┬───────────────┘             │
│  ┌──────────────┐  ┌─────────────┐  ┌──────────────────────┐   │
│  │Derniers      │  │Statistiques │  │Numéros en retard     │   │
│  │tirages       │  │fréquence    │  │calcul des écarts     │   │
│  │(Treeview,    │  │(matplotlib) │  │(médiane des écarts)  │   │
│  │ascenseur)    │  │             │  │                      │   │
│  └──────────────┘  └─────────────┘  └──────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │Générateur de grilles                                     │  │
│  │aléatoire pur · pondéré (fréquence ET statut "en retard") │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬──────────────────────────────┘
                                  │ toutes les 24h (thread de fond)
                   ┌──────────────┴──────────────────┐
                   │  Mise à jour incrémentale       │
                   │  archive la plus récente        │
                   │  uniquement                     │
                   │  (après 1er backfill complet)   │
                   └─────────────────────────────────┘
```

## Fonctionnalités

### 🌐 Source des données & mise à jour incrémentale
- Source unique et fiable : les archives ZIP/CSV officielles de la FDJ (`fdj.fr/jeux-de-tirage/loto/historique`), et non des portails tiers dont l'URL et le format ne sont pas garantis dans le temps.
- Les liens de téléchargement FDJ contiennent un identifiant qui change au fil du temps : ils sont donc **relus par scraping à chaque mise à jour**, jamais codés en dur.
- **1er lancement** : backfill complet (5 archives, 1976 → aujourd'hui).
- **Lancements suivants** : seule l'archive la plus récente est retéléchargée (les périodes passées ne changent jamais a posteriori) — beaucoup plus rapide, respectueux du site FDJ.

### 📅 Parsing des dates (multi-stratégies)
Le format des CSV FDJ n'est pas homogène entre 1976 et aujourd'hui. `normalize()` teste **toutes** les colonnes contenant "date" avec **3 stratégies** de parsing (chaîne jour/mois/année, entier AAAAMMJJ, numéro de série type Excel) et retient la combinaison qui produit le plus de dates plausibles (entre 1976 et aujourd'hui). Un diagnostic (`{kept}/{raw} lignes conservées`) est calculé à chaque archive ; si le taux de conservation d'une archive tombe sous 50 %, un avertissement est levé plutôt que de laisser passer des données silencieusement incomplètes.

### 🔢 Un jour, deux tirages
Avant octobre 2008, le Loto avait parfois un **second tirage** le même jour (deux grilles différentes tirées à la même date). La base SQLite utilise donc une clé **composite** (date + les 5 boules), pas la date seule — sinon le second tirage d'une journée était silencieusement perdu. Une migration automatique s'exécute au démarrage si une base créée avec l'ancien schéma est détectée.

### 🗂️ Persistance
| Fichier                   | Contenu                                                                                         |
|---                        |---                                                                                              |
| `loto_historique.sqlite3` | Table `tirages` (clé composite date + boules), historique complet                               |
| `loto_config.json`        | Indicateur de backfill complet, horodatage de dernière mise à jour, période de départ persistée |

### 🖥️ Interface (4 onglets synchronisés)
Un **sélecteur de période unique** (Début / Fin, granularité mois) est placé au-dessus des onglets et s'applique aux 4 en même temps — plus besoin de régler la période séparément dans chaque onglet.
- La borne **Fin** est toujours réinitialisée sur le dernier tirage connu à chaque rafraîchissement (jamais figée sur une ancienne valeur).
- La borne **Début** est persistée dans `loto_config.json` dès qu'on clique « Appliquer » ou « Tout l'historique », et restaurée au lancement suivant.

| Onglet                    | Description                                                                                                             |
|---                        |---                                                                                                                      |
| **Derniers tirages**      | Liste des tirages sur la période (date, 5 boules, numéro chance), dans un tableau avec ascenseur natif (`ttk.Treeview`) |
| **Statistiques**          | Histogramme matplotlib de la fréquence de sortie des 49 numéros sur la période, avec ligne de moyenne théorique         |
| **Numéros en retard**     | Classement des numéros par nombre de tirages écoulés depuis leur dernière sortie / la période. Un numéro est « en retard » quand son écart dépasse la **médiane** des écarts des 49 numéros (donc environ la moitié des numéros sont toujours classés « en retard », le seuil s'ajuste à la période) |
| **Générateur de grilles** | Voir ci-dessous                                                                                                         |

### 🎲 Générateur de grilles
Deux modes, sélectionnables par bouton radio, sur la **période partagée** :

| Mode                        | Détail                                                   |
|---                          |---                                                       |
| **① Aléatoire pur**         | 5 boules + 1 numéro chance tirés équiprobablement        |
| **② Basé sur l'historique** | Fréquences **et** écarts calculés sur la même période, puis tirage pondéré **sans remise** :<br>• *Fréquences élevées* → exclut les numéros actuellement en retard, même s'ils sont historiquement fréquents<br>• *Fréquences faibles* → ne pioche **que** parmi les numéros en retard             |

Un seuil minimum de 20 tirages est requis pour activer le mode historique (période trop courte sinon = statistiques peu fiables). Si le pool de candidats devient trop restreint sur une période très courte, la contrainte est assouplie — et l'utilisateur en est explicitement informé dans le message, jamais en silence.

Le résultat s'affiche sous forme de boules dessinées sur un canvas (bleu pour les numéros, orange pour le numéro chance). Un rappel reste visible en permanence : chaque tirage étant indépendant des précédents, il s'agit d'un outil ludique, pas d'un outil de prédiction.

### 🔄 Rafraîchissement
- Automatique toutes les 24h (les tirages ont lieu lundi/mercredi/samedi soir — inutile de scruter en continu comme pour la bourse).
- Bouton « ⟳ Actualiser » pour forcer une mise à jour manuelle à tout moment.
- Bouton « ⟲ Réimporter tout l'historique » pour purger les éventuels artefacts de dates aberrantes et forcer un backfill complet (utile après une évolution du script de parsing).
- Le téléchargement se fait dans un **thread séparé** (`threading.Thread` + `self.after(0, …)`) pour ne jamais geler l'interface.

### 🎨 Splash & thème
- Splash screen affichant `~/Projects/Tirages_Loto/icons/loto.png` (redimensionnée automatiquement), avec repli sur un texte stylisé si le fichier est absent.
- Thème sombre (fond `#0d0f14`, accents or `#f0c040`, police Courier New, tailles de police confortables à lire).

## Sécurité & robustesse
- Aucune donnée sensible manipulée (source publique, pas de clé API) — pas de fichier de config à protéger.
- Toute page FDJ dont la structure HTML aurait changé fait échouer le scraping avec une erreur explicite plutôt qu'un plantage silencieux ou des données corrompues.
- Les échecs de téléchargement d'une archive individuelle sont capturés et signalés (fenêtre d'avertissement) sans bloquer les autres archives.
- Diagnostic automatique du taux de conservation par archive lors du parsing (avertissement si < 50 %), pour détecter tôt un éventuel changement de format côté FDJ.
- Insertion en base via `INSERT OR IGNORE` sur la clé composite (date + boules) : une mise à jour relancée plusieurs fois de suite ne crée jamais de doublons, tout en acceptant les jours à deux tirages.
- Migration de schéma automatique et non destructive si une base créée avec une version antérieure du script est détectée.

## Fichiers du projet
```
Tirages_Loto/
├── loto_dashboard.py        # Application principale (interface graphique)
├── loto_scraper.py          # Script CLI autonome (export CSV/SQLite, sans interface)
├── requirements.txt
├── ReadMe.md                # Ce fichier
└── icons/
    └── loto.png             # Icône du splash screen

# Générés automatiquement à côté de loto_dashboard.py (non versionnés)
├── loto_historique.sqlite3  # Base de données des tirages
└── loto_config.json         # État de la mise à jour incrémentale + période persistée
```

## Prérequis
- Python 3.10+
- Raspberry Pi 5 (ou toute machine Linux/Windows/macOS avec Tkinter)
- Accès Internet vers `fdj.fr` (aucune clé API requise, la source est publique)

## Installation
```bash
# Cloner le dépôt
git clone https://github.com/JeanFrancoisBrunet/Tirages_Loto.git
cd Tirages_Loto

# Installer les dépendances
pip install -r requirements.txt --break-system-packages
```

Déposer une icône (PNG, format libre — redimensionnée automatiquement) :
```bash
mkdir -p ~/Projects/Tirages_Loto/icons
cp votre_icone.png ~/Projects/Tirages_Loto/icons/loto.png
```

## Lancement
```bash
python3 loto_dashboard.py
```
⚠️ Le tout premier lancement télécharge l'historique complet (5 archives depuis 1976, ~7600 tirages) : comptez quelques dizaines de secondes selon la connexion. Les lancements suivants sont quasi instantanés (une seule archive récente à retélécharger).

### Utilisation en script seul (sans interface, ex. cron)
```bash
python3 loto_scraper.py
```

Exporte `loto_data/loto_historique.csv` et `loto_data/loto_historique.sqlite3`.

## Limites connues
| Limite               | Détail                                                                                                                             |
|---                   |---                                                                                                                                 |
| Pas d'API officielle | Le scraping dépend de la structure HTML actuelle de `fdj.fr` ; un changement de site peut nécessiter un ajustement du sélecteur    |
| Jeu de hasard        | Statistiques de fréquence et « numéros en retard » sont informatifs uniquement — chaque tirage Loto est indépendant des précédents |

## Fichiers à ne pas versionner
Créer un fichier `.gitignore` à la racine du projet :

```gitignore
# Données générées
loto_historique.sqlite3
loto_config.json
loto_data/

# Fichiers Python générés
__pycache__/
*.pyc
*.pyo
```

## Auteur
**Jean-François Brunet** — [JFBConseils](https://github.com/JeanFrancoisBrunet)
Consultant Lean Management — projet personnel d'un tableau de bord Loto sur Raspberry Pi 5 *Juillet 2026*
