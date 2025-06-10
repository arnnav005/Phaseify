import os
import requests
from flask import Flask, redirect, request, session, jsonify, url_for, render_template
from dotenv import load_dotenv
from urllib.parse import urlencode
import logging
from datetime import datetime
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
You are a creative music journalist. Based on the following data about a person's music phase, generate two things: a cool, evocative "Daylist-style" name (3-5 words, no numbers), and a short, one-paragraph summary describing the vibe.
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
    if 'access_token' in session and 'user_id' in session: return redirect(url_for('loading'))
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
        session['access_token'] = res.json().get('access_token')
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
    if 'final_phases' not in session: return redirect(url_for('loading'))
    return render_template('timeline.html', phases=session['final_phases'], display_name=session.get('display_name', 'friend'))

# ===================================================================
# NEW ASYNCHRONOUS ANALYSIS API
# ===================================================================

@app.route('/api/start_analysis')
def start_analysis():
    access_token = session.get('access_token')
    if not access_token: return jsonify({"status": "error", "error": "Not logged in"}), 401
    
    try:
        logging.info("API: Fetching raw data...")
        saved_tracks = _get_all_pages('me/tracks?limit=50', access_token)
        all_tracks_info = {}
        for item in saved_tracks:
            track = item.get('track')
            if not track or not track.get('id'): continue
            album = track.get('album', {})
            images = album.get('images', [])
            all_tracks_info[track['id']] = {
                'name': track.get('name', 'N/A'),
                'added_at': item.get('added_at'),
                'cover_url': images[0]['url'] if images else 'https://placehold.co/128x128/121212/FFFFFF?text=?'
            }
        
        phases = defaultdict(list)
        for track_id, info in all_tracks_info.items():
            if not info.get('added_at'): continue
            dt = datetime.fromisoformat(info['added_at'].replace('Z', ''))
            key = _get_season_key(dt)
            if key:
                phases[key].append(all_tracks_info[track_id])

        def get_sort_key(phase_key):
            season, year_str = phase_key.split(" ")
            return int(year_str), ["Winter", "Spring", "Summer", "Autumn"].index(season)
        
        sorted_phase_keys = sorted(phases.keys(), key=get_sort_key, reverse=True)
        session['phase_data_to_process'] = {key: phases[key] for key in sorted_phase_keys}

        return jsonify({"status": "success", "phases": sorted_phase_keys})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/api/process_phase', methods=['POST'])
def process_phase():
    access_token = session.get('access_token')
    phase_key = request.json['phase_key']
    phase_tracks = session.get('phase_data_to_process', {}).get(phase_key)
    
    if not access_token or not phase_tracks:
        return jsonify({"status": "error", "error": "Missing data or not logged in"}), 400

    try:
        # This function can be expanded later to fetch more details if needed
        # For now, we are just using the name and cover from the initial fetch
        sample_tracks = [t['name'] for t in phase_tracks[:5]]
        cover_url = phase_tracks[0]['cover_url'] if phase_tracks else 'https://placehold.co/128x128/121212/FFFFFF?text=?'
        
        # This example uses placeholder stats. A real implementation would fetch these.
        ai_details = {
            'phase_name': f"Your {phase_key} Era",
            'phase_summary': "A summary of your listening during this period."
        }
        
        phase_output = {
            'phase_period': phase_key,
            'ai_phase_name': ai_details.get('phase_name'),
            'ai_phase_summary': ai_details.get('phase_summary'),
            'track_count': len(phase_tracks),
            'top_genres': ["Genre 1", "Genre 2"], # Placeholder
            'average_popularity': 50, # Placeholder
            'average_release_year': 2020, # Placeholder
            'sample_tracks': sample_tracks,
            'phase_cover_url': cover_url
        }
        
        # Store processed phase in a temporary list in session
        if 'final_phases' not in session:
            session['final_phases'] = []
        
        current_phases = session['final_phases']
        current_phases.append(phase_output)
        session['final_phases'] = current_phases

        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# --- Application Runner ---
if __name__ == '__main__':
    app.run(debug=True, port=5000)
