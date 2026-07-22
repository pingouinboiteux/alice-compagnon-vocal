# -*- coding: utf-8 -*-
"""LES RÉGLAGES QUI DÉPENDENT DE TA MACHINE — le seul fichier à éditer.

Tout le reste du projet déduit la racine du projet tout seul (depuis
l'emplacement de chaque fichier) : rien d'autre n'est à changer.
"""
import os

# Le micro à utiliser : un morceau de son nom, ou None pour le micro par défaut.
# (ex. "Yeti", "USB", "Casque")
MICRO_PREFERE = None

# ─── LES CHEMINS DES MODÈLES ────────────────────────────────────────────────
# Le cerveau : un GGUF quantifié (Mistral Small 3.2 24B Q4_K_M chez l'auteur).
MODELE_CERVEAU = os.path.expanduser(
    r"~\modeles\Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf")

# Les embeddings de la mémoire : un petit GGUF d'embeddings (nomic-embed-text).
MODELE_EMBEDDINGS = os.path.expanduser(
    r"~\modeles\nomic-embed-text-v1.5.Q4_K_M.gguf")

# llama.cpp compilé (llama-server.exe) — voir README.
SERVEUR_LLAMA = os.path.expanduser(r"~\llama.cpp\llama-server.exe")

# Facultatif : LM Studio, uniquement si tu utilises le moteur de repli.
CHEMIN_LMS = os.path.expanduser(
    r"~\AppData\Local\Programs\LM Studio\resources\app\.webpack\lms.exe")
