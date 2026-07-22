# Alice — un compagnon vocal 100 % local (français, AMD/Windows)

Une IA de conversation qui **parle, écoute et se souvient**, entièrement hors
ligne, conçue pour tourner **pendant que vous jouez** sur une machine grand
public. Pas un assistant utilitaire : une présence, avec une personnalité qui
tient et une mémoire à long terme.

> **État : V1.** Utilisée quotidiennement par son auteur. Ce dépôt est partagé
> pour ses **choix d'architecture** et surtout pour ses **pièges documentés**
> (voir [PIEGES.md](PIEGES.md)) — la partie AMD/Windows est un désert
> documentaire, et l'essentiel de ce qui suit a été payé en heures perdues.
>
> ⚠️ **Ce n'est pas un produit clé en main.** Les chemins et les modèles sont à
> configurer, l'installation demande de la patience, et le code porte l'histoire
> de son développement dans ses commentaires (en français). Servez-vous-en comme
> d'une **carte**, pas d'un package.

## Ce que ça fait

- **Écoute en continu**, se réveille sur son nom, et sait quand vous avez fini
  de parler grâce à un modèle d'intonation (pas un simple chrono de silence).
- **Répond avec une personnalité stable** : ne redevient pas un assistant poli
  sous la pression, garde une humeur d'un tour à l'autre, lance les sujets.
- **Se souvient d'une session à l'autre** : faits, déroulé, et une mémoire qui
  se range pendant les silences.
- **Ne monopolise pas la machine** : oreille et voix sur le processeur, seul le
  modèle de langage occupe la carte graphique.

## Matériel de référence (celui de l'auteur)

| | |
|---|---|
| GPU | Radeon RX 7900 XTX 24 Go (**pas de CUDA**, llama.cpp en Vulkan) |
| CPU | Intel i5-13600K (l'oreille et la voix y tournent) |
| RAM | 32 Go |
| OS | Windows 11 |

Une carte NVIDIA fonctionnera aussi (et mieux) ; les contournements ROCm/Vulkan
deviendront simplement inutiles.

## Architecture

```
   micro ──> détection de parole (Silero)
              │
              ├─> fin de tour à l'intonation (smart-turn v3.0, CPU, ~30 ms)
              └─> transcription (Parakeet TDT 0.6B v3, sherpa-onnx, CPU)
                        │
                        v
              cerveau (Mistral Small 3.2 24B, llama.cpp Vulkan)
                   + mémoire (Mem0 + Chroma + code maison)
                        │
                        v
              voix (Pocket TTS, CPU, 0 Go de VRAM)
                   + portier : chaque unité est réécoutée avant d'être jouée
                        │
                        v
                  haut-parleurs
```

Chaque organe est un **petit service HTTP local** (ports 8080/8081/8082), parce
que les bibliothèques nécessaires sont mutuellement incompatibles et vivent dans
des environnements Python séparés. Chacun garde son modèle chaud.

Un **interrupteur par organe** permet de changer de moteur en une ligne :
`MOTEUR_OREILLE` (parakeet / whisper), `MOTEUR_VOIX` (pocket / supertonic /
piper).

## Installation (résumé — comptez une soirée)

1. **Python 3.11** (3.12 pour les bulles GPU AMD). Un environnement virtuel
   **par organe** : `ecoute/`, `voix/`, `memoire/`.
2. **llama.cpp** compilé avec Vulkan (ou CUDA), et un GGUF de modèle de langage.
3. Les modèles : Parakeet (sherpa-onnx), smart-turn **v3.0** (voir PIEGES),
   Pocket TTS, un GGUF d'embeddings pour la mémoire.
4. Copier `config.py` et renseigner les chemins + le micro.
5. Copier `prompts/exemple.txt`, écrire la personnalité et le socle d'identité.
6. Lancer l'orchestrateur (`ecoute/boucle_alice.py` en console, ou
   `ecoute/interface_alice.py` pour la fenêtre) : il démarre les trois services.

Les versions exactes et les pièges d'installation sont dans
[PIEGES.md](PIEGES.md).

## Ce qui rend la mémoire utilisable

Un magasin vectoriel seul ne suffit pas. Ce qui a fait la différence :

- un **socle d'identité inconditionnel** injecté à chaque tour (jamais
  recherché : une recherche sémantique ne remonte rien quand l'utilisateur dit
  juste « Hm », et c'est précisément là que le modèle invente) ;
- une **fiche par personne**, pour ne jamais attribuer à un inconnu ce qu'on
  sait d'un autre ;
- un **portier à l'entrée** : les phrases méta et les échos de ce que l'IA vient
  de dire ne deviennent jamais des souvenirs (sans lui, ses propres
  hallucinations se transforment en faits — le pire défaut rencontré, parce
  qu'il s'auto-entretient) ;
- un **tri pendant les silences**, qui cède la place dès qu'une parole arrive ;
- un **fil épisodique** (ce dont on a parlé, dans l'ordre) en plus des faits ;
- une **réconciliation par juge** : un fait nouveau qui en contredit un ancien
  le remplace au lieu de coexister avec lui.

## Faiblesses connues (V1 assumée)

- ~6 % des prises de voix ont un petit défaut (corruption intermittente connue
  du moteur TTS, ticket amont ouvert).
- Tours de parole stricts : pas de full-duplex, l'IA ne peut pas acquiescer ni
  être interrompue pendant qu'elle parle.
- Elle n'existe pas entre les sessions (pas de cognition d'arrière-plan).
- La personnalité vient d'un prompt, pas d'un affinage sur son propre vécu.
- Elle + un jeu exigeant, c'est tendu : le modèle de langage prend 15,3 des
  24 Go de VRAM.
- Les faux souvenirs sont fortement réduits, pas structurellement éliminés.

## Crédits

Ce projet n'est qu'un assemblage. Le mérite revient à :
[llama.cpp](https://github.com/ggml-org/llama.cpp) ·
[Mistral](https://mistral.ai) ·
[NVIDIA Parakeet](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) ·
[sherpa-onnx (k2-fsa)](https://github.com/k2-fsa/sherpa-onnx) ·
[Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts) et le corpus
[CML-TTS](https://huggingface.co/kyutai/tts-voices) ·
[smart-turn (Pipecat)](https://github.com/pipecat-ai/smart-turn) ·
[Supertone Supertonic](https://github.com/supertone-inc/supertonic) ·
[Piper](https://github.com/OHF-Voice/piper1-gpl) ·
[Mem0](https://github.com/mem0ai/mem0) · [Chroma](https://www.trychroma.com) ·
[Silero VAD](https://github.com/snakers4/silero-vad).

Vérifiez la licence de chaque modèle avant tout usage autre que personnel.

Construit par un non-développeur avec l'aide de
[Claude Code](https://claude.com/claude-code) (Anthropic).
