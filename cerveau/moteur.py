# -*- coding: utf-8 -*-
"""
LE MOTEUR DU CERVEAU — llama.cpp lance directement, sans LM Studio.

╔══════════════════════════════════════════════════════════════════════════════╗
║ POURQUOI ON A QUITTE LM STUDIO — mesures du 18/07/2026                       ║
║                                                                               ║
║ Le mouchard a montre que pendant une vraie session, la memoire vive de        ║
║ Utilisateur restait bloquee a 98 % (31,3 Go sur 31,7) du debut a la fin, et ses  ║
║ reponses prenaient 130 s, 47 s, 14 s. Son jeu ramait. Pas par manque de       ║
║ carte graphique : par manque de MEMOIRE VIVE.                                 ║
║                                                                               ║
║     LM Studio                       17,6 Go de RAM                            ║
║     llama.cpp seul, par defaut      17,1 Go de RAM   <- l'emballage n'y etait ║
║                                                          presque pour rien    ║
║     llama.cpp + --no-mmap            4,6 Go de RAM   <- LE reglage            ║
║     + flash attention + cache q8     3,5 Go de RAM                            ║
║                                                                               ║
║ Soit 14 Go rendus a la machine. Il reste ~17 Go pour un jeu, contre 0,4 Go.   ║
║ Vitesse identique des deux cotes (5,1 jetons/s) : on ne paie rien en echange. ║
╚══════════════════════════════════════════════════════════════════════════════╝

⚠️ CE QUI A MIS SUR LA PISTE, ET LES DEUX FAUSSES PISTES QUI ONT PRECEDE :
   1. « c'est un bug de LM Studio » (leur signalement n°1198 decrit exactement le
      symptome) -> FAUX, ou marginal : llama.cpp seul gaspillait pareil (1,6 Go
      d'ecart). Le vrai coupable etait un REGLAGE que LM Studio n'exposait ni en
      ligne de commande ni dans ses fichiers de configuration.
   2. « la puce graphique Intel du processeur prend la moitie du modele, et sa
      memoire video EST la memoire vive » (llama.cpp voit bien deux cartes :
      Vulkan0 = la Radeon, Vulkan1 = l'Intel) -> FAUX aussi, mesure : forcer
      --device Vulkan0 ne change RIEN (3,5 Go et 5,1 jetons/s dans les deux cas).
      llama.cpp choisit deja la bonne carte tout seul.
   LEcON : le vrai coupable etait le plus ennuyeux des trois — un drapeau par
   defaut. Les deux hypotheses seduisantes etaient fausses.

À QUOI SERVENT LES REGLAGES :
   --no-mmap        n'installe pas le fichier de 13,4 Go dans la memoire vive.
                    Le modele part sur la carte, la memoire vive reste libre.
                    C'est LUI qui rapporte 12,5 des 14 Go.
   -fa on           « flash attention » : calcule l'attention sans ecrire les
                    resultats intermediaires. Exige par le cache q8 ci-dessous.
   --cache-type-*   compresse la memoire de conversation (1 Go de plus).
   --parallel 1     un seul emplacement de conversation. On n'en utilise qu'un ;
                    en gerer quatre coutait pres de la moitie de la vitesse.
   -ngl 99          tout sur la carte graphique.

REPLI SI CA TOURNE MAL : mettre MOTEUR = "lmstudio" ci-dessous. LM Studio est
toujours installe, avec le meme modele. Rien d'autre a defaire.
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
import config  # noqa: E402
import urllib.request
from pathlib import Path

# ── choix du moteur : "llamacpp" (le nouveau) ou "lmstudio" (l'ancien, en repli) ──
MOTEUR = "llamacpp"

PROJET = Path(__file__).resolve().parent.parent
SERVEUR = Path(config.SERVEUR_LLAMA)
MODELE_GGUF = Path(config.MODELE_CERVEAU)

LMS = config.CHEMIN_LMS
MODELE_LMS = "mistral-small-3.2-24b-instruct-2506"

PORT = 8095
PORT_EMBEDDINGS = 8096
CONTEXTE = 16384

# Le petit modèle qui transforme les phrases en nombres, pour que la mémoire
# puisse chercher « par le sens ». 81 Mo. Il vient de LM Studio (livré avec
# l'application) — on ne fait que lire son fichier, LM Studio n'a pas à tourner.
MODELE_EMBEDDINGS = Path(config.MODELE_EMBEDDINGS)

API = (f"http://127.0.0.1:{PORT}/v1/chat/completions" if MOTEUR == "llamacpp"
       else "http://localhost:1234/v1/chat/completions")
NOM_MODELE = "local" if MOTEUR == "llamacpp" else MODELE_LMS

# LE PROMPT DE PERSONNALITÉ — UNE SEULE SOURCE, ICI, pour le service vocal ET
# le chat écrit. Corrigé le 21/07/2026 : le chat chargeait encore le v2 pendant
# que le service vocal chargeait le v3 — deux personnalités différentes, et le
# paragraphe anti-« simple programme » du v3 n'était jamais testé par écrit.
# Le commentaire du chat affirmait pourtant « aucune divergence possible » :
# c'est précisément parce que chaque fichier définissait le sien. Même remède
# que pour l'adresse et le nom du modèle : tout le monde lit CETTE ligne.
# REPLI : remettre "alice_prompt_v2.txt" ci-dessous, rien d'autre.
PROMPT_FILE = PROJET / "docs" / "alice_prompt_v3.txt"

OPTIONS = ["-c", str(CONTEXTE), "--parallel", "1", "-ngl", "99",
           "--no-mmap", "-fa", "on",
           "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]


def _lms(*a, timeout=900):
    return subprocess.run([LMS, *a], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def demarrer(tracer=print):
    """Allume le cerveau. Rend l'objet a passer a arreter() (None pour LM Studio)."""
    if MOTEUR == "lmstudio":
        if "running" not in _lms("server", "status").stdout.lower():
            tracer("démarrage du serveur LM Studio...")
            _lms("server", "start")
            time.sleep(3)
        _lms("unload", "--all")
        time.sleep(1)
        tracer(f"chargement du cerveau via LM Studio ({CONTEXTE} jetons)...")
        r = _lms("load", MODELE_LMS, "-c", str(CONTEXTE), "--parallel", "1", "-y")
        if r.returncode != 0:
            raise RuntimeError(f"chargement impossible : {r.stderr[:400]}")
        return None

    # LM Studio pourrait garder un exemplaire du modele sur la carte : 15 Go + 15 Go
    # sur une carte de 24 = debordement, et tout rame. On s'assure qu'elle est vide.
    try:
        _lms("unload", "--all", timeout=120)
    except Exception:
        pass

    dossier = PROJET / "tests" / "logs"
    dossier.mkdir(parents=True, exist_ok=True)

    def _lancer(nom, args, port, attente):
        journal = dossier / f"llamacpp_{nom}.txt"
        p = subprocess.Popen([str(SERVEUR), *args, "--port", str(port),
                              "--host", "127.0.0.1"],
                             stdout=journal.open("w", encoding="utf-8"),
                             stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(attente):
            time.sleep(2)
            if p.poll() is not None:
                raise RuntimeError(f"{nom} s'est arrêté seul — voir {journal}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                            timeout=5) as x:
                    if x.status == 200:
                        return p
            except Exception:
                pass
        p.terminate()
        raise RuntimeError(f"{nom} n'a pas démarré à temps — voir {journal}")

    procs = []
    # LA MÉMOIRE D'ABORD : elle est minuscule (81 Mo) et démarre en 2 s. Sans
    # elle, le tri des souvenirs tourne dans le vide sans le dire — c'est
    # exactement ce qui est arrivé le 18/07 au soir.
    tracer("chargement du modèle de mémoire (81 Mo)...")
    # ⚠️ -c / -b / -ub A 8192 : SANS EUX, LE TRI DE LA MÉMOIRE ÉCHOUE EN SILENCE.
    # Par défaut, ce serveur n'avale que 512 jetons d'un coup. Or le trieur lui
    # envoie la conversation entière à vectoriser. Mesuré le 18/07/2026 :
    #     « input (1077 tokens) is too large to process.
    #       increase the physical batch size (current batch size: 512) »
    # et le rangement rendait « 0 souvenir(s) en 1,3 s » — sans erreur visible
    # ailleurs que dans ce journal-ci. Deuxième panne muette de la mémoire dans
    # la même soirée, pour une cause différente de la première.
    procs.append(_lancer("embeddings",
                         ["-m", str(MODELE_EMBEDDINGS), "--embedding",
                          "-ngl", "99", "--parallel", "1",
                          "-c", "8192", "-b", "8192", "-ub", "8192"],
                         PORT_EMBEDDINGS, 60))

    tracer(f"chargement du cerveau via llama.cpp ({CONTEXTE} jetons)... (~40 s)")
    procs.append(_lancer("cerveau",
                         ["-m", str(MODELE_GGUF), *OPTIONS], PORT, 240))
    tracer("cerveau chaud.")
    return procs


def arreter(p, tracer=print):
    """Eteint le cerveau et rend la memoire video. A appeler SANS FAUTE en partant."""
    if MOTEUR == "lmstudio":
        tracer("déchargement du cerveau...")
        try:
            _lms("unload", "--all", timeout=300)
        except Exception:
            pass
        return
    if not p:
        return
    tracer("déchargement du cerveau...")
    for proc in (p if isinstance(p, (list, tuple)) else [p]):
        try:
            proc.terminate()
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
