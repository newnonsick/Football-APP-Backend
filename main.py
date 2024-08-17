import requests
import flask
import threading
import datetime
import pytz
import time
from flask_socketio import SocketIO
from flask_cors import CORS
import copy
import firebase_admin
from firebase_admin import credentials, messaging, firestore
from werkzeug.exceptions import HTTPException
from dotenv import load_dotenv
import os

load_dotenv()

API_TOKEN = os.getenv('API_TOKEN')
FIREBASE_CONFIG = {
    "type": os.getenv("FIREBASE_TYPE"),
    "project_id": os.getenv("FIREBASE_PROJECT_ID"),
    "private_key_id": os.getenv("FIREBASE_PRIVATE_KEY_ID"),
    "private_key": os.getenv("FIREBASE_PRIVATE_KEY").replace('\\n', '\n'),
    "client_email": os.getenv("FIREBASE_CLIENT_EMAIL"),
    "client_id": os.getenv("FIREBASE_CLIENT_ID"),
    "auth_uri": os.getenv("FIREBASE_AUTH_URI"),
    "token_uri": os.getenv("FIREBASE_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.getenv("FIREBASE_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.getenv("FIREBASE_CLIENT_X509_CERT_URL"),
    "universe_domain": os.getenv("FIREBASE_UNIVERSE_DOMAIN")
}

cred = credentials.Certificate(FIREBASE_CONFIG)
firebase_admin.initialize_app(cred)
db = firestore.client()

app = flask.Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

utc_tz = pytz.timezone('UTC')
yesterday = datetime.datetime.now(utc_tz) - datetime.timedelta(days=1)
aweek = datetime.datetime.now(utc_tz) + datetime.timedelta(days=7)

data_lock = threading.Lock()
data = {
    'old_response_data': {},
    'live_matches': [],
    'upcoming_matches': []
}

table_lock = threading.Lock()
table = {}

top_scorers_lock = threading.Lock()
top_scorers_data = {
    'check': {},
    'top_scorers': {}
}

teams_lock = threading.Lock()
teams = {}


def get_user_fcm_tokens(user_id):
    data = db.collection('users').where('uid', '==', user_id).get()
    return data[0].to_dict().get('fcmTokens', None) if data else None

def send_notification(title, body, token):
    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token
        )
        messaging.send(message)
    except Exception as e:
        print(f"Error in send_notification: {e}")

def send_notifications(title, body, match_id):
    try:
        data = db.collection('followedMatch').where('matchId', '==', match_id).get()
        for doc in data:
            tokens = get_user_fcm_tokens(doc.to_dict()['uid'])
            if tokens:
                for token in tokens:
                    threading.Thread(target=send_notification, args=(title, body, token)).start()

    except Exception as e:
        print(f"Error in send_notifications: {e}")

def send_coin_to_correct_guesses(match_id, choice, result):
    try:
        data = db.collection('guesses').where('matchId', '==', match_id).where('choice', '==', choice).where('status', '==', 'active').get()
        for doc in data:
            doc_data = doc.to_dict()
            amount = doc_data.get('amount', 0) * 2
            user_ref = db.collection('users').where('uid', '==', doc_data['uid']).get()[0].reference
            if doc_data['result'] == result:
                user_ref.update({
                    'coin': firestore.Increment(amount),
                    'guessedCorrect': firestore.Increment(1),
                    'correctStreak': firestore.Increment(1)
                })
                socketio.emit('update_coin', {'uid': doc_data['uid'], 'amount': amount})
            else:
                user_ref.update({
                    'guessedWrong': firestore.Increment(1),
                    'correctStreak': 0
                })
            db.collection('guesses').document(doc.id).update({'status': 'finished'})
    except Exception as e:
        print(f"Error in send_coin_to_correct_guesses: {e}")

def create_document(collection, data):
    doc_ref = db.collection(collection).document()
    doc_ref.set(data)
    return doc_ref.id

def check_match_update(match):
    match['score']['fullTime']['home'] = match['score']['fullTime']['home'] or 0
    match['score']['fullTime']['away'] = match['score']['fullTime']['away'] or 0
    
    match_ref = db.collection('matches').where('id', '==', match['id']).get()
    if not match_ref:
        data = {
            'id': match['id'],
            'status': match['status'],
            'homeScore': match['score']['fullTime']['home'],
            'awayScore': match['score']['fullTime']['away'],
            'homeTeamId': match['homeTeam']['id'],
            'awayTeamId': match['awayTeam']['id']
        }
        create_document('matches', data)
        add_followed_match(match)
    else:
        update_existing_match(match, match_ref[0])

def add_followed_match(match):
    for team_id in [match['homeTeam']['id'], match['awayTeam']['id']]:
        users = db.collection('favoritedTeam').where('teamId', '==', team_id).get()
        for user in users:
            user_data = user.to_dict()
            data = {
                'uid': user_data['uid'],
                'matchId': match['id'],
                'byUser': False,
                'homeTeamId': match['homeTeam']['id'],
                'awayTeamId': match['awayTeam']['id']
            }
            if not db.collection('followedMatch').where('uid', '==', user_data['uid']).where('matchId', '==', match['id']).get():
                create_document('followedMatch', data)

def update_existing_match(match, match_ref):
    data_dict = match_ref.to_dict()
    if data_dict['status'] != match['status']:
        handle_status_change(match, data_dict)
    
    if data_dict['homeScore'] != match['score']['fullTime']['home'] or data_dict['awayScore'] != match['score']['fullTime']['away']:
        handle_score_change(match, data_dict)

    data_dict['status'] = match['status']
    data_dict['homeScore'] = match['score']['fullTime']['home']
    data_dict['awayScore'] = match['score']['fullTime']['away']
    db.collection('matches').document(match_ref.id).update(data_dict)

def handle_status_change(match, data_dict):
    if match['status'] == 'FINISHED':
        socketio.emit(match['id'], match)
        send_notifications('Match Finished', f"{match['homeTeam']['shortName']} {match['score']['fullTime']['home']} - {match['score']['fullTime']['away']} {match['awayTeam']['shortName']}", match['id'])
        winner = 'home' if match['score']['fullTime']['home'] > match['score']['fullTime']['away'] else 'away' if match['score']['fullTime']['home'] < match['score']['fullTime']['away'] else 'draw'
        send_coin_to_correct_guesses(match['id'], 'fulltime', winner)
    elif match['status'] == 'IN_PLAY' and data_dict['status'] == 'TIMED':
        socketio.emit(match['id'], match)
        send_notifications('Match Started', f"{match['homeTeam']['shortName']} vs {match['awayTeam']['shortName']}", match['id'])
    elif match['status'] == 'PAUSED':
        winner = 'home' if match['score']['fullTime']['home'] > match['score']['fullTime']['away'] else 'away' if match['score']['fullTime']['home'] < match['score']['fullTime']['away'] else 'draw'
        send_coin_to_correct_guesses(match['id'], 'hafttime', winner)

def handle_score_change(match, data_dict):
    if match['score']['fullTime']['home'] == 0 and match['score']['fullTime']['away'] == 0:
        return
    socketio.emit(match['id'], match)
    send_notifications('Goal', f"{match['homeTeam']['shortName']} {match['score']['fullTime']['home']} - {match['score']['fullTime']['away']} {match['awayTeam']['shortName']}", match['id'])

def update_data():
    global data, yesterday, aweek, utc_tz
    while True:
        try:
            utc_tz = pytz.timezone('UTC')
            yesterday = datetime.datetime.now(utc_tz) - datetime.timedelta(days=1)
            aweek = datetime.datetime.now(utc_tz) + datetime.timedelta(days=7)

            url = "https://api.football-data.org/v4/competitions/PL/matches"
            headers = {'X-Auth-Token': API_TOKEN}
            response = requests.get(url, headers=headers)
            response_data = response.json()
            
            for match in response_data['matches']:
                match['lastUpdated'] = 'null'

            if response_data != data['old_response_data']:
                with data_lock:
                    update_matches(response_data)

            time.sleep(10)
        except Exception as e:
            print(f"Error in update_data thread: {e}")

def update_matches(response_data):
    new_live_matches = []
    new_upcoming_matches = []
    for match in response_data['matches']:
        match_date = datetime.datetime.strptime(match['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
        if yesterday.date() <= match_date.date() <= aweek.date():
            if match['status'] in ['SCHEDULED', 'TIMED', 'FINISHED']:
                new_upcoming_matches.append(match)
            elif match['status'] in ['LIVE', 'IN_PLAY', 'PAUSED']:
                new_live_matches.append(match)
        
        if match_date.date() > aweek.date():
            update_match_lists(new_live_matches, new_upcoming_matches)
            break

    if not new_upcoming_matches:
        handle_empty_upcoming_matches(response_data)

    data['old_response_data'] = copy.deepcopy(response_data)
    print("All matches updated")

def update_match_lists(new_live_matches, new_upcoming_matches):
    if new_live_matches != data['live_matches']:
        data['live_matches'] = new_live_matches
        for match in data['live_matches']:
            check_match_update(copy.deepcopy(match))
        socketio.emit('update_live_matches', data['live_matches'])
        print("Live matches updated")

    if new_upcoming_matches != data['upcoming_matches']:
        data['upcoming_matches'] = new_upcoming_matches
        for match in data['upcoming_matches']:
            check_match_update(copy.deepcopy(match))
        socketio.emit('update_upcoming_matches', data['upcoming_matches'])
        print("Upcoming matches updated")

def handle_empty_upcoming_matches(response_data):
    first_match_date = datetime.datetime.strptime(response_data['matches'][0]['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
    if first_match_date.date() < yesterday.date():
        new_upcoming_matches = response_data['matches'][-10:]
    else:
        new_upcoming_matches = response_data['matches'][:10]
    
    data['upcoming_matches'] = new_upcoming_matches
    for match in data['upcoming_matches']:
        check_match_update(copy.deepcopy(match))
    socketio.emit('update_upcoming_matches', data['upcoming_matches'])
    print("Upcoming matches updated")

def update_table():
    global table
    while True:
        try:
            url = "https://api.football-data.org/v4/competitions/PL/standings"
            headers = {'X-Auth-Token': API_TOKEN}
            response = requests.get(url, headers=headers)
            response_data = response.json()
            if response_data != table:
                with table_lock:
                    table = response_data
                    socketio.emit('update_table', table)
                    print("Table updated")
            time.sleep(60)
        except Exception as e:
            print(f"Error in update_table thread: {e}")

def get_more_info(player_name):
    url = f"https://footballapi.pulselive.com/search/PremierLeague/?terms={player_name},{player_name}*&type=player&size=10&start=0&fullObjectResponse=true"
    headers = {
        'authority': 'footballapi.pulselive.com',
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.5',
        'content-type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'origin': 'https://www.premierleague.com',
        'referer': 'https://www.premierleague.com/',
        'sec-ch-ua': '"Not A(Brand";v="99", "Brave";v="121", "Chromium";v="121"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'cross-site',
        'sec-gpc': '1',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36'
    }
    response = requests.get(url, headers=headers)
    response_data = response.json()
    if response_data['hits']['found'] != 0:
        for hit in response_data['hits']['hit']:
            if hit['response']['name']['display'] == player_name:
                return hit['response']
    return response_data['hits']['hit'][0]['response']

def update_top_scorers():
    global top_scorers_data
    while True:
        try:
            url = "https://api.football-data.org/v4/competitions/PL/scorers"
            headers = {'X-Auth-Token': API_TOKEN}
            response = requests.get(url, headers=headers)
            response_data = response.json()
            for scorer in response_data['scorers']:
                scorer['player']['lastUpdated'] = 'null'
                scorer['team']['lastUpdated'] = 'null'

            if response_data != top_scorers_data['check']:
                with top_scorers_lock:
                    top_scorers_data['check'] = copy.deepcopy(response_data)
                    top_scorers_data['top_scorers'] = copy.deepcopy(response_data)
                    for scorer in top_scorers_data['top_scorers']['scorers']:
                        scorer['moreInfo'] = get_more_info(scorer['player']['name'])
                    socketio.emit('update_top_scorers', top_scorers_data['top_scorers'])
                    print("Top scorers updated")
            time.sleep(60)
        except Exception as e:
            print(f"Error in update_top_scorers thread: {e}")

def update_teams():
    global teams
    while True:
        try:
            url = "https://api.football-data.org/v4/competitions/PL/teams"
            headers = {'X-Auth-Token': API_TOKEN}
            response = requests.get(url, headers=headers)
            response_data = response.json()

            for team in response_data['teams']:
                team['lastUpdated'] = 'null'

            if response_data != teams:
                with teams_lock:
                    teams = response_data
                    socketio.emit('update_all_teams', teams)
                    print("Teams updated")
            time.sleep(60)
        except Exception as e:
            print(f"Error in update_teams thread: {e}")

@app.errorhandler(HTTPException)
def handle_http_exception(error):
    response = error.get_response()
    response.data = flask.json.dumps({
        "code": error.code,
        "name": error.name,
        "description": error.description,
    })
    response.content_type = "application/json"
    return response

@app.errorhandler(Exception)
def handle_exception(error):
    response = flask.jsonify({"error": "Internal Server Error"})
    response.status_code = 500
    return response

@app.route('/')
async def index():
    return 'Hello, World!'

@app.route('/getAllMatches', methods=['GET'])
async def get_all_matches():
    with data_lock:
        return flask.jsonify(data['old_response_data'])

@app.route('/getLiveMatches', methods=['GET'])
async def get_live_matches():
    with data_lock:
        return flask.jsonify(data['live_matches'])

@app.route('/getUpcomingMatches', methods=['GET'])
async def get_upcoming_matches():
    with data_lock:
        return flask.jsonify(data['upcoming_matches'])

@app.route('/getMatch/<day>', methods=['GET'])
async def get_match(day):
    timezone_offset_str = flask.request.args.get('timezone', '0:00:00').split('.')[0]
    try:
        hours, minutes, seconds = map(int, timezone_offset_str.split(':'))
        timezone_offset = datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds)
    except ValueError:
        return flask.jsonify({'error': 'Invalid timezone format. Please use HH:MM:SS.'}), 400
    
    try:
        day_datetime = datetime.datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return flask.jsonify({'error': 'Invalid date format. Please use YYYY-MM-DD.'}), 400
    
    with data_lock:
        matches = [
            match for match in data['old_response_data']['matches']
            if (datetime.datetime.strptime(match['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC) + timezone_offset).date() == day_datetime.date()
        ]
        return flask.jsonify(matches)

@app.route('/getMatchById', methods=['POST'])
async def get_match_by_id():
    list_match_id = flask.request.json['list_match_id']
    list_match = [
        match for match in data['old_response_data']['matches']
        if match['id'] in list_match_id
    ]
    return flask.jsonify(list_match)

@app.route('/getStandings', methods=['GET'])
async def get_standings():
    with table_lock:
        return flask.jsonify(table)

@app.route('/getTopScorers', methods=['GET'])
async def get_top_scorers():
    with top_scorers_lock:
        return flask.jsonify(top_scorers_data['top_scorers'])

@app.route('/getAllTeams', methods=['GET'])
async def get_teams():
    with teams_lock:
        return flask.jsonify(teams)

@socketio.on('message')
async def handle_message(message):
    print(f'received message: {message}')

if __name__ == '__main__':
    threads = [
        threading.Thread(target=update_data, daemon=True),
        threading.Thread(target=update_table, daemon=True),
        threading.Thread(target=update_top_scorers, daemon=True),
        threading.Thread(target=update_teams, daemon=True)
    ]
    for thread in threads:
        thread.start()

    socketio.run(app, host='0.0.0.0', port=2734, allow_unsafe_werkzeug=True)