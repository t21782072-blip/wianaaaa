# 🤖 Bot Winamax Value Bet v3
### Tous les sports · Tous les marchés · Pinnacle + Betfair

---

## Ce que le bot surveille

### Sports (tous)
Football, Tennis, Basketball, Rugby, Handball, Hockey sur glace,
Cyclisme, Biathlon, Ski alpin, Formule 1, MMA/UFC, Boxe, Golf,
Volleyball, Baseball, Fléchettes, Snooker, Esports, et plus...

### Marchés (tous)
| Marché | Exemple |
|--------|---------|
| 1N2 / Winner | PSG gagne |
| Buteur / Scorer | Mbappé marque |
| Handicap | PSG -1.5 buts |
| Over/Under | Plus de 2.5 buts |
| Vainqueur tournoi | Pogacar gagne le Tour |
| Podium | Verstappen top 3 |
| Sets / Jeux | Djokovic gagne en 3 sets |
| et tous les autres... | |

### Référence de comparaison
1. **Pinnacle** — bookmaker sharp, marge ~2%
2. **Betfair Exchange** — marché P2P, marge ~0%
3. **No-vig interne** — fallback si pas de référence externe

---

## Exemple d'alerte Telegram

```
🚨 VALUE BET — Cyclisme

🚴 Tour de France - Étape 12
📅 2025-07-10 14:00

🔥🔥🔥 Vainqueur d'étape — Tadej Pogacar
   Winamax  : 4.50
   Référence: 1.80 (Pinnacle)
   Edge     : +150%

🔥 Buteur — Kylian Mbappé (à tout moment)
   Winamax  : 3.20
   Référence: 2.10 (Betfair Exchange)
   Edge     : +52%
```

---

## Installation (10 min, gratuit)

### 1. Clé The Odds API
→ **the-odds-api.com** → Get API Key (500 requêtes/mois gratuites)

### 2. Bot Telegram
→ **@BotFather** → `/newbot` → copie le TOKEN
→ **@userinfobot** → copie ton CHAT ID

### 3. GitHub
→ Nouveau repo → uploade les 4 fichiers

### 4. Railway
→ **railway.app** → New Project → ton repo GitHub

Variables à ajouter :
| Variable | Valeur |
|----------|--------|
| TELEGRAM_TOKEN | token BotFather |
| TELEGRAM_CHAT_ID | ton chat ID |
| ODDS_API_KEY | clé the-odds-api.com |
| EDGE_THRESHOLD | 15 |
| CHECK_INTERVAL | 300 |
| MIN_ODD | 1.05 |
| MAX_ODD | 50.0 |

→ **Deploy** → le bot tourne 24h/24 🎉

---

## Niveaux d'alerte
| 🔥 | Edge 15–25% | Value solide |
| 🔥🔥 | Edge 25–50% | Très bonne value |
| 🔥🔥🔥 | Edge +50% | Erreur massive |

---

⚠️ *Jouez responsablement. Ce bot est un outil d'analyse, pas un conseil financier.*
