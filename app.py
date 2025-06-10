import os
import requests
from flask import Flask, redirect, request, session, jsonify, url_for, render_template
from dotenv import load_dotenv
from urllib.parse import urlencode
import logging
from datetime import datetime
from collections import defaultdict
import json

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

def _get_season_key(dt):
    month = dt.month
    year = dt.year
    if month in (1, 2): return f"Winter {year - 1}"
    if month in (3, 4, 5): return f"Spring {year}"
    if month in (6, 7, 8): return f"Summer {year}"
    if month in (9, 10, 11): return f"Autumn {year}"
    if month == 12: return f"Winter {year}"

def _get_ai_phase_details(phase_characteristics):
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    fallback_response = {"phase_name": f"Your {phase_characteristics['period']} Era", "phase_summary": "A distinct period in your listening journey."}
    if not gemini_api_key: return fallback_response
    
    gemini_api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_api_key}"
    prompt = f"""
You are a creative music journalist. Based on the following data about a person's music phase, generate two things:
1. A cool, evocative "Daylist-style" name for the phase (3-5 words, no numbers).
2. A short, personal, one-paragraph summary describing the vibe of this era.
**Phase Data:**
- **Period:** {phase_characteristics['period']}
- **Top Genres:** {', '.join(phase_characteristics['top_genres'])}
- **Era Vibe:** {'Modern mainstream' if phase_characteristics['avg_release_year'] > 2010 else 'Nostalgic throwback'}
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
    if 'access_token' in session:
        return redirect(url_for('timeline'))
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
        
        return redirect(url_for('timeline'))
    return jsonify({"error": "Unknown callback error"})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/timeline')
def timeline():
    """Renders the timeline page with skeleton loaders."""
    if 'access_token' not in session: return redirect('/login')
    display_name = session.get('display_name', 'friend')
    return render_template('timeline.html', display_name=display_name)


# ===================================================================
# NEW ASYNCHRONOUS ANALYSIS API
# ===================================================================

@app.route('/api/get_initial_phases')
def get_initial_phases():
    """
    NEW: Fast endpoint that fetches Spotify data and calculates phases WITHOUT AI.
    """
    access_token = session.get('access_token')
    if not access_token: return jsonify({"error": "Not authenticated"}), 401
    
    try:
        saved_tracks_items = _get_all_pages('me/tracks?limit=50', access_token)
        
        relevance_scores = defaultdict(int)
        for item in saved_tracks_items:
            if item.get('track'): relevance_scores[item['track']['id']] += 1
        
        all_tracks_info = {}
        for item in saved_tracks_items:
            track = item.get('track')
            if not track or not track.get('id') or track['id'] in all_tracks_info: continue
            album = track.get('album', {})
            images = album.get('images', [])
            all_tracks_info[track['id']] = {'name': track.get('name', 'N/A'), 'artist_name': track['artists'][0]['name'] if track.get('artists') else 'N/A', 'popularity': track.get('popularity', 0), 'release_year': int(album.get('release_date', '0').split('-')[0]), 'added_at': item.get('added_at'), 'relevance_score': relevance_scores.get(track['id'], 0), 'cover_url': images[0]['url'] if images else 'https://placehold.co/128x128/121212/FFFFFF?text=?', 'genres': []}
        
        phases = defaultdict(list)
        for track_id, info in all_tracks_info.items():
            if not info.get('added_at'): continue
            dt = datetime.fromisoformat(info['added_at'].replace('Z', ''))
            key = _get_season_key(dt)
            if key: phases[key].append(info)
        
        def get_sort_key(phase_key):
            season, year_str = phase_key.split(" ")
            return int(year_str), ["Winter", "Spring", "Summer", "Autumn"].index(season)
        
        final_phases_output = []
        for key in sorted(phases.keys(), key=get_sort_key, reverse=True):
            phase_tracks = phases[key]
            if not phase_tracks: continue

            sorted_tracks = sorted(phase_tracks, key=lambda t: t['relevance_score'], reverse=True)
            
            final_phases_output.append({
                'phase_period': key,
                'track_count': len(phase_tracks),
                'sample_tracks': [t['name'] for t in sorted_tracks[:5]],
                'cover_url': sorted_tracks[0]['cover_url'] if sorted_tracks else 'https://placehold.co/128x128/121212/FFFFFF?text=?'
            })
            
        return jsonify(final_phases_output)

    except Exception as e:
        logging.error(f"Error in get_initial_phases: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/get_ai_details_for_phase', methods=['POST'])
def get_ai_details_for_phase():
    """
    NEW: Endpoint that gets AI details for only ONE phase.
    """
    access_token = session.get('access_token')
    if not access_token: return jsonify({"error": "Not authenticated"}), 401
    
    try:
        phase_tracks = request.json['tracks']
        phase_period = request.json['period']
        
        # Calculate stats from the provided track data
        all_artist_ids = {t['artist_id'] for t in phase_tracks if t.get('artist_id')}
        genres_map = _get_artist_genres(list(all_artist_ids), access_token)
        
        genres_count = defaultdict(int)
        for track in phase_tracks:
            if track.get('artist_id') in genres_map:
                for genre in genres_map[track['artist_id']]:
                    genres_count[genre] += 1
                    
        top_genres = sorted(genres_count, key=genres_count.get, reverse=True)[:5]
        avg_pop = round(sum(t['popularity'] for t in phase_tracks) / len(phase_tracks)) if phase_tracks else 0
        valid_years = [t['release_year'] for t in phase_tracks if t.get('release_year', 0) > 0]
        avg_year = round(sum(valid_years) / len(valid_years)) if valid_years else 'N/A'

        phase_chars = {"period": phase_period, "top_genres": top_genres, "avg_release_year": avg_year, "avg_popularity": avg_pop}
        
        ai_details = _get_ai_phase_details(phase_chars)

        return jsonify({
            **ai_details,
            'top_genres': top_genres,
            'average_popularity': avg_pop,
            'average_release_year': avg_year
        })
    except Exception as e:
        logging.error(f"Error in get_ai_details_for_phase: {e}")
        return jsonify({"error": str(e)}), 500


# --- Application Runner ---
if __name__ == '__main__':
    app.run(debug=True, port=5000)
