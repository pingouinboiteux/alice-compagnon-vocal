"""La frontiere : ce que l'interface a le droit de connaitre.

L'interface graphique doit rester une fenetre. Elle peut jouer un WAV deja
fabrique avec `winsound`, mais elle n'ouvre jamais le micro et ne charge aucun
moteur vocal ni modele. Sinon le poids et les pannes reviendraient dans le
programme que l'utilisateur regarde.

Le controle est statique : on lit le code, on ne l'execute pas. Il porte sur le
graphe d'imports, pas sur ce qu'un appelant pourrait charger dynamiquement.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent

#: Ce qui ne doit jamais entrer dans l'interface ni dans le protocole.
INTERDITS = frozenset(
    {
        "boucle_alice",
        "interface_alice",
        "menage",
        "sounddevice",
        "pyaudio",
        "openwakeword",
        "webrtcvad",
        "torch",
        "onnxruntime",
        "onnx_asr",
        "piper",
        "chromadb",
        "mem0",
        "transformers",
        "numpy",
        "requests",
    }
)

#: Les seuls modules exterieurs autorises dans l'interface.
AUTORISES_INTERFACE = frozenset({"PySide6", "commun", "interface", "pont_discord"})


def fichiers(dossier: str) -> list[Path]:
    return sorted((RACINE / dossier).glob("*.py"))


def racines_importees(chemin: Path) -> set[str]:
    arbre = ast.parse(chemin.read_text(encoding="utf-8"), filename=str(chemin))
    trouvees: set[str] = set()
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            for alias in noeud.names:
                trouvees.add(alias.name.split(".")[0])
        elif isinstance(noeud, ast.ImportFrom):
            if noeud.level == 0 and noeud.module:
                trouvees.add(noeud.module.split(".")[0])
    return trouvees


@pytest.mark.parametrize("chemin", fichiers("interface") + [RACINE / "alice_control_center.py"])
def test_l_interface_n_importe_rien_de_lourd(chemin):
    importees = racines_importees(chemin)
    fautives = sorted(importees & INTERDITS)
    assert not fautives, f"{chemin.name} importe {fautives}"


@pytest.mark.parametrize("chemin", fichiers("interface") + [RACINE / "alice_control_center.py"])
def test_l_interface_reste_dans_sa_liste_blanche(chemin):
    """Une liste blanche, pas une liste noire.

    Une liste de mots interdits ne protege que contre ce qu'on a su nommer. La
    lecon de l'inventaire GPU vaut ici : on autorise, on n'interdit pas.
    """
    standard = set(sys.stdlib_module_names)
    dehors = sorted(racines_importees(chemin) - standard - AUTORISES_INTERFACE)
    assert not dehors, f"{chemin.name} importe {dehors}, hors liste blanche"


@pytest.mark.parametrize("chemin", fichiers("commun"))
def test_le_protocole_ne_depend_que_de_la_bibliotheque_standard(chemin):
    """Le protocole est parle par deux interpreteurs differents.

    S'il dependait d'un paquet installe, il faudrait l'installer des deux cotes,
    et la frontiere se percerait par la porte de service.
    """
    dehors = sorted(racines_importees(chemin) - set(sys.stdlib_module_names) - {"commun"})
    assert not dehors, f"{chemin.name} depend de {dehors}"


@pytest.mark.parametrize("chemin", fichiers("hote"))
def test_l_hote_ne_depend_que_de_la_bibliotheque_standard(chemin):
    """L'hote tourne avec un interpreteur de V3, qu'on ne modifie pas.

    Lui demander un paquet supplementaire obligerait a installer quelque chose
    dans un environnement d'Alice V3, ce que ce sous-projet s'interdit.
    """
    # `menage` est la seule exception, et c'est tout l'interet de l'hote : il
    # reutilise l'enclos existant de V3 au lieu d'en fabriquer un second.
    permis = set(sys.stdlib_module_names) | {"commun", "hote", "menage"}
    dehors = sorted(racines_importees(chemin) - permis)
    assert not dehors, f"{chemin.name} depend de {dehors}"


def test_l_environnement_de_l_interface_ne_contient_ni_audio_ni_modele():
    """La preuve par le disque, pas seulement par le code.

    Un import interdit se verrait dans le code ; une dependance interdite
    installee « au cas ou » ne se verrait nulle part. On regarde donc le venv.
    """
    paquets = RACINE / "venv_interface" / "Lib" / "site-packages"
    if not paquets.exists():
        pytest.skip("environnement de l'interface absent")
    presents = {chemin.name.split("-")[0].lower() for chemin in paquets.glob("*.dist-info")}
    fautifs = sorted(
        nom
        for nom in presents
        if nom.replace("_", "") in {i.replace("_", "").lower() for i in INTERDITS}
    )
    assert not fautifs, f"l'environnement graphique contient {fautifs}"


def test_seul_l_adaptateur_separe_peut_atteindre_le_micro():
    """La regle PortAudio : un seul programme du Control Center peut l'ouvrir.

    On cherche les portes d'entree de l'audio dans tout le sous-projet, code de
    test compris. L'interface et l'hote restent incapables de le faire.
    """
    portes = re.compile(
        r"\b(sounddevice|pyaudio|InputStream|open_stream|paInt16|interface_alice)\b"
    )
    # Deux fichiers nomment ces bibliotheques exprès, pour les interdire : ce
    # test lui-meme, et le controle que l'interface fait sur elle-meme au
    # demarrage. Les exclure des mots-cles ne les exempte de rien — le controle
    # d'imports ci-dessus, lui, les couvre comme les autres.
    # ⚠️ `test_lipsync.py` est le quatrieme, ajoute le 10/08 : il interdit
    # exactement les memes portes d'entree, mais du cote du CORPS (« la
    # musique du jeu n'ouvre plus sa bouche », critere C4). Deux gardiens qui
    # se denoncent l'un l'autre — ce test-ci l'a attrape des sa premiere passe,
    # et c'est la preuve qu'il fait son travail.
    gardiens = {"test_frontiere.py", "canal.py", "test_fenetre.py",
                "test_lipsync.py"}
    coupables = []
    for chemin in RACINE.rglob("*.py"):
        # 🔴 TOUTE bulle Python est hors sujet, pas seulement `venv_interface`.
        # Ce test cherche NOTRE code ; les bibliotheques tierces ne sont pas
        # des portes que nous ouvrons. La liste nominative a mordu des l'ajout
        # de `venv_twitch` le 07/08 : un lexer de Pygments contient le mot
        # « InputStream », et la frontiere audio s'est declaree cassee. Un
        # garde-fou qui hurle sur du bruit finit debranche — on le rend juste
        # au lieu de rallonger la liste a chaque nouvelle bulle.
        if (
            any(partie.startswith("venv") for partie in chemin.parts)
            or "deploiement" in chemin.parts
            or chemin.name in gardiens
        ):
            continue
        if portes.search(chemin.read_text(encoding="utf-8")):
            coupables.append(chemin.name)
    assert coupables == ["micro_adaptateur.py"], (
        f"la frontiere audio n'a plus un proprietaire unique : {coupables}"
    )

    adaptateur = RACINE / "micro_adaptateur.py"
    assert "Interface.ecouter" in adaptateur.read_text(encoding="utf-8")
    assert not (racines_importees(adaptateur) & {"sounddevice", "pyaudio"}), (
        "l'adaptateur doit reutiliser la boucle validee, pas ouvrir un second flux"
    )

    # Et les gardiens eux-memes n'importent rien de tout cela.
    for nom in gardiens:
        for chemin in RACINE.rglob(nom):
            if "venv_interface" in chemin.parts:
                continue
            fautifs = sorted(racines_importees(chemin) & INTERDITS)
            assert not fautifs, f"{nom} importe {fautifs}"


def test_aucun_secret_dans_le_sous_projet():
    """Aucun jeton, aucun mot de passe, nulle part.

    Le futur jeton Discord ira dans le coffre de Windows. Ce test existe pour
    que « plus tard » ne devienne pas « dans un fichier, provisoirement ».
    """
    suspects = re.compile(
        r"(discord[_-]?token|bot[_-]?token|api[_-]?key\s*=\s*['\"][A-Za-z0-9_\-]{16,})",
        re.IGNORECASE,
    )
    coupables = []
    for chemin in RACINE.rglob("*"):
        if not chemin.is_file() or "venv_interface" in chemin.parts:
            continue
        if chemin.suffix.lower() not in {".py", ".md", ".json", ".txt", ".ps1", ".bat", ".log"}:
            continue
        texte = chemin.read_text(encoding="utf-8", errors="ignore")
        if suspects.search(texte):
            coupables.append(chemin.name)
    assert not coupables, f"secret possible dans {coupables}"


def test_le_raccourci_du_bureau_est_intact():
    """`Alice.lnk` doit toujours lancer l'ancien programme.

    Tant que l'utilisateur n'a pas valide ce tableau de bord, son raccourci habituel
    ne change pas. Ce test le verifie a chaque campagne.
    """
    raccourci = Path.home() / "Desktop" / "Alice.lnk"
    if not raccourci.exists():
        pytest.skip("raccourci absent de ce poste")
    octets = raccourci.read_bytes()
    assert b"ALICE.bat" in octets or b"A\x00L\x00I\x00C\x00E\x00.\x00b\x00a\x00t" in octets, (
        "Alice.lnk ne pointe plus vers l'ancien programme"
    )
    assert b"controle_alice" not in octets, "Alice.lnk a ete detourne vers le nouveau programme"


def test_les_scripts_powershell_sont_en_ascii():
    for chemin in RACINE.rglob("*.ps1"):
        if "venv_interface" in chemin.parts:
            continue
        octets = chemin.read_bytes()
        assert all(octet < 128 for octet in octets), f"{chemin.name} n'est pas en ASCII"
