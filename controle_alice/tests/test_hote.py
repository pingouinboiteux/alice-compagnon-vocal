"""La chaine de survie : si l'interface meurt, plus rien ne reste.

Ces tests utilisent le vrai hote, le vrai enclos Windows et le vrai tuyau. Seuls
les services sont faux, pour que la campagne tienne en secondes et ne touche
jamais la memoire reelle.
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from commun import protocole

from hote import sante

DOSSIER = Path(__file__).parent
RACINE = DOSSIER.parent


def deux_ports() -> tuple[int, int]:
    prises = [socket.socket(socket.AF_INET, socket.SOCK_STREAM) for _ in range(2)]
    for prise in prises:
        prise.bind(("127.0.0.1", 0))
    ports = tuple(prise.getsockname()[1] for prise in prises)
    for prise in prises:
        prise.close()
    return ports


def attendre(condition, patience=25.0, pas=0.1) -> bool:
    limite = time.monotonic() + patience
    while time.monotonic() < limite:
        if condition():
            return True
        time.sleep(pas)
    return False


def ports_pris(ports) -> bool:
    return all(sante.port_occupe(port) for port in ports)


def ports_libres(ports) -> bool:
    return not any(sante.port_occupe(port) for port in ports)


@pytest.fixture
def ports():
    paire = deux_ports()
    yield paire
    assert attendre(lambda: ports_libres(paire), patience=30.0), (
        "des services ont survecu a la fin du test"
    )


def lancer_hote(ports, *options: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            str(DOSSIER / "hote_de_test.py"),
            *map(str, ports),
            *options,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(RACINE),
    )


def lire_jusqu_a(proc, verbe, patience=40.0) -> dict:
    limite = time.monotonic() + patience
    while time.monotonic() < limite:
        ligne = proc.stdout.readline()
        if not ligne:
            break
        message = json.loads(ligne)
        if message.get("verbe") == verbe:
            return message
    raise AssertionError(f"l'hote n'a jamais dit {verbe!r}")


# ----------------------------------------------------------------------


def test_l_hote_se_presente_et_demarre(ports):
    proc = lancer_hote(ports)
    try:
        bonjour = lire_jusqu_a(proc, "bonjour")
        assert bonjour["version"] == protocole.VERSION

        proc.stdin.write(b'{"verbe": "demarrer"}\n')
        proc.stdin.flush()
        fini = lire_jusqu_a(proc, "fini")
        assert fini["ok"], fini.get("raison")
        assert ports_pris(ports)
    finally:
        proc.kill()
        proc.wait(timeout=15)


def test_la_fin_du_tuyau_arrete_tout(ports):
    """La fermeture normale : l'interface se ferme, l'hote sort, tout tombe."""
    proc = lancer_hote(ports)
    try:
        lire_jusqu_a(proc, "bonjour")
        proc.stdin.write(b'{"verbe": "demarrer"}\n')
        proc.stdin.flush()
        assert lire_jusqu_a(proc, "fini")["ok"]
        assert ports_pris(ports)

        proc.stdin.close()  # exactement ce que fait une fenetre qui se ferme
        assert proc.wait(timeout=30) == 0
        assert attendre(lambda: ports_libres(ports)), "des services ont survecu a l'hote"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_un_hote_tue_emporte_ses_services(ports):
    """Le filet dur : meme abattu, l'hote ne laisse rien.

    C'est l'enclos Windows qui tient la promesse, pas la politesse du code :
    tuer un processus ne lui laisse aucune chance de faire le menage.
    """
    proc = lancer_hote(ports)
    try:
        lire_jusqu_a(proc, "bonjour")
        proc.stdin.write(b'{"verbe": "demarrer"}\n')
        proc.stdin.flush()
        assert lire_jusqu_a(proc, "fini")["ok"]
        assert ports_pris(ports)

        proc.kill()
        proc.wait(timeout=15)
        assert attendre(lambda: ports_libres(ports)), (
            "les services ont survecu a la mort de l'hote"
        )
    finally:
        if proc.poll() is None:
            proc.kill()


def test_une_interface_tuee_brutalement_emporte_tout(ports, tmp_path):
    """Le cas que l'utilisateur vivra : la fenetre disparait sans prevenir.

    On tue le processus qui tient le tuyau, sans toucher a ses enfants
    (`taskkill` sans `/T`). L'hote doit s'en apercevoir tout seul, sortir, et
    emporter les services avec lui.
    """
    journal = tmp_path / "hote.log"
    interface = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            str(DOSSIER / "fausse_interface.py"),
            str(ports[0]),
            str(ports[1]),
            str(journal),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(RACINE),
    )
    try:
        pid_hote = int(interface.stdout.readline().strip())
        assert attendre(lambda: ports_pris(ports), patience=40.0), (
            "les faux services ne sont jamais montes"
        )

        # Sans /T : on ne tue que la fenetre, jamais sa descendance. C'est tout
        # l'interet du test — la chaine doit se defaire d'elle-meme.
        subprocess.run(
            ["taskkill", "/F", "/PID", str(interface.pid)],
            capture_output=True,
            check=True,
        )

        assert attendre(lambda: not _vivant(pid_hote), patience=30.0), (
            "l'hote a survecu a la mort de l'interface"
        )
        assert attendre(lambda: ports_libres(ports), patience=30.0), (
            "les services ont survecu a la mort de l'interface"
        )
    finally:
        if interface.poll() is None:
            interface.kill()


def test_l_hote_deploye_s_ouvre_et_sort_proprement():
    """Le point d'entree **deploye** doit vivre, pas seulement les sources.

    Reproduction du defaut bloquant du 01/08/2026 : la copie deployee sortait
    avec `ModuleNotFoundError: No module named 'menage'` et le code 1, tandis
    que la source sortait avec le code 0. La fenetre compilee affichait alors
    « hote de services absent ». Le programme etait inutilisable, et ma
    verification passait a cote.
    """
    dist = RACINE / "deploiement" / "alice_control_center.dist"
    entree = dist / "hote_alice.py"
    if not entree.exists():
        pytest.skip("dossier deploye absent ; lancer construire.ps1")

    # Un dossier deploye perime ne prouverait rien sur le code d'aujourd'hui, et
    # empecherait meme de reconstruire, puisque `construire.ps1` refuse de
    # compiler quand un test echoue. On s'abstient donc, et la construction
    # rejoue la suite apres avoir copie : c'est la que ce test doit mordre.
    # Un fichier absent compte comme perime, et non comme une erreur : sinon la
    # campagne echouerait, `construire.ps1` refuserait de compiler, et le
    # deploiement resterait casse sans moyen de le reparer. La presence, elle,
    # est exigee **apres** la copie, par le script de construction.
    perimes = [
        relatif
        for relatif in (
            "hote_alice.py", "micro_adaptateur.py", "service_discord_alice.py",
            "hote/principal.py",
            "hote/superviseur.py", "hote/sante.py", "hote/conversation.py",
            "hote/enseignement_jeu.py", "hote/micro.py",
            "commun/protocole.py", "commun/micro_protocole.py",
            "pont_discord/coffre.py",
            "pont_discord/cerveau.py", "pont_discord/service.py",
            "service_twitch_alice.py", "hote/twitch_pont.py",
            "pont_twitch/coffre.py", "pont_twitch/cerveau.py",
            "pont_twitch/service.py", "pont_twitch/portier.py",
            "pont_twitch/selection.py", "pont_twitch/entrants.py",
            "pont_twitch/reglages.py", "pont_twitch/source.py",
            "pont_twitch/source_eventsub.py",
        )
        if not (dist / relatif).exists()
        or (dist / relatif).read_bytes() != (RACINE / relatif).read_bytes()
    ]
    if perimes:
        pytest.skip(f"dossier deploye perime ({len(perimes)} fichiers) ; reconstruire")

    python_v3 = RACINE.parent / "memoire" / "venv" / "Scripts" / "python.exe"
    assert python_v3.exists(), "interpreteur V3 introuvable"

    proc = subprocess.run(
        [str(python_v3), "-X", "utf8", str(entree)],
        input=b"",  # entree fermee tout de suite : l'hote doit sortir seul
        capture_output=True,
        timeout=120,
        cwd=str(dist),
    )
    sortie = proc.stdout.decode("utf-8", errors="replace")
    erreurs = proc.stderr.decode("utf-8", errors="replace")

    assert "bonjour" in sortie, f"l'hote deploye n'a pas salue. stderr : {erreurs[:400]}"
    assert proc.returncode == 0, f"code {proc.returncode}. stderr : {erreurs[:400]}"
    assert "ModuleNotFoundError" not in erreurs


def test_le_verificateur_envoie_son_premier_ordre_sans_bom():
    """Le StreamWriter .NET ajoute sinon un BOM que le JSON strict refuse."""
    script = (RACINE / "verifier_executable.ps1").read_text(encoding="ascii")
    assert "StandardInput" not in script
    aide = (RACINE / "outils" / "verifier_enclos_deploye.py").read_text(
        encoding="utf-8"
    )
    assert "proc.stdin.write(b'" in aide
    assert "sys._base_executable" in aide


def test_l_arret_d_urgence_coupe_un_demarrage_bloque(ports):
    """La preuve que F12 attendait : un demarrage qui n'avance pas, coupe net.

    Le premier service repond, le second dort trente secondes : `demarrer()`
    est bloque a attendre une sonde. C'est exactement le moment ou une demande
    polie resterait en file. On tue donc l'hote, comme le fait F12, et tout
    doit disparaitre en moins d'une seconde.
    """
    proc = lancer_hote(ports, "--lent=30")
    try:
        lire_jusqu_a(proc, "bonjour")
        proc.stdin.write(b'{"verbe": "demarrer"}\n')
        proc.stdin.flush()

        assert attendre(lambda: sante.port_occupe(ports[0]), patience=40.0), (
            "le premier faux service n'est jamais monte"
        )
        assert not sante.port_occupe(ports[1]), "le second ne devait pas etre pret"

        debut = time.monotonic()
        proc.kill()  # ce que fait exactement l'arret d'urgence
        proc.wait(timeout=5)
        assert attendre(lambda: ports_libres(ports), patience=5.0, pas=0.02), (
            "des services ont survecu a l'arret d'urgence"
        )
        ecoule = time.monotonic() - debut
        assert ecoule < 1.0, f"l'arret d'urgence a pris {ecoule:.2f} s, au-dela d'une seconde"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_le_second_filet_existe_et_se_declenche():
    """Reproduction exacte d'un defaut vecu le 31/07/2026.

    `sortie_forcee` avait ete ecrite hors de sa classe : la surveillance du
    parent levait `AttributeError`, l'hote mourait au demarrage, et la fenetre
    affichait « hote de services absent ». Les autres tests n'avaient rien vu,
    parce qu'ils passaient tous par la fin de tuyau, jamais par ce filet.

    On verifie donc les deux moities separement : la methode existe, et la
    surveillance rappelle bien quand le parent disparait.
    """
    from hote.principal import Hote, surveiller_le_parent

    assert callable(getattr(Hote, "sortie_forcee", None)), (
        "le second filet n'existe pas ; la surveillance du parent planterait"
    )

    dormeur = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rappels = []
    fil = surveiller_le_parent(dormeur.pid, lambda: rappels.append("parti"))
    assert fil is not None, "la surveillance n'a pas pu ouvrir le processus parent"
    assert rappels == [], "la surveillance a cru le parent mort trop tot"

    dormeur.kill()
    dormeur.wait(timeout=15)
    assert attendre(lambda: rappels == ["parti"], patience=15.0), (
        "la disparition du parent n'a rien declenche"
    )


def _vivant(pid: int) -> bool:
    sortie = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True
    )
    return str(pid) in (sortie.stdout or "")


def test_une_demande_hors_protocole_est_refusee_sans_tuer_l_hote(ports):
    """Le canal est ferme : une ligne folle ne fait pas tomber le tableau de bord."""
    proc = lancer_hote(ports)
    try:
        lire_jusqu_a(proc, "bonjour")
        proc.stdin.write(b'{"verbe": "tout_effacer"}\n')
        proc.stdin.write(b"ceci n'est pas du json\n")
        proc.stdin.flush()

        refus = lire_jusqu_a(proc, "journal")
        assert refus["niveau"] == "attention"
        assert "refusee" in refus["texte"]

        proc.stdin.write(b'{"verbe": "etat"}\n')
        proc.stdin.flush()
        etat = lire_jusqu_a(proc, "etat")
        assert len(etat["services"]) >= 2  # l'hote vit toujours et repond
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
