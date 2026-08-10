# -*- coding: utf-8 -*-
"""Controle en direct de la priorite CPU des processus d'Alice.

Regle du 06/08/2026 : le jeu gagne tout arbitrage processeur, Alice attend
son tour. Concretement, tout processus Python appartenant a Alice_V3 doit
tourner en priorite BASSE (BelowNormal, 0x4000) ou moindre.

Ce controle liste tous les python/pythonw de la machine avec leur priorite
et rend un verdict. A lancer PENDANT qu'Alice tourne.
Lecture seule : rien n'est modifie.

EXEMPLAIRE DE REFERENCE depuis le 06/08/2026. L'outil est ne dans
`Desktop\\vision Alice\\outils` ; il vit desormais AVEC Alice (le raccourci
"Priorite d'Alice" du Bureau vise ce fichier-ci, via
`Alice_V3\\VERIFIER_PRIORITE_ALICE.bat` et l'interpreteur `memoire\\venv`).
La copie restee cote vision est historique : ne plus la modifier.

    memoire\\venv\\Scripts\\python.exe -X utf8 outils\\verifier_priorite_alice.py
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess

import win32api
import win32process

CLASSES = {
    0x40: "tres basse (Idle)",
    0x4000: "BASSE (BelowNormal)",
    0x20: "NORMALE",
    0x8000: "au-dessus (AboveNormal)",
    0x80: "haute (High)",
    0x100: "temps reel",
}
ACCEPTABLES = {0x40, 0x4000}


def chemin_processus(handle):
    tampon = ctypes.create_unicode_buffer(512)
    taille = ctypes.c_ulong(512)
    if ctypes.windll.kernel32.QueryFullProcessImageNameW(
            int(handle), 0, tampon, ctypes.byref(taille)):
        return tampon.value
    return ""


def lignes_de_commande(pids):
    """Ligne de commande et pere de chaque PID, via WMI (une seule requete).

    Indispensable pour identifier QUEL script tourne dans un python fautif :
    le chemin de l'interpreteur ne suffit pas, plusieurs services partagent
    le meme venv.
    """
    if not pids:
        return {}
    filtre = " or ".join(f"ProcessId={p}" for p in pids)
    script = (
        "Get-CimInstance -Query \"select ProcessId,ParentProcessId,CommandLine "
        f"from Win32_Process where {filtre}\" | "
        "ForEach-Object { @{pid=$_.ProcessId; pere=$_.ParentProcessId; "
        "cmd=$_.CommandLine} | ConvertTo-Json -Compress }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        table = {}
        for ligne in r.stdout.splitlines():
            ligne = ligne.strip()
            if ligne:
                d = json.loads(ligne)
                table[d["pid"]] = (d.get("pere"), d.get("cmd") or "")
        return table
    except Exception:
        return {}


def principal():
    pythons = []
    for pid in win32process.EnumProcesses():
        try:
            h = win32api.OpenProcess(0x1000, False, pid)
        except win32api.error:
            continue
        try:
            chemin = chemin_processus(h)
            if not chemin.lower().endswith(("python.exe", "pythonw.exe")):
                continue
            classe = win32process.GetPriorityClass(h)
            pythons.append((pid, chemin, classe))
        finally:
            h.close()

    if not pythons:
        print("Aucun processus Python en cours : Alice est fermee.")
        print("Relancer ce controle pendant qu'Alice tourne.")
        return

    # Un processus d'Alice se reconnait a son interpreteur OU a sa ligne de
    # commande : le centre de controle lance certains etages avec le Python
    # systeme, qui echappait au premier critere (vu en direct le 06/08).
    toutes_commandes = lignes_de_commande([p[0] for p in pythons])

    # Le controle ne doit jamais s'accuser lui-meme.
    _moi = os.getpid()
    _miens = {_moi}
    for _ in range(3):
        for pid, (pere, _cmd) in toutes_commandes.items():
            if pere in _miens or pid in _miens:
                _miens.update({pid, pere} - {None})

    def est_alice(pid, chemin):
        if pid in _miens:
            return False
        if "alice_v3" in chemin.lower():
            return True
        _, cmd = toutes_commandes.get(pid, (None, ""))
        return "alice_v3" in cmd.lower()

    # La voix est exemptee depuis le 08/08/2026 : la mesure a montre qu'en
    # priorite basse elle accumulait trop de blancs sous charge.
    def est_la_voix(pid, chemin):
        _, cmd = toutes_commandes.get(pid, (None, ""))
        return "service_voix" in (chemin + " " + cmd).lower()

    alice = [p for p in pythons if est_alice(p[0], p[1])]
    autres = [p for p in pythons if not est_alice(p[0], p[1])]

    if alice:
        print("=== Processus Python d'ALICE ===")
    else:
        print("Aucun processus d'Alice trouve (elle est fermee).")

    details = toutes_commandes
    fautifs = []
    for pid, chemin, classe in alice:
        nom = CLASSES.get(classe, hex(classe))
        voix = est_la_voix(pid, chemin)
        ok = classe in ACCEPTABLES or voix
        if not ok:
            fautifs.append(pid)
        etat = "VOIX " if voix else ("OK   " if ok else "FAUTE")
        print(f"  {etat} PID {pid:6d}  priorite {nom}  {chemin}")
        pere, cmd = details.get(pid, (None, ""))
        if cmd:
            print(f"        pere {pere}  commande : ...{cmd[-90:]}")

    if autres:
        print()
        print("=== Autres Python (information, pas de regle) ===")
        for pid, chemin, classe in autres:
            nom = CLASSES.get(classe, hex(classe))
            print(f"        PID {pid:6d}  priorite {nom}  {chemin}")

    print()
    if alice and not fautifs:
        print("VERDICT : VERT - tous les processus d'Alice sont en priorite basse,")
        print("           la VOIX exceptee : elle est en priorite normale depuis le")
        print("           08/08/2026, a la mesure (sinon 25 % de blancs dans sa")
        print("           parole quand le processeur est charge, contre 9 %).")
    elif fautifs:
        print(f"VERDICT : ROUGE - {len(fautifs)} processus d'Alice en priorite trop haute.")
        print("Le jeu ne gagne pas l'arbitrage CPU face a eux. A corriger dans le")
        print("lanceur concerne (drapeau BELOW_NORMAL_PRIORITY_CLASS manquant).")


if __name__ == "__main__":
    principal()
