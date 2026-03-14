"""
Bot Winamax Value Bet v4 — SOCKET.IO DIRECT
=============================================
- Connexion directe au WebSocket Socket.IO de Winamax
- Pas de Selenium, fonctionne sur Railway
- Tous les sports et marchés en temps réel
- Comparaison vs Pinnacle + Betfair (The Odds API)
- Fallback no-vig si pas de référence externe
- Alertes Telegram
"""

import socketio
import requests
import time
import os
import logging
import threading
import json
from difflib import SequenceMatcher
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
EDGE_THRESHOLD   = float(os.getenv("EDGE_THRESHOLD", "15"))
CHECK_INTERVAL   = int(os.getenv("CHECK_INTERVAL", "300"))
MIN_ODD          = float(os.getenv("MIN_ODD", "1.05"))
MAX_ODD          = float(os.getenv("MAX_ODD", "50.0"))

WINAMAX_WSS  = "wss://sports-eu-west-3.winamax.fr"
WINAMAX_PATH = "/uof-sports-server/socket.io/"

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.winamax.fr/paris-sportifs",
    "Origin": "https://www.winamax.fr",
}

ODDS_API_SPORTS = [
    "soccer_france_ligue_one", "soccer_epl", "soccer_spain_la_liga",
    "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_uefa_champs_league",
    "soccer_france_ligue_two", "soccer_belgium_first_div", "soccer_portugal_primeira_liga",
    "basketball_nba", "basketball_euroleague",
    "americanfootball_nfl",
    "icehockey_nhl",
    "rugbyunion_premiership", "rugbyunion_super_rugby",
    "mma_mixed_martial_arts",
    "boxing_boxing",
]

already_alerted      = set()
credits_used         = 0
winamax_events_store = []
winamax_events_lock  = threading.Lock()
socketio_connected   = False


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TELEGRAM OFF] {message[:150]}")
        return
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
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


def start_socketio_listener():
    global socketio_connected

    sio = socketio.Client(
        logger=False,
        engineio_logger=False,
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=5,
        reconnection_delay_max=30,
    )

    @sio.event
    def connect():
        global socketio_connected
        socketio_connected = True
        logger.info("✅ Socket.IO Winamax connecté !")
        for payload in [
            {"type": "sports", "sportId": "all"},
            {"type": "matches", "status": "open"},
        ]:
            try:
                sio.emit("subscribe", payload)
            except Exception:
                pass
        for ev in ("getMatches", "getSports", "getAllMatches"):
            try:
                sio.emit(ev, {})
            except Exception:
                pass

    @sio.event
    def disconnect():
        global socketio_connected
        socketio_connected = False
        logger.warning("⚠️ Socket.IO déconnecté — reconnexion auto...")

    @sio.event
    def connect_error(data):
        logger.error(f"Socket.IO erreur: {data}")

    @sio.on('*')
    def catch_all(event, data):
        _process_socketio_event(event, data)

    for ev in ['matches', 'sports', 'odds', 'events', 'data',
               'matchUpdate', 'oddsUpdate', 'allMatches', 'update']:
        sio.on(ev, lambda data, e=ev: _process_socketio_event(e, data))

    while True:
        try:
            logger.info(f"🔌 Connexion Socket.IO vers {WINAMAX_WSS}...")
            sio.connect(
                WINAMAX_WSS,
                socketio_path=WINAMAX_PATH,
                transports=['websocket', 'polling'],
                headers={
                    "User-Agent": HEADERS_HTTP["User-Agent"],
                    "Origin": "https://www.winamax.fr",
                    "Referer": "https://www.winamax.fr/paris-sportifs",
                },
                wait_timeout=15,
            )
            sio.wait()
        except Exception as e:
            logger.error(f"Socket.IO exception: {e}")
            socketio_connected = False
            time.sleep(10)


def _process_socketio_event(event: str, data):
    try:
        if not data:
            return
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return
        if not isinstance(data, (dict, list)):
            return

        matches = []
        if isinstance(data, list):
            matches = data
        elif isinstance(data, dict):
            for key in ('matches', 'events', 'data', 'items', 'sports', 'results'):
                val = data.get(key)
                if isinstance(val, list) and val:
                    matches = val
                    break
                elif isinstance(val, dict) and val:
                    matches = list(val.values())
                    break
            if not matches and 'id' in data:
                matches = [data]

        if not matches:
            return

        events = [_parse_match(m) for m in matches if isinstance(m, dict)]
        events = [e for e in events if e]

        if events:
            with winamax_events_lock:
                existing_ids = {e['id'] for e in winamax_events_store}
                new_count = 0
                for ev in events:
                    if ev['id'] not in existing_ids:
                        winamax_events_store.append(ev)
                        new_count += 1
                    else:
                        for i, stored in enumerate(winamax_events_store):
                            if stored['id'] == ev['id']:
                                winamax_events_store[i] = ev
                                break
            if new_count > 0:
                logger.info(f"Socket.IO [{event}]: +{new_count} nouveaux (total: {len(winamax_events_store)})")
    except Exception as e:
        logger.debug(f"_process_socketio_event: {e}")


def _parse_match(match: dict) -> dict | None:
    try:
        match_id = str(match.get('id') or match.get('matchId') or match.get('eventId') or '')
        if not match_id:
            return None

        home = (match.get('homeTeam') or match.get('home') or match.get('team1') or
                (match.get('homeCompetitor') or {}).get('name', ''))
        away = (match.get('awayTeam') or match.get('away') or match.get('team2') or
                (match.get('awayCompetitor') or {}).get('name', ''))
        title = match.get('title') or match.get('name') or match.get('label', '')

        if not home and title:
            home, away = _split_title(title)
        if not home:
            return None

        sport = (match.get('sport') or match.get('sportName') or
                 match.get('category') or str(match.get('sportId', 'Sport')))
        date  = (match.get('matchStart') or match.get('startTime') or
                 match.get('date') or match.get('scheduledAt', ''))

        markets = []
        for mkey in ('betTypes', 'bets', 'markets', 'betGroups', 'odds'):
            raw = match.get(mkey)
            if not raw:
                continue
            items = raw if isinstance(raw, list) else list(raw.values())
            for m in items:
                if isinstance(m, dict):
                    pm = _parse_market(m)
                    if pm:
                        markets.append(pm)

        if not markets:
            direct = _extract_direct_odds(match, str(home), str(away))
            if direct:
                markets.append(direct)

        if not markets:
            return None

        return {
            'id':      match_id,
            'sport':   str(sport)[:50],
            'home':    str(home)[:100],
            'away':    str(away)[:100],
            'date':    str(date)[:16].replace('T', ' ') if date else 'N/A',
            'markets': markets,
        }
    except Exception as e:
        logger.debug(f"_parse_match: {e}")
        return None


def _extract_direct_odds(match: dict, home: str, away: str) -> dict | None:
    try:
        outcomes = []
        for name, k1, k2, k3 in [
            (home, 'odds1', 'homeOdds', 'odd1'),
            ('Nul', 'oddsX', 'drawOdds', 'oddX'),
            (away, 'odds2', 'awayOdds', 'odd2'),
        ]:
            price = match.get(k1) or match.get(k2) or match.get(k3)
            if price:
                try:
                    p = float(price)
                    if MIN_ODD <= p <= MAX_ODD:
                        outcomes.append({'name': name, 'odd': p})
                except (ValueError, TypeError):
                    pass
        return {'label': '1N2', 'outcomes': outcomes} if len(outcomes) >= 2 else None
    except Exception:
        return None


def _parse_market(market: dict) -> dict | None:
    try:
        label = (market.get('label') or market.get('name') or
                 market.get('title') or market.get('betTypeName') or 'Marché principal')
        raw = (market.get('outcomes') or market.get('bets') or
               market.get('selections') or market.get('runners') or [])
        outcomes = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            name  = o.get('label') or o.get('name') or o.get('title') or ''
            price = o.get('odds') or o.get('price') or o.get('odd') or o.get('decimalOdds', 0)
            try:
                p = float(price)
                if name and MIN_ODD <= p <= MAX_ODD:
                    outcomes.append({'name': name, 'odd': p})
            except (ValueError, TypeError):
                pass
        return {'label': str(label)[:100], 'outcomes': outcomes} if len(outcomes) >= 2 else None
    except Exception:
        return None


def _split_title(title: str) -> tuple:
    for sep in (' - ', ' vs ', ' / ', ' – ', ' contre '):
        if sep in title:
            parts = title.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return title.strip(), ''


def build_reference_db() -> dict:
    global credits_used
    if not ODDS_API_KEY:
        return {}
    db = {}
    for sport_key in ODDS_API_SPORTS:
        for market in ['h2h', 'spreads', 'totals']:
            try:
                r = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                    params={
                        "apiKey": ODDS_API_KEY, "regions": "eu,uk",
                        "markets": market,
                        "bookmakers": "pinnacle,betfair_ex_eu,betfair_ex_uk",
                        "oddsFormat": "decimal",
                    },
                    timeout=15
                )
                credits_used = int(r.headers.get('x-requests-used', 0))
                remaining    = int(r.headers.get('x-requests-remaining', 999))

                if r.status_code == 401:
                    logger.error("Odds API: clé invalide")
                    return db
                if r.status_code == 422:
                    continue
                if r.status_code == 429:
                    logger.warning("Odds API rate limit — pause 60s")
                    time.sleep(60)
                    continue
                if r.status_code != 200:
                    continue

                for game in r.json():
                    gkey = f"{game['home_team']}|{game['away_team']}"
                    if gkey not in db:
                        db[gkey] = defaultdict(lambda: defaultdict(dict))
                    for bk in game.get('bookmakers', []):
                        sharp = any(s in bk['title'].lower() for s in ('pinnacle', 'betfair'))
                        for mkt in bk.get('markets', []):
                            for o in mkt.get('outcomes', []):
                                oname = o['name']
                                price = float(o['price'])
                                if sharp or oname not in db[gkey][mkt['key']]:
                                    db[gkey][mkt['key']][oname] = {'price': price, 'source': bk['title']}

                logger.info(f"  Odds API {sport_key}/{market}: {remaining} crédits restants")
                time.sleep(1)
            except Exception as e:
                logger.debug(f"Odds API {sport_key}/{market}: {e}")
    return db


def find_in_db(home: str, away: str, db: dict) -> dict | None:
    best_score, best = 0.0, None
    for key, data in db.items():
        parts = key.split('|')
        if len(parts) != 2:
            continue
        score = (SequenceMatcher(None, home.lower(), parts[0].lower()).ratio() +
                 SequenceMatcher(None, away.lower(), parts[1].lower()).ratio()) / 2
        if score > best_score:
            best_score, best = score, data
    return best if best_score >= 0.55 else None


def remove_margin(odds: list) -> list:
    total = sum(1/o for o in odds if o > 1)
    if not total:
        return odds
    return [round(1/((1/o)/total), 4) for o in odds if o > 1]


def edge(wina: float, ref: float) -> float:
    return 0.0 if ref <= 1 else round((wina * (1/ref) - 1) * 100, 2)


def analyze_event(event: dict, ref_db: dict) -> list:
    alerts    = []
    ref_match = find_in_db(event['home'], event['away'], ref_db) if ref_db else None

    for market in event.get('markets', []):
        outcomes  = market.get('outcomes', [])
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
            if ref_match:
                for mkt_key in ref_match:
                    if oname in ref_match[mkt_key]:
                        entry      = ref_match[mkt_key][oname]
                        ref_odd    = entry['price']
                        ref_source = entry['source']
                        break

            if not ref_odd:
                fair = remove_margin(odds_vals)
                idx  = [o['odd'] for o in outcomes].index(wina_odd)
                if idx < len(fair):
                    ref_odd, ref_source = fair[idx], "No-vig interne"

            if not ref_odd:
                continue

            e = edge(wina_odd, ref_odd)
            if e >= EDGE_THRESHOLD:
                alerts.append({
                    'market': market['label'], 'outcome': oname,
                    'wina_odd': wina_odd, 'ref_odd': ref_odd,
                    'ref_source': ref_source, 'edge': e,
                })
                already_alerted.add(alert_key)
    return alerts


def format_alert(event: dict, alerts: list) -> str:
    EMOJIS = {
        'football': '⚽', 'tennis': '🎾', 'basketball': '🏀',
        'rugby': '🏉', 'cyclisme': '🚴', 'formule': '🏎️',
        'mma': '🥊', 'boxe': '🥊', 'golf': '⛳', 'hockey': '🏒',
    }
    emoji = next((v for k, v in EMOJIS.items() if k in event['sport'].lower()), '🎯')

    msg  = f"🚨 <b>VALUE BET — {event['sport']}</b>\n\n"
    msg += f"{emoji} <b>{event['home']}"
    if event['away']:
        msg += f" vs {event['away']}"
    msg += f"</b>\n📅 {event['date']}\n\n"

    for a in alerts:
        stars = "🔥🔥🔥" if a['edge'] >= 50 else ("🔥🔥" if a['edge'] >= 25 else "🔥")
        msg += f"{stars} <b>{a['market']}</b> — {a['outcome']}\n"
        msg += f"   Winamax  : <b>{a['wina_odd']:.2f}</b>\n"
        msg += f"   Référence: <b>{a['ref_odd']:.2f}</b> ({a['ref_source']})\n"
        msg += f"   Edge     : <b>+{a['edge']}%</b>\n\n"

    msg += "⚠️ Jouez responsablement. Pas un conseil financier."
    return msg


def scan_all(ref_db: dict) -> int:
    logger.info("🔍 Scan de tous les événements Winamax...")
    events = get_winamax_events()

    if not events:
        logger.warning(f"⚠️ Aucun événement (Socket.IO connecté: {socketio_connected})")
        return 0

    total_alerts = 0
    for event in events:
        try:
            alerts = analyze_event(event, ref_db)
            if alerts:
                total_alerts += len(alerts)
                send_telegram(format_alert(event, alerts))
                logger.info(f"ALERTE [{event['sport']}] {event['home']} vs {event['away']} — {len(alerts)} value(s)")
        except Exception as e:
            logger.error(f"Erreur événement: {e}")

    logger.info(f"✅ Scan terminé — {len(events)} événements, {total_alerts} alertes")
    return total_alerts


def get_winamax_events() -> list:
    with winamax_events_lock:
        return list(winamax_events_store)


def main():
    logger.info("🤖 Bot Winamax Value Bet v4 — SOCKET.IO DIRECT")
    logger.info(f"⚙️  Seuil edge  : {EDGE_THRESHOLD}%")
    logger.info(f"⏱️  Intervalle  : {CHECK_INTERVAL}s")

    send_telegram(
        "🤖 <b>Bot Value Bet v4 démarré (Socket.IO direct)</b>\n\n"
        "🔌 Connexion temps réel à Winamax\n"
        "📊 Référence : Pinnacle + Betfair Exchange\n"
        f"⚙️ Seuil : +{EDGE_THRESHOLD}% d'edge\n"
        f"⏱ Scan toutes les {CHECK_INTERVAL//60} min"
    )

    sio_thread = threading.Thread(target=start_socketio_listener, daemon=True)
    sio_thread.start()
    logger.info("🧵 Thread Socket.IO lancé...")

    for i in range(30):
        if socketio_connected:
            break
        time.sleep(1)
        if i % 5 == 4:
            logger.info(f"  Attente Socket.IO... ({i+1}s)")

    logger.info("📥 Réception données initiales (15s)...")
    time.sleep(15)

    logger.info("📥 Construction DB référence Pinnacle/Betfair...")
    ref_db = build_reference_db()
    logger.info(f"DB référence : {len(ref_db)} matchs chargés")
    ref_db_last_update = time.time()

    while True:
        try:
            if time.time() - ref_db_last_update > 1800:
                logger.info("🔄 Mise à jour DB référence...")
                ref_db = build_reference_db()
                ref_db_last_update = time.time()

            scan_all(ref_db)
        except Exception as e:
            logger.error(f"Erreur générale: {e}")

        logger.info(f"⏳ Prochain scan dans {CHECK_INTERVAL}s... (crédits Odds API: {credits_used})")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
```

---

## 📄 `requirements.txt` — copie tout ça
```
requests
python-socketio[client]
websocket-client
