"""
Bot Winamax Value Bet v3
========================
- Tous les sports disponibles sur Winamax (foot, tennis, basket, vélo, biathlon, rugby, etc.)
- Tous les marchés : 1N2, buteur, handicap, over/under, sets, podium, etc.
- Comparaison vs Pinnacle + Betfair Exchange (The Odds API)
- Fallback no-vig si pas de référence externe
- Alerte Telegram avec niveau d'urgence
"""

import requests
import json
import time
import os
import logging
from difflib import SequenceMatcher
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
EDGE_THRESHOLD   = float(os.getenv("EDGE_THRESHOLD", "15"))
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "300"))
MIN_ODD          = float(os.getenv("MIN_ODD", "1.05"))   # ignore cotes quasi certaines
MAX_ODD          = float(os.getenv("MAX_ODD", "50.0"))   # ignore cotes trop longues

# Tous les sports Winamax (ID sport → label)
WINAMAX_SPORTS = {
    1:  "Football",
    2:  "Tennis",
    3:  "Basketball",
    4:  "Rugby",
    5:  "Handball",
    6:  "Hockey sur glace",
    7:  "Baseball",
    8:  "Américain",
    9:  "Volley-ball",
    10: "Golf",
    11: "Cyclisme",
    12: "Formule 1",
    13: "MMA / UFC",
    14: "Boxe",
    15: "Snooker",
    16: "Fléchettes",
    17: "Cricket",
    18: "Natation",
    19: "Athlétisme",
    20: "Biathlon",
    21: "Ski alpin",
    22: "Ski de fond",
    23: "Saut à ski",
    24: "Combiné nordique",
    25: "Curling",
    26: "Patinage",
    27: "Esports",
    28: "Politique",
    29: "Divertissement",
}

# Tous les marchés The Odds API disponibles
ODDS_API_MARKETS = [
    "h2h",             # 1N2 / winner
    "spreads",         # handicap
    "totals",          # over/under
    "outrights",       # vainqueur tournoi / podium
    "h2h_lay",         # lay Betfair
    "player_props",    # buteur, assists, etc.
]

# Sports The Odds API disponibles (pour comparaison externe)
ODDS_API_SPORTS = [
    "soccer_france_ligue_one", "soccer_epl", "soccer_spain_la_liga",
    "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_uefa_champs_league",
    "soccer_france_ligue_two", "soccer_belgium_first_div", "soccer_portugal_primeira_liga",
    "tennis_atp_french_open", "tennis_wta_french_open", "tennis_atp_wimbledon",
    "tennis_atp_us_open", "tennis_atp_aus_open",
    "basketball_nba", "basketball_euroleague",
    "americanfootball_nfl", "americanfootball_ncaa",
    "icehockey_nhl", "icehockey_sweden_hockey_league",
    "rugbyleague_nrl", "rugbyunion_premiership", "rugbyunion_super_rugby",
    "golf_masters_tournament_winner", "golf_pga_championship_winner",
    "mma_mixed_martial_arts",
    "boxing_boxing",
    "cricket_test_match",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.winamax.fr/paris-sportifs",
    "X-Requested-With": "XMLHttpRequest",
}

already_alerted = set()
credits_used    = 0


# ── Telegram ─────────────────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TELEGRAM OFF] {message[:150]}")
        return
    # Telegram limite à 4096 caractères
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=10
            )
            if r.status_code != 200:
                logger.error(f"Telegram {r.status_code}: {r.text[:100]}")
        except Exception as e:
            logger.error(f"Telegram exception: {e}")
        time.sleep(0.3)


# ── Winamax — récupération de TOUS les marchés ───────────────────
def get_all_winamax_events() -> list:
    """
    Récupère tous les événements Winamax avec TOUS les marchés disponibles.
    Essaie plusieurs endpoints dans l'ordre.
    """
    events = []

    # Méthode 1 : State Redux embarqué dans le HTML
    for sport_id in WINAMAX_SPORTS:
        sport_events = _fetch_winamax_sport(sport_id)
        events.extend(sport_events)
        if sport_events:
            logger.info(f"  Winamax sport {WINAMAX_SPORTS[sport_id]}: {len(sport_events)} événements")

    # Méthode 2 : Endpoint JSON direct si méthode 1 vide
    if not events:
        logger.warning("Méthode HTML vide — essai endpoint JSON direct")
        events = _fetch_winamax_api_direct()

    logger.info(f"Total Winamax : {len(events)} événements, {sum(len(e.get('markets',[])) for e in events)} marchés")
    return events


def _fetch_winamax_sport(sport_id: int) -> list:
    try:
        url = f"https://www.winamax.fr/paris-sportifs/sports/{sport_id}"
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        return _parse_html_state(r.text, sport_id)
    except Exception as e:
        logger.debug(f"Sport {sport_id} erreur: {e}")
        return []


def _parse_html_state(html: str, sport_id: int) -> list:
    marker = "PRELOADED_STATE__"
    idx = html.find(marker)
    if idx == -1:
        return []
    start = html.find('{', idx)
    if start == -1:
        return []

    depth, end = 0, start
    for i, c in enumerate(html[start:], start):
        if c == '{':   depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    try:
        state = json.loads(html[start:end])
        return _extract_events_from_state(state, sport_id)
    except json.JSONDecodeError:
        return []


def _extract_events_from_state(state: dict, sport_id: int) -> list:
    """
    Extrait récursivement tous les événements et leurs marchés depuis l'état Redux Winamax.
    """
    events = []
    sport_label = WINAMAX_SPORTS.get(sport_id, f"Sport {sport_id}")

    # Cherche les matchs / events à tous les niveaux du state
    def walk(obj, depth=0):
        if depth > 5 or not isinstance(obj, dict):
            return
        for key, val in obj.items():
            if key in ('matches', 'events', 'competitions') and isinstance(val, dict):
                for eid, event in val.items():
                    parsed = _parse_event(eid, event, sport_label)
                    if parsed:
                        events.append(parsed)
            elif isinstance(val, dict):
                walk(val, depth+1)

    walk(state)
    return events


def _parse_event(eid, event: dict, sport_label: str) -> dict | None:
    try:
        title = event.get('title', event.get('name', event.get('label', '')))
        if not title:
            return None

        # Participants
        home, away = _split_title(title)

        # Date
        date = event.get('matchStart', event.get('startTime', event.get('date', '')))

        # Collecte TOUS les marchés disponibles
        markets = []

        # Marchés principaux (betTypes, bets, markets, betGroups...)
        for mkey in ('betTypes', 'bets', 'markets', 'betGroups', 'mainBets'):
            raw_markets = event.get(mkey, {})
            if isinstance(raw_markets, list):
                for m in raw_markets:
                    parsed_m = _parse_market(m)
                    if parsed_m:
                        markets.append(parsed_m)
            elif isinstance(raw_markets, dict):
                for mid, m in raw_markets.items():
                    parsed_m = _parse_market(m)
                    if parsed_m:
                        markets.append(parsed_m)

        # Marché principal (mainBetType)
        main = event.get('mainBetType', event.get('mainBet', {}))
        if main:
            parsed_m = _parse_market(main)
            if parsed_m:
                markets.insert(0, parsed_m)

        if not markets:
            return None

        return {
            'id': str(eid),
            'sport': sport_label,
            'home': home,
            'away': away,
            'date': str(date)[:16].replace('T', ' ') if date else 'N/A',
            'markets': markets,
        }
    except:
        return None


def _parse_market(market: dict) -> dict | None:
    try:
        label = (market.get('label', '') or
                 market.get('name', '') or
                 market.get('title', '') or
                 market.get('betTypeName', ''))

        outcomes_raw = (market.get('outcomes', []) or
                        market.get('bets', []) or
                        market.get('selections', []))

        outcomes = []
        for o in outcomes_raw:
            name  = o.get('label', o.get('name', o.get('title', '')))
            price = float(o.get('odds', o.get('price', o.get('odd', 0))))
            if name and MIN_ODD <= price <= MAX_ODD:
                outcomes.append({'name': name, 'odd': price})

        if len(outcomes) < 2:
            return None

        return {'label': label or 'Marché principal', 'outcomes': outcomes}
    except:
        return None


def _split_title(title: str) -> tuple:
    for sep in (' - ', ' vs ', ' / ', ' – '):
        if sep in title:
            parts = title.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return title.strip(), ''


def _fetch_winamax_api_direct() -> list:
    """Fallback : endpoints JSON non documentés."""
    events = []
    urls = [
        "https://www.winamax.fr/api/v1/sports/matches?limit=500",
        "https://www.winamax.fr/api/v1/events?limit=500",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get('items', data.get('events', []))
                for item in items:
                    parsed = _parse_event(item.get('id', ''), item, 'Inconnu')
                    if parsed:
                        events.append(parsed)
                if events:
                    break
        except:
            continue
    return events


# ── The Odds API — Pinnacle + Betfair, tous marchés ─────────────
def build_reference_db() -> dict:
    """
    Construit une DB de référence {sport_key: {team_key: {market: {outcome: best_odd}}}}
    en interrogeant Pinnacle + Betfair sur tous les sports et marchés disponibles.
    """
    global credits_used
    if not ODDS_API_KEY:
        return {}

    db = {}
    for sport_key in ODDS_API_SPORTS:
        for market in ['h2h', 'spreads', 'totals', 'outrights']:
            try:
                r = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                    params={
                        "apiKey":      ODDS_API_KEY,
                        "regions":     "eu,uk",
                        "markets":     market,
                        "bookmakers":  "pinnacle,betfair_ex_eu,betfair_ex_uk,unibet,williamhill",
                        "oddsFormat":  "decimal",
                    },
                    timeout=15
                )
                remaining = int(r.headers.get('x-requests-remaining', 999))
                used      = int(r.headers.get('x-requests-used', 0))
                credits_used = used

                if r.status_code == 401:
                    logger.error("Odds API: clé invalide")
                    return db
                if r.status_code == 429:
                    logger.warning("Odds API: rate limit — pause 60s")
                    time.sleep(60)
                    continue
                if r.status_code != 200:
                    continue

                games = r.json()
                for game in games:
                    gkey = f"{game['home_team']}|{game['away_team']}"
                    if gkey not in db:
                        db[gkey] = defaultdict(lambda: defaultdict(dict))
                    for bk in game.get('bookmakers', []):
                        bk_name = bk['title']
                        for mkt in bk.get('markets', []):
                            for outcome in mkt.get('outcomes', []):
                                oname = outcome['name']
                                price = float(outcome['price'])
                                existing = db[gkey][mkt['key']].get(oname, {})
                                # Garde la meilleure cote parmi les bookmakers de référence
                                if _is_sharp(bk_name):
                                    db[gkey][mkt['key']][oname] = {
                                        'price': price,
                                        'source': bk_name
                                    }
                                elif oname not in db[gkey][mkt['key']]:
                                    db[gkey][mkt['key']][oname] = {
                                        'price': price,
                                        'source': bk_name
                                    }

                logger.info(f"  Odds API {sport_key}/{market}: {len(games)} matchs — {remaining} crédits restants")
                time.sleep(1)  # Rate limit

            except Exception as e:
                logger.debug(f"Odds API {sport_key}/{market}: {e}")
                continue

    return db


def _is_sharp(bookmaker: str) -> bool:
    """Pinnacle et Betfair sont les références sharp."""
    return any(s in bookmaker.lower() for s in ('pinnacle', 'betfair'))


# ── Matching fuzzy ───────────────────────────────────────────────
def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def find_in_db(home: str, away: str, db: dict) -> dict | None:
    best_score, best = 0.0, None
    for key, data in db.items():
        parts = key.split('|')
        if len(parts) != 2:
            continue
        score = (similarity(home, parts[0]) + similarity(away, parts[1])) / 2
        if score > best_score:
            best_score = score
            best = data
    return best if best_score >= 0.55 else None


# ── Calcul d'edge ────────────────────────────────────────────────
def remove_margin(odds: list) -> list:
    total = sum(1/o for o in odds if o > 1)
    if not total:
        return odds
    probs = [(1/o)/total for o in odds if o > 1]
    return [round(1/p, 4) for p in probs]


def edge(wina: float, ref: float) -> float:
    if ref <= 1:
        return 0.0
    return round((wina * (1/ref) - 1) * 100, 2)


# ── Analyse d'un événement ───────────────────────────────────────
def analyze_event(event: dict, ref_db: dict) -> list:
    """
    Analyse tous les marchés d'un événement.
    Retourne une liste d'alertes.
    """
    alerts = []
    ref_match = find_in_db(event['home'], event['away'], ref_db) if ref_db else None

    for market in event.get('markets', []):
        outcomes = market.get('outcomes', [])
        if len(outcomes) < 2:
            continue

        odds_vals = [o['odd'] for o in outcomes]

        for outcome in outcomes:
            wina_odd  = outcome['odd']
            oname     = outcome['name']
            alert_key = f"{event['id']}_{market['label']}_{oname}"

            if alert_key in already_alerted:
                continue

            ref_odd, ref_source = None, None

            # Cherche dans la DB externe (Pinnacle/Betfair)
            if ref_match:
                for mkt_key in ref_match:
                    if oname in ref_match[mkt_key]:
                        entry = ref_match[mkt_key][oname]
                        ref_odd    = entry['price']
                        ref_source = entry['source']
                        break

            # Fallback no-vig
            if not ref_odd:
                fair = remove_margin(odds_vals)
                idx  = [o['odd'] for o in outcomes].index(wina_odd)
                if idx < len(fair):
                    ref_odd    = fair[idx]
                    ref_source = "No-vig interne"

            if not ref_odd:
                continue

            e = edge(wina_odd, ref_odd)
            if e >= EDGE_THRESHOLD:
                alerts.append({
                    'market':     market['label'],
                    'outcome':    oname,
                    'wina_odd':   wina_odd,
                    'ref_odd':    ref_odd,
                    'ref_source': ref_source,
                    'edge':       e,
                })
                already_alerted.add(alert_key)

    return alerts


# ── Formatage des alertes Telegram ──────────────────────────────
def format_alert(event: dict, alerts: list) -> str:
    sport_emoji = {
        'Football': '⚽', 'Tennis': '🎾', 'Basketball': '🏀',
        'Rugby': '🏉', 'Cyclisme': '🚴', 'Biathlon': '🎿',
        'Formule 1': '🏎️', 'MMA / UFC': '🥊', 'Boxe': '🥊',
        'Golf': '⛳', 'Hockey sur glace': '🏒', 'Handball': '🤾',
        'Volleyball': '🏐', 'Baseball': '⚾', 'Américain': '🏈',
    }
    emoji = sport_emoji.get(event['sport'], '🎯')

    msg  = f"🚨 <b>VALUE BET — {event['sport']}</b>\n\n"
    msg += f"{emoji} <b>{event['home']}"
    if event['away']:
        msg += f" vs {event['away']}"
    msg += f"</b>\n"
    msg += f"📅 {event['date']}\n\n"

    for a in alerts:
        stars = "🔥🔥🔥" if a['edge'] >= 50 else ("🔥🔥" if a['edge'] >= 25 else "🔥")
        msg += f"{stars} <b>{a['market']}</b> — {a['outcome']}\n"
        msg += f"   Winamax  : <b>{a['wina_odd']:.2f}</b>\n"
        msg += f"   Référence: <b>{a['ref_odd']:.2f}</b> ({a['ref_source']})\n"
        msg += f"   Edge     : <b>+{a['edge']}%</b>\n\n"

    msg += "⚠️ Jouez responsablement. Pas un conseil financier."
    return msg


# ── Boucle principale ────────────────────────────────────────────
def scan_all(ref_db: dict) -> int:
    logger.info("🔍 Scan de tous les événements Winamax...")
    events = get_all_winamax_events()
    total_alerts = 0

    for event in events:
        try:
            alerts = analyze_event(event, ref_db)
            if alerts:
                total_alerts += len(alerts)
                msg = format_alert(event, alerts)
                send_telegram(msg)
                logger.info(f"ALERTE [{event['sport']}] {event['home']} vs {event['away']} — {len(alerts)} value(s)")
        except Exception as e:
            logger.error(f"Erreur événement: {e}")

    logger.info(f"✅ Scan terminé — {len(events)} événements, {total_alerts} alertes")
    return total_alerts


def main():
    logger.info("🤖 Bot Winamax Value Bet v3 — TOUS SPORTS / TOUS MARCHÉS")
    logger.info(f"⚙️  Seuil edge   : {EDGE_THRESHOLD}%")
    logger.info(f"⏱️  Intervalle   : {CHECK_INTERVAL}s")
    logger.info(f"📊  Référence    : Pinnacle + Betfair Exchange + No-vig")
    logger.info(f"🏅  Sports       : Tous ({len(WINAMAX_SPORTS)} catégories)")
    logger.info(f"📋  Marchés      : Tous (1N2, buteur, handicap, O/U, outrights...)")

    send_telegram(
        "🤖 <b>Bot Value Bet v3 démarré</b>\n\n"
        "🏅 Sports : TOUS (foot, tennis, vélo, biathlon, F1...)\n"
        "📋 Marchés : TOUS (1N2, buteur, handi, O/U, vainqueur...)\n"
        "📊 Référence : Pinnacle + Betfair Exchange\n"
        f"⚙️ Seuil : +{EDGE_THRESHOLD}% d'edge\n"
        f"⏱ Scan toutes les {CHECK_INTERVAL//60} min"
    )

    # Construit la DB de référence une fois au démarrage
    logger.info("📥 Construction de la DB de référence Pinnacle/Betfair...")
    ref_db = build_reference_db()
    logger.info(f"DB référence : {len(ref_db)} matchs chargés")
    ref_db_last_update = time.time()

    while True:
        try:
            # Rafraîchit la DB de référence toutes les 30 minutes
            if time.time() - ref_db_last_update > 1800:
                logger.info("🔄 Mise à jour DB référence...")
                ref_db = build_reference_db()
                ref_db_last_update = time.time()
                logger.info(f"DB référence mise à jour : {len(ref_db)} matchs")

            scan_all(ref_db)

        except Exception as e:
            logger.error(f"Erreur générale: {e}")

        logger.info(f"⏳ Prochain scan dans {CHECK_INTERVAL}s... (crédits Odds API utilisés: {credits_used})")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
