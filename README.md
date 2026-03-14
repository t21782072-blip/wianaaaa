"""
Bot Winamax Value Bet v3 — CORRIGÉ
====================================
- Utilise la vraie API Winamax (api/1/competitions + api/1/matches)
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
MIN_ODD          = float(os.getenv("MIN_ODD", "1.05"))
MAX_ODD          = float(os.getenv("MAX_ODD", "50.0"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.winamax.fr/paris-sportifs",
    "Origin": "https://www.winamax.fr",
}

# Sports The Odds API disponibles (pour comparaison externe)
ODDS_API_SPORTS = [
    "soccer_france_ligue_one", "soccer_epl", "soccer_spain_la_liga",
    "soccer_germany_bundesliga", "soccer_italy_serie_a", "soccer_uefa_champs_league",
    "soccer_france_ligue_two", "soccer_belgium_first_div", "soccer_portugal_primeira_liga",
    "tennis_atp_french_open", "tennis_wta_french_open", "tennis_atp_wimbledon",
    "tennis_atp_us_open", "tennis_atp_aus_open",
    "basketball_nba", "basketball_euroleague",
    "americanfootball_nfl",
    "icehockey_nhl",
    "rugbyunion_premiership", "rugbyunion_super_rugby",
    "golf_masters_tournament_winner", "golf_pga_championship_winner",
    "mma_mixed_martial_arts",
    "boxing_boxing",
    "cricket_test_match",
]

already_alerted = set()
credits_used    = 0


# ── Telegram ─────────────────────────────────────────────────────
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"[TELEGRAM OFF] {message[:150]}")
        return
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


# ── Winamax — VRAIE API ──────────────────────────────────────────
def get_all_winamax_events() -> list:
    """
    Récupère tous les événements Winamax via la vraie API publique.
    Endpoint : https://www.winamax.fr/api/1/sports
    """
    events = []

    try:
        # Étape 1 : récupère tous les sports disponibles
        r = requests.get(
            "https://www.winamax.fr/api/1/sports",
            headers=HEADERS,
            timeout=20
        )
        if r.status_code != 200:
            logger.error(f"Winamax API sports: HTTP {r.status_code}")
            return _fallback_winamax()

        sports_data = r.json()
        sport_ids = []

        if isinstance(sports_data, list):
            sport_ids = [s.get('id') or s.get('sportId') for s in sports_data if s]
        elif isinstance(sports_data, dict):
            items = sports_data.get('sports', sports_data.get('items', []))
            sport_ids = [s.get('id') or s.get('sportId') for s in items if s]

        sport_ids = [sid for sid in sport_ids if sid]
        logger.info(f"Winamax: {len(sport_ids)} sports trouvés")

        # Étape 2 : pour chaque sport, récupère les compétitions
        for sport_id in sport_ids[:20]:  # Limite à 20 sports pour éviter le rate limit
            sport_events = _fetch_sport_events(sport_id)
            events.extend(sport_events)
            if sport_events:
                logger.info(f"  Sport {sport_id}: {len(sport_events)} événements")
            time.sleep(0.5)

    except Exception as e:
        logger.error(f"Erreur API Winamax principale: {e}")
        return _fallback_winamax()

    # Si rien trouvé, essaie les fallbacks
    if not events:
        logger.warning("API sports vide — essai fallback direct")
        events = _fallback_winamax()

    logger.info(f"Total Winamax : {len(events)} événements, {sum(len(e.get('markets',[])) for e in events)} marchés")
    return events


def _fetch_sport_events(sport_id) -> list:
    """Récupère les événements d'un sport via l'API Winamax."""
    events = []

    # Essaie d'abord de récupérer les compétitions du sport
    try:
        r = requests.get(
            f"https://www.winamax.fr/api/1/sports/{sport_id}/competitions",
            headers=HEADERS,
            timeout=15
        )
        if r.status_code == 200:
            competitions = r.json()
            comp_list = competitions if isinstance(competitions, list) else competitions.get('competitions', [])

            for comp in comp_list[:10]:  # Limite à 10 compétitions par sport
                comp_id = comp.get('id') or comp.get('competitionId')
                if comp_id:
                    comp_events = _fetch_competition_matches(comp_id, comp.get('name', ''))
                    events.extend(comp_events)
                    time.sleep(0.3)
            return events
    except Exception:
        pass

    # Fallback : matches directs par sport
    try:
        r = requests.get(
            f"https://www.winamax.fr/api/1/sports/{sport_id}/matches",
            headers=HEADERS,
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            matches = data if isinstance(data, list) else data.get('matches', data.get('items', []))
            for match in matches:
                parsed = _parse_winamax_match(match)
                if parsed:
                    events.append(parsed)
    except Exception:
        pass

    return events


def _fetch_competition_matches(comp_id, comp_name: str = '') -> list:
    """Récupère les matchs d'une compétition."""
    events = []
    try:
        r = requests.get(
            f"https://www.winamax.fr/api/1/competitions/{comp_id}/matches",
            headers=HEADERS,
            timeout=15
        )
        if r.status_code != 200:
            return []

        data = r.json()
        matches = data if isinstance(data, list) else data.get('matches', data.get('items', []))

        for match in matches:
            parsed = _parse_winamax_match(match, comp_name)
            if parsed:
                events.append(parsed)

    except Exception as e:
        logger.debug(f"Compétition {comp_id}: {e}")

    return events


def _parse_winamax_match(match: dict, sport_label: str = '') -> dict | None:
    """Parse un match Winamax depuis l'API."""
    try:
        # ID
        match_id = str(match.get('id') or match.get('matchId') or match.get('eventId', ''))
        if not match_id:
            return None

        # Titre / participants
        title = match.get('title') or match.get('name') or match.get('label', '')
        home = match.get('homeTeam') or match.get('home') or match.get('team1', '')
        away = match.get('awayTeam') or match.get('away') or match.get('team2', '')

        # Si on n'a pas home/away séparément, on parse le titre
        if not home and title:
            home, away = _split_title(title)
        if not home:
            return None

        # Sport
        sport = (sport_label or
                 match.get('sport') or
                 match.get('sportName') or
                 match.get('category', 'Sport'))

        # Date
        date = (match.get('matchStart') or
                match.get('startTime') or
                match.get('date') or
                match.get('scheduledAt', ''))

        # Marchés
        markets = []

        # Cherche les marchés dans différents champs possibles
        for mkey in ('betTypes', 'bets', 'markets', 'betGroups', 'mainBets', 'odds'):
            raw = match.get(mkey)
            if not raw:
                continue
            if isinstance(raw, list):
                for m in raw:
                    parsed_m = _parse_market(m)
                    if parsed_m:
                        markets.append(parsed_m)
            elif isinstance(raw, dict):
                for mid, m in raw.items():
                    if isinstance(m, dict):
                        parsed_m = _parse_market(m)
                        if parsed_m:
                            markets.append(parsed_m)

        # Marché principal direct (cotes 1N2 souvent à la racine)
        if not markets:
            direct_market = _extract_direct_odds(match, home, away)
            if direct_market:
                markets.append(direct_market)

        if not markets:
            return None

        return {
            'id': match_id,
            'sport': str(sport)[:50],
            'home': str(home)[:100],
            'away': str(away)[:100],
            'date': str(date)[:16].replace('T', ' ') if date else 'N/A',
            'markets': markets,
        }
    except Exception as e:
        logger.debug(f"Parse match erreur: {e}")
        return None


def _extract_direct_odds(match: dict, home: str, away: str) -> dict | None:
    """Tente d'extraire des cotes directement depuis les champs du match."""
    try:
        outcomes = []
        # Formats courants : odds1, oddsX, odds2 ou homeOdds, drawOdds, awayOdds
        pairs = [
            (home, match.get('odds1') or match.get('homeOdds') or match.get('odd1')),
            ('Nul', match.get('oddsX') or match.get('drawOdds') or match.get('oddX')),
            (away, match.get('odds2') or match.get('awayOdds') or match.get('odd2')),
        ]
        for name, price in pairs:
            if price:
                try:
                    p = float(price)
                    if MIN_ODD <= p <= MAX_ODD:
                        outcomes.append({'name': name, 'odd': p})
                except (ValueError, TypeError):
                    pass

        if len(outcomes) >= 2:
            return {'label': '1N2', 'outcomes': outcomes}
    except Exception:
        pass
    return None


def _parse_market(market: dict) -> dict | None:
    try:
        label = (market.get('label') or
                 market.get('name') or
                 market.get('title') or
                 market.get('betTypeName') or
                 'Marché principal')

        outcomes_raw = (market.get('outcomes') or
                        market.get('bets') or
                        market.get('selections') or
                        market.get('runners') or [])

        outcomes = []
        for o in outcomes_raw:
            if not isinstance(o, dict):
                continue
            name  = o.get('label') or o.get('name') or o.get('title') or o.get('runnerName', '')
            price_raw = o.get('odds') or o.get('price') or o.get('odd') or o.get('decimalOdds', 0)
            try:
                price = float(price_raw)
            except (ValueError, TypeError):
                continue
            if name and MIN_ODD <= price <= MAX_ODD:
                outcomes.append({'name': name, 'odd': price})

        if len(outcomes) < 2:
            return None

        return {'label': str(label)[:100], 'outcomes': outcomes}
    except Exception:
        return None


def _split_title(title: str) -> tuple:
    for sep in (' - ', ' vs ', ' / ', ' – ', ' contre '):
        if sep in title:
            parts = title.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    return title.strip(), ''


def _fallback_winamax() -> list:
    """
    Fallback : essaie des endpoints alternatifs connus de Winamax.
    """
    events = []
    fallback_urls = [
        "https://www.winamax.fr/api/1/matches?limit=200&status=open",
        "https://www.winamax.fr/api/1/events?limit=200",
        "https://www.winamax.fr/api/1/bets?limit=200",
    ]

    for url in fallback_urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                items = (data if isinstance(data, list)
                         else data.get('matches', data.get('events', data.get('items', []))))
                for item in items:
                    parsed = _parse_winamax_match(item)
                    if parsed:
                        events.append(parsed)
                if events:
                    logger.info(f"Fallback OK: {url} → {len(events)} événements")
                    break
        except Exception as e:
            logger.debug(f"Fallback {url}: {e}")
            continue

    return events


# ── The Odds API — Pinnacle + Betfair, tous marchés ─────────────
def build_reference_db() -> dict:
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
                if r.status_code == 422:
                    # Sport/marché non disponible, on skip
                    continue
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
                time.sleep(1)

            except Exception as e:
                logger.debug(f"Odds API {sport_key}/{market}: {e}")
                continue

    return db


def _is_sharp(bookmaker: str) -> bool:
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

            if ref_match:
                for mkt_key in ref_match:
                    if oname in ref_match[mkt_key]:
                        entry = ref_match[mkt_key][oname]
                        ref_odd    = entry['price']
                        ref_source = entry['source']
                        break

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
        'Formule 1': '🏎️', 'MMA': '🥊', 'Boxe': '🥊',
        'Golf': '⛳', 'Hockey': '🏒', 'Handball': '🤾',
        'Volleyball': '🏐', 'Baseball': '⚾',
    }
    emoji = '🎯'
    for k, v in sport_emoji.items():
        if k.lower() in event['sport'].lower():
            emoji = v
            break

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
    logger.info("🤖 Bot Winamax Value Bet v3 — CORRIGÉ")
    logger.info(f"⚙️  Seuil edge   : {EDGE_THRESHOLD}%")
    logger.info(f"⏱️  Intervalle   : {CHECK_INTERVAL}s")
    logger.info(f"📊  Référence    : Pinnacle + Betfair Exchange + No-vig")

    send_telegram(
        "🤖 <b>Bot Value Bet v3 démarré (version corrigée)</b>\n\n"
        "🏅 Sports : TOUS\n"
        "📋 Marchés : TOUS (1N2, buteur, handi, O/U, vainqueur...)\n"
        "📊 Référence : Pinnacle + Betfair Exchange\n"
        f"⚙️ Seuil : +{EDGE_THRESHOLD}% d'edge\n"
        f"⏱ Scan toutes les {CHECK_INTERVAL//60} min"
    )

    logger.info("📥 Construction de la DB de référence Pinnacle/Betfair...")
    ref_db = build_reference_db()
    logger.info(f"DB référence : {len(ref_db)} matchs chargés")
    ref_db_last_update = time.time()

    while True:
        try:
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
