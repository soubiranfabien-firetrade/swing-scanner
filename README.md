# Scanner Swing Pullback V5 : alertes sans limite TradingView

Port Python de votre script Pine. Il analyse autant de symboles que vous voulez sur **2h, journalier et hebdomadaire**, puis envoie les alertes par **Telegram**, **e-mail** et sur un **tableau de bord HTML**. Tout tourne gratuitement sur **GitHub Actions**.

## Ce qui est surveillé (mêmes événements que l'alerte unique Pine)
Achat swing, cassure HH, changement de couleur de l'EMA 180, du tableau Mensuel/Hebdo/Journalier, des rubans T/S/A/A-1 et du momentum, clôture qui casse une ligne pivot (graphique, journalier, hebdo). Chaque événement se désactive dans `config.yaml`.

## Installation (une seule fois, environ 15 minutes)

### 1. Créer le dépôt GitHub
1. Créez un dépôt (ex. `swing-scanner`) et envoyez-y tout le contenu de ce dossier, y compris `.github/`.
2. Un dépôt **public** n'a pas de limite de minutes Actions, mais il rend `state.json` et le tableau de bord visibles. Un dépôt **privé** est limité en minutes gratuites par mois : le scan 2h toutes les heures peut s'en approcher. Dans ce cas, réduisez la plage horaire dans `.github/workflows/scan.yml`.

### 2. Bot Telegram
1. Dans Telegram, parlez à **@BotFather**, tapez `/newbot` et suivez les étapes : vous recevez un **token**.
2. Écrivez un message à votre nouveau bot, puis ouvrez `https://api.telegram.org/bot<TOKEN>/getUpdates` : cherchez `"chat":{"id":...}`, c'est votre **chat id**.

### 3. E-mail (Gmail)
Activez la validation en 2 étapes sur votre compte Google, puis créez un **mot de passe d'application** (Compte Google > Sécurité). Utilisez-le comme mot de passe SMTP, pas votre mot de passe habituel.

### 4. Secrets GitHub
Dépôt > Settings > Secrets and variables > Actions > New repository secret :

| Secret | Valeur |
|---|---|
| `TELEGRAM_TOKEN` | token du bot |
| `TELEGRAM_CHAT_ID` | votre chat id |
| `SMTP_USER` | votre adresse Gmail |
| `SMTP_PASSWORD` | mot de passe d'application |
| `EMAIL_TO` | adresse de réception |
| `SMTP_HOST` / `SMTP_PORT` | facultatifs (par défaut `smtp.gmail.com` / `465`) |

Un canal dont les secrets sont absents est simplement ignoré.

### 5. Tableau de bord
Settings > Pages > Source : *Deploy from a branch*, branche `main`, dossier `/docs`. L'adresse s'affiche ensuite (`https://<utilisateur>.github.io/<dépôt>/`).

### 6. Premier lancement
Onglet **Actions** > *Scan Swing Pullback* > **Run workflow**. Vérifiez Telegram, l'e-mail et la page.

## Ajouter vos 100 actions
Éditez `config.yaml`, section `tickers`, avec les symboles **Yahoo Finance** : `AAPL`, `MC.PA` (Paris), `SAP.DE` (Francfort), `SHEL.L` (Londres), `BTC-USD`, `^FCHI`...

## Test en local
```
pip install -r requirements.txt
python scanner.py --demo        # données synthétiques, aucun envoi
python scanner.py --no-send     # vraies données, sans envoi
```

## Horaires
- **Journalier + hebdo** : une fois par jour à 05:30 UTC (les bougies de la veille sont clôturées). Un signal hebdo ou mensuel est donc détecté à la première exécution **après la fin de la période** (lundi pour la semaine).
- **2h** : toutes les heures en semaine, de 06:40 à 22:40 UTC. Un signal n'est envoyé qu'une fois (mémoire dans `state.json`).
- GitHub peut retarder les tâches planifiées de plusieurs minutes. Si le dépôt reste inactif 60 jours, GitHub peut suspendre les tâches planifiées : un lancement manuel les réactive.

## Limites à connaître
- **Résultats proches mais pas identiques à TradingView** : source de données différente (Yahoo Finance, non officiel), sessions et ajustements de prix, amorçage des moyennes. Comparez sur quelques symboles avant de vous y fier.
- **Bougies 2h** reconstruites à partir du 1h, ancrées sur l'ouverture de séance. Yahoo limite l'historique 1h (environ 2 ans). La dernière bougie 2h d'une séance incomplète (1 seule heure) est ignorée jusqu'à la séance suivante.
- **Non repris** : les filtres optionnels de l'achat (alignement, distance, volume, résistance, désactivés par défaut dans Pine).
- Pour les niveaux pivots des unités supérieures, le niveau est lu sur les bougies supérieures **déjà clôturées**.
- Ce n'est pas un conseil en investissement.

## Licence
Les niveaux pivots s'inspirent de *Pivot Levels [BigBeluga]* (CC BY-NC-SA 4.0) : usage non commercial, partage dans les mêmes conditions, crédit à BigBeluga.
