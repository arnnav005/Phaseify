import os
import requests
from flask import Flask, redirect, request, session, jsonify, url_for, render_template
from dotenv import load_dotenv
from urllib.parse import urlencode
import logging
from datetime import datetime, timedelta
from collections import defaultdict
import json
import time

# --- Setup ---
logging.basicConfig(level=logging.INFO)
load_dotenv()
app = Flask(__name__, template_folder='templates') 
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(24))

# --- Spotify Credentials and API Configuration ---
CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1/"
SCOPE = "user-top-read user-library-read"

# ===================================================================
# INTERNAL HELPER FUNCTIONS
# ===================================================================

def _get_api_data(endpoint, access_token, params=None):
    headers = {'Authorization': f'Bearer {access_token}'}
    res = requests.get(API_BASE_URL + endpoint, headers=headers, params=params)
    res.raise_for_status()
    return res.json()

def _get_all_pages(url, access_token):
    items = []
    endpoint = url
    while endpoint:
        data = _get_api_data(endpoint, access_token)
        items.extend(data.get('items', []))
        next_url = data.get('next')
        endpoint = next_url.replace(API_BASE_URL, '') if next_url else None
    return items

def _get_artist_genres(artist_ids, access_token):
    genres_map = {}
    for i in range(0, len(artist_ids), 50):
        chunk = artist_ids[i:i+50]
        params = {'ids': ','.join(chunk)}
        data = _get_api_data('artists', access_token, params=params)
        for artist in data.get('artists', []):
            if artist:
                genres_map[artist['id']] = artist.get('genres', [])
    return genres_map

def _get_season_key(dt):
    month = dt.month
    year = dt.year
    if month in (1, 2): return f"Winter {year - 1}"
    if month in (3, 4, 5): return f"Spring {year}"
    if month in (6, 7, 8): return f"Summer {year}"
    if month in (9, 10, 11): return f"Autumn {year}"
    if month == 12: return f"Winter {year}"

def _get_ai_phase_details(phase_characteristics, top_artists):
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    fallback_response = {"phase_name": f"Your {phase_characteristics['period']} Era", "phase_summary": "A distinct period in your listening journey."}
    if not gemini_api_key: return fallback_response
    
    gemini_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
    logging.info(f"Requesting AI details for phase: {phase_characteristics['period']}")
    prompt = f"""
You are a creative music journalist. Based on the following data about a person's music phase, generate two things:
1. A cool, evocative "Daylist-style" name for the phase (3-5 words, no numbers).
2. A short, personal, one-paragraph summary describing the vibe of this era.
**Phase Data:**
- **Period:** {phase_characteristics['period']}
- **Top Genres:** {', '.join(phase_characteristics['top_genres'])}
- **Top Artists during this phase:** {', '.join(top_artists)}
- **Era Vibe:** {'Modern mainstream' if phase_characteristics['avg_release_year'] > 2010 else 'Nostalgic throwback'}
- **Popularity Vibe:** {'Mainstream hits' if phase_characteristics['avg_popularity'] > 60 else 'Underground discoveries'}
Return the response ONLY as a valid JSON object with the keys "phase_name" and "phase_summary".
"""
    schema = {"type": "OBJECT", "properties": {"phase_name": {"type": "STRING"}, "phase_summary": {"type": "STRING"}}}
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}}
    
    try:
        response = requests.post(gemini_api_url, headers={"Content-Type": "application/json"}, data=json.dumps(payload))
        response.raise_for_status()
        result_text = response.json()['candidates'][0]['content']['parts'][0]['text']
        return json.loads(result_text)
    except Exception as e:
        logging.error(f"AI details generation failed: {e}")
        return fallback_response

# ===================================================================
# FLASK ROUTES
# ===================================================================

@app.route('/')
def index():
    if 'access_token' in session and 'user_id' in session:
        return redirect(url_for('loading'))
    return render_template('login.html')

@app.route('/login')
def login():
    params = {'response_type': 'code', 'redirect_uri': REDIRECT_URI, 'scope': SCOPE, 'client_id': CLIENT_ID, 'show_dialog': 'true'}
    return redirect(f"{AUTH_URL}?{urlencode(params)}")

@app.route('/callback')
def callback():
    if 'error' in request.args: return jsonify({"error": request.args['error']})
    if 'code' in request.args:
        payload = {'grant_type': 'authorization_code', 'code': request.args['code'], 'redirect_uri': REDIRECT_URI, 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET}
        res = requests.post(TOKEN_URL, data=payload)
        res_data = res.json()
        session['access_token'] = res_data.get('access_token')
        
        user_data = _get_api_data('me', session['access_token'])
        session['user_id'] = user_data.get('id')
        session['display_name'] = user_data.get('display_name', 'music lover')
        
        return redirect(url_for('loading'))
    return jsonify({"error": "Unknown callback error"})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/loading')
def loading():
    if 'access_token' not in session: return redirect('/login')
    return render_template('loading.html')

@app.route('/timeline')
def timeline():
    access_token = session.get('access_token')
    if not access_token: return redirect('/login')

    try:
        logging.info("Performing analysis for the last 3 years...")
        
        # --- NEW: Filter saved tracks to the last 3 years ---
        all_saved_tracks = _get_all_pages('me/tracks?limit=50', access_token)
        three_years_ago = datetime.now() - timedelta(days=3*365)
        
        recent_saved_tracks = []
        for item in all_saved_tracks:
            if item.get('added_at'):
                added_date = datetime.fromisoformat(item['added_at'].replace('Z', ''))
                if added_date > three_years_ago:
                    recent_saved_tracks.append(item)
        logging.info(f"Found {len(recent_saved_tracks)} tracks saved in the last 3 years.")
        
        top_tracks = {
            'long_term': _get_api_data('me/top/tracks', access_token, {'limit': 50, 'time_range': 'long_term'})['items'],
            'medium_term': _get_api_data('me/top/tracks', access_token, {'limit': 50, 'time_range': 'medium_term'})['items'],
            'short_term': _get_api_data('me/top/tracks', access_token, {'limit': 50, 'time_range': 'short_term'})['items']
        }
        
        relevance_scores = defaultdict(int)
        for item in recent_saved_tracks:
            if item.get('track'): relevance_scores[item['track']['id']] += 1
        for t in top_tracks['long_term']: relevance_scores[t['id']] += 2
        for t in top_tracks['medium_term']: relevance_scores[t['id']] += 5
        for t in top_tracks['short_term']: relevance_scores[t['id']] += 10
        
        all_tracks_info, all_artist_ids = {}, set()
        all_track_items = recent_saved_tracks + [{'track': t, 'added_at': None} for t_list in top_tracks.values() for t in t_list]

        for item in all_track_items:
            track = item.get('track')
            if not track or not track.get('id') or track['id'] in all_tracks_info: continue
            artist = track.get('artists', [{}])[0]
            album = track.get('album', {})
            images = album.get('images', [])
            cover_url = images[0]['url'] if images else 'https://placehold.co/128x128/121212/FFFFFF?text=?'
            all_tracks_info[track['id']] = {'name': track.get('name', 'N/A'), 'artist_id': artist.get('id'), 'album_id': album.get('id'), 'artist_name': artist.get('name', 'N/A'), 'popularity': track.get('popularity', 0), 'release_year': int(album.get('release_date', '0').split('-')[0]), 'added_at': item.get('added_at'), 'relevance_score': relevance_scores.get(track['id'], 0), 'cover_url': cover_url}
            if artist.get('id'): all_artist_ids.add(artist.get('id'))

        artist_genres_map = _get_artist_genres(list(all_artist_ids), access_token)

        phases = defaultdict(lambda: {'track_infos': [], 'genres': defaultdict(int)})
        for track_id, track_info in all_tracks_info.items():
            if not track_info.get('added_at'): continue
            dt = datetime.fromisoformat(track_info['added_at'].replace('Z', ''))
            phase_key = _get_season_key(dt)
            if phase_key:
                phases[phase_key]['track_infos'].append(track_info)
                if track_info.get('artist_id') in artist_genres_map:
                    for genre in artist_genres_map[track_info['artist_id']]:
                        phases[phase_key]['genres'][genre] += 1
        
        final_phases_output = []
        used_album_ids = set()
        
        def get_sort_key(phase_key):
            season, year_str = phase_key.split(" ")
            return int(year_str), ["Winter", "Spring", "Summer", "Autumn"].index(season)
        sorted_phases = sorted(phases.items(), key=lambda item: get_sort_key(item[0]))

        for key, data in sorted_phases:
            if not data['track_infos']: continue
            album_stats = defaultdict(lambda: {'count': 0, 'relevance': 0, 'cover': ''})
            for track in data['track_infos']:
                if track.get('album_id'):
                    album_stats[track['album_id']]['count'] += 1
                    album_stats[track['album_id']]['relevance'] += track['relevance_score']
                    album_stats[track['album_id']]['cover'] = track['cover_url']
            
            album_candidates = sorted([{'id': aid, **stats} for aid, stats in album_stats.items()], key=lambda x: (x['count'], x['relevance']), reverse=True)
            cover_url = next((c['cover'] for c in album_candidates if c['id'] not in used_album_ids), 'https://placehold.co/128x128/121212/FFFFFF?text=?')
            if cover_url != 'https://placehold.co/128x128/121212/FFFFFF?text=?': used_album_ids.add(next((c['id'] for c in album_candidates if c['cover'] == cover_url), None))

            sorted_tracks = sorted(data['track_infos'], key=lambda t: t['relevance_score'], reverse=True)
            top_artists = list(dict.fromkeys([t['artist_name'] for t in sorted_tracks]))[:5]
            
            valid_year_tracks = [t['release_year'] for t in data['track_infos'] if t.get('release_year', 0) > 0]
            avg_release_year = round(sum(valid_year_tracks) / len(valid_year_tracks)) if valid_year_tracks else 'N/A'
            avg_popularity = round(sum(t['popularity'] for t in data['track_infos']) / len(data['track_infos'])) if data['track_infos'] else 0
            
            phase_chars = {"period": key, "top_genres": sorted(data['genres'], key=data['genres'].get, reverse=True)[:5], "avg_release_year": avg_release_year, "avg_popularity": avg_popularity}
            # Reduced sleep time as the process is much faster now
            time.sleep(2) 
            ai_details = _get_ai_phase_details(phase_chars, top_artists)

            final_phases_output.append({
                'phase_period': key, 
                'ai_phase_name': ai_details.get('phase_name', f"Your {key} Era"), 
                'ai_phase_summary': ai_details.get('phase_summary', "A distinct period in your listening journey."),
                'track_count': len(data['track_infos']), 
                'top_genres': phase_chars['top_genres'],
                'average_popularity': avg_popularity, 
                'average_release_year': avg_release_year,
                'sample_tracks': [t['name'] for t in sorted_tracks[:5]], 
                'phase_cover_url': cover_url
            })
        
        final_phases_output.reverse()
        display_name = session.get('display_name', 'friend')
        return render_template('timeline.html', phases=final_phases_output, display_name=display_name)

    except requests.exceptions.RequestException as e:
        logging.error(f"An error occurred during analysis: {e}")
        return render_template('login.html', error="An error occurred during analysis. Your session might have expired. Please log in again.")

# --- Application Runner ---
if __name__ == '__main__':
    app.run(debug=True, port=5000)
