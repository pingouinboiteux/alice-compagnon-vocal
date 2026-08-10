# Les pieges payes

Tout ce qui suit a ete retenu parce que ca a vraiment coute du temps, ou parce
que ca a vraiment debloque le projet.

Rien ici n'est la pour faire "complet". Si un piege est banal, deja mille fois
documente, ou trop flou pour aider concretement, il ne sort pas.

## 1. Un `.gitignore` ne demenage pas tout seul

Des motifs ancres a la racine ne protegent plus rien apres un deplacement du
code dans un sous-dossier. Toujours verifier avec :

`git check-ignore -v <chemin>`

## 2. Windows ne tue pas les arbres de processus

Fermer le parent ne tue pas toujours les enfants. Pour un projet a services
locaux, ca cree vite des zombies qui mangent RAM, VRAM ou ports TCP.

Le garde-fou solide ici a ete le Job Object Windows.

## 3. `winsound` ne suffit pas pour une vraie voix en flux

`winsound` sait jouer un son entier. Il ne sait pas streamer correctement des
blocs qui arrivent au fil de l'eau. Pour une voix locale qui se fabrique en
meme temps qu'elle parle, il a fallu passer a `waveOut`.

## 4. Le reveil a froid d'un moteur vocal peut ruiner toute l'experience

La voix pouvait sembler "cassee" alors que le vrai probleme etait surtout son
retour a froid apres un silence. Un petit pouls interne a garde le moteur chaud
et a fait tomber le delai ressenti.

## 5. GPU-Z ment au repos sur le PCIe

Pour savoir si une carte est vraiment en x1, x2, x4 ou x16, il faut regarder
sous charge. Sinon on peut accuser le mauvais coupable pendant des heures.

## 6. Sur AMD/Windows, la priorite GPU miracle n'existe pas

Quand le jeu et l'IA se battent pour la meme carte, il n'y a pas de bouton
magique. Les leviers utiles ont ete :

- reduire la demande ;
- choisir quelle appli Windows prefere quelle carte ;
- baisser la priorite des bons processus ;
- mesurer avant de croire.

## 7. Un garde-fou deterministe bat une interdiction dans un prompt

Quand un comportement doit etre interdit pour de bon, le prompt ne suffit pas.
Pour les fuites, les didascalies, les repetitions, les frontieres d'interface
ou les mots sensibles, le code a tenu la ou la consigne seule cede tot ou tard.

## 8. Les instruments de mesure mentent aussi

Le projet a perdu du temps sur :

- une mauvaise lecture VRAM ;
- des mesures prises au mauvais endroit ;
- des tests qui tenaient eux-memes la ressource mesuree ;
- des chiffres vrais mais mal interpretes.

Regle de survie :

- verifier l'instrument ;
- verifier l'unite ;
- verifier le contexte ;
- seulement apres accuser le systeme.

## 9. PowerShell 5.1 reste fragile sur l'encodage

Un `.ps1` non ASCII peut casser silencieusement sur certains postes Windows.
Quand un script doit juste marcher, l'ASCII strict reste une discipline utile.

## 10. La valeur d'un projet comme celui-ci n'est pas seulement dans les "bons" choix

La vraie valeur vient souvent de :

- ce qui a ete ecarte ;
- pourquoi ca a ete ecarte ;
- avec quelle mesure ;
- et quel repli a finalement tenu.

Un depot public utile n'est pas juste un depot "propre". C'est un depot qui
explique aussi ses impasses.
