# join_template_history.py

Un dépôt distribué par Classroom50 n'a aucun historique git en commun avec le
dépôt template dont il provient. Si l'enseignant·e met à jour le template plus
tard, un `git merge` échoue, et `git merge --allow-unrelated-histories`
fonctionne mais marque en conflit tous les fichiers qui ont changé, même ceux
jamais touchés.

Ce script relie une fois pour toutes votre dépôt au template, pour que les
mises à jour futures se fusionnent proprement.

## Utilisation

```
TEMPLATE_URL=<url du dépôt template> uv run <url de ce script>
```

- `TEMPLATE_URL` (obligatoire) : URL du dépôt template.
- `TEMPLATE_BRANCH` (optionnel, défaut `main`) : branche du template à suivre.
- `--quiet` / `-q` (ou `QUIET=1`) : n'affiche que les erreurs et le résultat.

Nécessite [uv](https://docs.astral.sh/uv/getting-started/installation/) et `git`.

## Ce qu'il fait

Le script cherche dans l'historique du template le commit dont le contenu
correspond exactement à votre commit racine. S'il le trouve, il relie les deux
historiques sans toucher à vos commits ni à votre répertoire de travail : ça ne
fonctionne que parce que le contenu avant et après la jonction est identique,
donc rien à fusionner réellement, juste un lien ajouté dans l'historique.

S'il ne trouve aucune correspondance, il s'arrête sans rien modifier.

Idempotent : le relancer après un premier succès n'a aucun effet.

## Sur quelle branche le lancer

Il relie uniquement la branche courante, jamais `main` à votre place. Vous
pouvez le lancer sur plusieurs branches séparément (chacune se retrouve reliée
au même commit du template), puis les fusionner normalement dans `main` :
aucun conflit particulier n'en résulte.

Le lien ne survit que si la fusion vers `main` est une vraie fusion. Un squash
merge ou un rebase le perdent (ils ne gardent pas l'ancêtre ajouté par le
script). Dans ce cas, relancez simplement le script sur `main` : idempotent,
ça répare la situation immédiatement.

## Après exécution

Poussez le résultat :

```
git push origin <votre-branche>
```
