"""BusTrack: login, trip planning, and live bus positions for real Kolkata (WBTC/CTC) bus routes.

Data notes (please read):
- Route numbers, depots and stop sequences below come from the WBTC/Calcutta Tramways
  public bus-route page (calcuttatramways.com/bus-route). That page does NOT publish
  first-bus/last-bus times or frequencies for any route -- those columns are blank on
  the source itself -- so there is no official timetable to show. Frequencies used here
  are simulation defaults, clearly not "the real schedule".
- Stop coordinates are NOT hand-entered. On startup this server calls the Google
  Geocoding API once per unique stop name (results are cached in SQLite so you only
  pay/wait for this once) and builds each route from whichever stops geocode
  successfully. Some of the more informal stop names ("More", "Xing", "Bus Stand" etc.)
  may geocode inaccurately or fail outright -- see GET /api/geocode-status after startup
  for a report, and edit STOP_NAME_OVERRIDES below to fix specific names.
- Bus movement is still simulated (WBTC does not publish live GPS). Buses drive out
  and back along the geocoded stop sequence at a simulated speed.

Run:  uvicorn main:app --reload      then open http://127.0.0.1:8000
"""
import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import smtplib
import time
from email.message import EmailMessage
from bisect import bisect_right
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH") or BASE / "bustrack.db")
# Pre-geocoded stop coordinates shipped with the code (made by export_geocode.py), so a fresh
# deploy starts instantly instead of re-geocoding ~680 stops with the Google API.
SEED_PATH = BASE / "geocode_seed.json"
SESSION_DAYS = 7
TICK_SECONDS = 1.0
# Buses in this demo drive DEMO_SPEEDUP times faster than DEFAULT_KMH so movement is
# visible. Set to 1 for real-time behaviour.
DEMO_SPEEDUP = 8
DEFAULT_KMH = 20
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# Password reset
RESET_TOKEN_MINUTES = 30
RESET_MAX_REQUESTS = 3          # per email, per RESET_WINDOW_SECONDS
RESET_WINDOW_SECONDS = 900
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
GEOCODE_CONCURRENCY = 8
GEOCODE_TIMEOUT = 10.0
# Bias geocoding to the Kolkata metro area (south-west, north-east corners).
KOLKATA_BOUNDS = "22.30,88.10|22.85,88.55"

# Optional manual fixes for stop names that geocode badly or ambiguously.
# Format: "Stop Name As Listed": "search text to send to Google instead"
STOP_NAME_OVERRIDES: Dict[str, str] = {
    "B. B. D. Bag": "BBD Bagh, Kolkata",
    "P.T.S.": "Park Circus Transit Station, Kolkata",
    "Toll Tax": "Kona Expressway Toll Plaza, Kolkata",
}

# ---------------------------------------------------------------- route data
# (route_number, depot, [stops in order]) - transcribed from the WBTC/CTC bus-route
# page. AC-2 is omitted: the source lists no stops for it.
ROUTES_RAW = [
    ("C-11", "Belgachia Depot", ["Belgachia", "Pharia Pukur", "Khanna", "Maniktala", "Bank of India", "Bowbazar", "Central", "Lalbazar", "B. B. D. Bag", "Esplanade", "Park Street", "Jeevandip", "Birla", "P.T.S.", "Hastings", "Carry Road", "Belepole", "Betor", "Bakshara", "Moukhali", "Garpha (Country Club)", "Kejurtala", "Lalbari", "Bat Tala", "Katliya", "Jhowtala", "Makardah", "B.D.O. Office", "Phokor Dokan", "Domjur P.S.", "Domjur Stand"]),
    ("T-11", "Belgachia Depot", ["Chandni (up)/B.B.D. Bag (dn)", "Central", "Md. Ali Park", "Mahajati Sadan", "Girish Park", "Beadon Street", "Hatibagan", "Khanna", "Lake Town", "Bangur", "Kestopur", "Baguihati", "Haldiram", "Kaikhali", "Gopalpur House", "Kalipark", "Bablatala", "Narayanpur Bat-Tala", "Manikhola", "Beraberi", "Raigachi", "Kazial Para", "Bishnupur", "Lauhati", "Chandpur Bazar", "Natun Hat", "Dhiv-Dhiva Bazar", "Pagla Pir", "Haroa"]),
    ("C-27", "", ["Chandni (up)/B.B.D. Bag (dn)", "Central", "Md. Ali Park", "Mahajati Sadan", "Girish Park", "Maniktala", "Bagmari", "Kakurgachi", "Lake Town", "Kestopur", "Baguihati", "Haldiram", "Kaikhali", "Airport No.1", "Airport No.2", "Airport 2.5 No.", "Birati More", "Michel Nagar", "B.T. College", "Doltala", "New Barrackpore", "Madhyamgram", "Dakbangla", "Hatkhola", "Chanpadali", "Colony More", "Hela Bat-tala", "Kali Bari", "Arip Bari", "Baduria", "Adamas College / ABP Office", "Kazibari"]),
    ("D1", "Tollygunge Depot", ["Golf Club", "Bangar Hospital", "Prince Anwarsha Road", "Navina Cinema", "Lords More", "E.D.F.", "Jadavpur PS", "Santoshpur", "Salimpur", "Ganguly Pukur", "Sarat Collony", "Mandal Para / Battala", "Gitanjali Park", "Purbasa / Kalikapur", "Mandir Para", "Sahid Nagar", "Laskarr Hut / Tagore Park", "Panchanan Gram", "Math Pukur", "Metropolitan", "Chingrighata", "Beleghata", "Kadapara", "Appole Hospital", "Mani Square Mall", "Bengal Chemical", "Purbasha", "Hudco", "Lake Town", "Bangur", "Dum Dum Park", "Kestopur", "Baguihati", "Raghunath Pur", "Haldiram", "Kaikhali", "Airport No.1", "Airport No.2", "Airport 2.5 No.", "Airport No. 3", "Science City", "Birati More", "Bankra", "Michel Nagar", "B.T. College", "Doltala", "New Barrackpore", "Madhyamgram Chowmatha", "Dak Bunglow More", "Rajberia", "College More/Hela Battala", "Mayna Check Post", "Mayana Hut", "Mirbati", "Sapui Para", "Kamdevpur", "Khelia", "Kansari", "Amdanga", "Rahana No. 1", "Rail gate", "Awal Sidhi", "Adha", "Kanchiara Power House", "Gadamara", "Ruby Hospital", "Jagulia", "Kalayani More", "Birahi", "Chakdaha"]),
    ("M-2", "Tollygunge Depot", ["Haldiram", "Bangar Hospital", "Dum Dum Park", "Metropolitan", "Jora Bridge", "8B Bus Stand", "Singha Bari", "Jora Mandir", "Ajoy Nagar", "Raghunath Pur", "Tegharia", "Kestopur", "Science City", "Math Pukur", "Ruby Hospital", "Sulekha Bridge", "Panchanan Gram", "Prince Anwarsha Road", "Dahka Kali Bari", "Beleghata", "Kaikhali", "Appole Hospital", "Mukundapur", "Bengal Chemical", "Santoshpur Lake", "Jadavpur PS", "Lords Bakery", "Bangur", "Golf Club", "Lake Town", "Baguihati", "Kadapara", "Sarat Collony", "Hudco", "Kalikapur", "Airport No.1", "Airport No.2", "Airport 2.5 No.", "Airport No. 3", "South City Mall", "Chingrighata", "Birati Bus Stand"]),
    ("ORD-1", "Khidirpur Depot", ["Howrah Station", "Barabazar", "B. B. D. Bag", "Esplanade", "Hastings", "Ekbalpur", "Mominpur", "Taratala", "Behala Tram Depot", "Blind School", "Behala Chowrasta", "Sakhar Bazar", "Sil Para", "Thakur Pukur Bazar", "3A Bus Stand", "Joka", "11M Kolkata", "Pailan", "Vasa-14", "Khariberia", "Bishnupur", "Amtola"]),
    ("T-8", "Belgachia Depot", ["Esplanade", "Park Street", "Jeevandip", "Birla", "Rabindra Sadan", "P.T.S.", "Hastings", "Toll Tax", "Carry Road", "Belepole", "Natun Rasta", "P.N.T.", "Jal-Tank", "Shanpur", "Tikiapara Stand"]),
    ("E-6", "Rajabazar Depot", ["P.T.S.", "Hastings", "Toll Tax", "Carry Road", "Belepole", "Santragachi Station", "Garpha", "Kona Expressway X-ing", "Ankurhati Check Post", "Swarswati Bridge", "Jalan Gate – 1", "Jalan Gate – 3", "Alampur", "Dhulagarh", "Ranihati", "Beltala", "Bashtala", "Gab Beria Hospital", "Haulibagan", "Mnik Pir", "10 No.", "Chakhana", "Kalatala", "Amta"]),
    ("T-12", "Park Circus Depot", ["Howrah", "Barabazar", "Sealdah", "CIT Road", "Park Circus", "4 No Bridge", "P.C. Connector", "Science City", "Chowbaga", "Banshtola", "Bamunghata", "Kantalata", "Leather Complex", "Karai Danga", "Ghuri Ghata", "Bamanpukur", "Minakhan", "Malancha", "Chaital Ghat"]),
    ("C-23", "Park Circus Depot", ["Park Circus", "Topsia", "Science City", "Metropolitan", "Chingrighata", "Nicco Park", "SDF / Sec V", "College More", "Technopolis", "DLF Bldg I", "New Town", "Najrul Tirtha", "Techno India College", "Tata Hospital", "DPS School", "Jatragachi", "Akansha Xing", "City Centre 2", "Chinar Park", "Kaikhali", "Airport No.1", "Belghoria Exp. Way", "Durga Nagar", "Dankuni"]),
    ("T-2", "Khidirpur Depot", ["Mandirtola", "Toll Tax", "Hastings", "P.T.S.", "Rabindra Sadan", "Birla", "Jeevandip", "Park Street", "Mayo Road (ESPL)", "Eastern Hotel", "B. B. D. Bag"]),
    ("T-AC-1", "Khidirpur Depot", ["Esplanade", "Park Street", "Jeevandip", "Birla", "Rabindra Sadan", "P.T.S.", "Hastings", "Toll Tax", "Carry Road", "Belepole", "Betor", "Bakshara", "Santragachi"]),
    ("C-20", "Khidirpur Depot", ["Esplanade", "Park Street", "Jeevandip", "Birla", "Rabindra Sadan", "P.T.S.", "Hastings", "Toll Tax", "Carry Road", "Belepole", "Betor", "Bakshara", "Santragachi"]),
    ("C-1-A", "Khidirpur Depot", ["Amtola Phari", "College Ghat", "Taltola", "Esplanade", "N.R.S. Hospital", "Park Street", "Birla", "Hastings", "P.T.S.", "Danesh Sekh Lane", "Chandni Chawk", "Toll Tax", "Sealdah", "Jeevandip", "Moulali", "Raja Bazar Depot", "Rabindra Sadan"]),
    ("C-26", "Ghasbagan Depot", ["Howrah", "Barabazar", "Dalhouse (T. Board)", "Great Eastern", "Esplanade", "Chandni Chawk", "Wellington", "Taltola", "Philips", "Moulali", "Ladies Park", "Padma Pukur", "Chittaranjan Hospital", "4 No Bridge", "Hindu Gorosthan", "Topsia", "Milan Mela Prangan", "Science City", "Uttar Panchanya Gram", "Panchanya Gram", "VIP Bazar", "Tagore Park", "Mono Bikas", "Ruby Hospital", "Mandir Para", "Dxatikapur", "Metro Bazar", "Mukuratapur", "Ajoy Nagar", "Juba Tirtha", "Saha Para", "Pepsi", "Kumrophali", "Kamalgachi Mazar", "Kamalgachi More", "Narendrapur Mandir", "Narendrapur Mission", "Rathtala", "Kalitala", "Rajpur Bazar", "Rajpur Police Station", "Rajpur Burning Ghat", "Chowhati", "Harinavi", "Haraharitala", "Atlas", "Kodalia Battala", "Malancha", "Benor Chand", "Gobindapur", "Jogi Battala", "Baruipur Bridge", "Sibani Pith"]),
    ("C-24", "Ghasbagan Depot", ["Raja Bazar Depot", "E.S.I. Hospital", "Jagat Cinema", "Sealdah Station", "Purabi Cinema", "Amherst Street", "Shradhya Nanda Park", "Bank of India", "Bowbazar", "Central Avenue", "Ganguram", "Bata", "Bentink Street", "Lalbazar", "Telephone Bhavan", "G.P.O.", "Kaylaghat Street", "Fairli Place", "Clive ghat", "Canning Street", "Howrah Station", "A.C. Market", "Ghasbagan Bus Depot"]),
    ("C-11/1", "Ghasbagan Depot", ["Howrah Station", "Howrah Maidan", "Annapurna Club", "Kadamtala Power House", "Kadamtala Bazar", "Ichapur Water Tank (Sunpur More)", "Dasnagar Station", "Baltikuri Government Quarter", "Japani Gate E.S.I Hospital", "Bankra Bazar", "Munshidangar More", "National Highway", "Salap Bazar", "Salap School", "Kantalia Station", "Kantalia Jhawtala", "Makardaha More", "Domjur B.D.O.", "Domjur P.S.", "Dakshini Bari", "No 2 Pole (Badampur)", "Molla Para", "Santoshpur", "Middya Para", "Bargachia Dharmotala", "Bargachia Hantal More", "Bargachia Station", "Bargachia Level Crossing", "Patihal Hattala", "Patihal Station", "Jadupir More", "Sibanandabati", "Munshirhat Station", "Murshidabad"]),
    ("C-8-A", "Tollygunge Depot", ["Golf Club", "Bangar Hospital", "Prince Anwarsha Road", "Tollygunge Phari", "Bhabani Cinema", "Charu Market", "Modially", "Tollygunge PS", "Rasbehari", "Lake Market", "Deshpriya Park", "Lake view", "Triangular Park", "Hindusthan Park", "Basanti Devi College", "Gariahat", "Ballygunge", "Kasba Post Office", "Kasba Old PS", "Bakultala", "Talbagan BPS", "Bosepukur", "Tribarna", "Nawapally", "Kasba New Market", "Siemens", "Narkel Bagan", "Ruby Hospital", "Laskarr Hut / Tagore Park", "VIP Bazar", "Panchanan Gram", "Uttar Panchanan", "Science City", "Math Pukur", "Metropolitan", "Chingrighata", "Sukanta Nagar", "Lohar Pole", "Nicco Park", "Sastha Bhawan", "Ashram", "S.D.F. Building", "Sector V", "College More", "R.S. Building", "Technopolis", "Mahish Bathan", "Eastern Club", "D.L.F.", "New Town", "Axis Bank", "Hela Building", "Narkel Bagan / Rabindra Tirtha", "Yatra Gachi", "Police Phari", "E.C.O. Park", "Goal Building", "Nabab Pur", "Akhanka", "City Certer", "Noa Para", "Chinar Park", "Charnakya City", "Haldiram", "Kaikhali", "Hatkhola / Dhakin Para", "Seth Pukur", "Chapadali More", "Barasat"]),
    ("C-2-A", "Tollygunge Depot", ["Golf Club", "Bangar Hospital", "Prince Anwarsha Road", "Tollygunge Phari", "Bhabani Cinema", "Charu Market", "Modially", "Tollygunge PS", "Rasbehari", "Lake Market", "Deshpriya Park", "Lake view", "Triangular Park", "Hindusthan Park", "Basanti Devi College", "Gariahat", "Ballygunge", "Kasba Post Office", "Kasba Old PS", "Bakultala", "Talbagan BPS", "Bosepukur", "Tribarna", "Nawapally", "Kasba New Market", "Siemens", "Narkel Bagan", "Ruby Hospital", "Laskarr Hut / Tagore Park", "VIP Bazar", "Panchanan Gram", "Uttar Panchanan", "Science City", "VIP Bridge", "Pakka Pole", "Paschim Chowbaga", "Chowbaga", "Bantal", "B.I.T. College", "Jal Path", "Bamanghata", "Kantalata", "Leather Complex", "Leather Complex 2", "Karai Danga", "Leather Complex 3", "Bhojerhat", "Bairam Pur", "Paglahut", "Baligadha", "Bibir Ait", "Abacus", "Padma Pukur", "Boralighat", "Nalmuri", "B.D.O. Office", "Ghatak Pukur"]),
    ("C-14/1", "Tollygunge Depot", ["Golf Club", "Bangar Hospital", "Prince Anwarsha Road", "Tollygunge Phari", "Bhabani Cinema", "Charu Market", "Modially", "Tollygunge PS", "Rasbehari", "Lake Market", "Deshpriya Park", "Lake view", "Triangular Park", "Hindusthan Park", "Basanti Devi College", "Gariahat", "Ballygunge", "Kasba Post Office", "Kasba Old PS", "Bakultala", "Talbagan BPS", "Bosepukur", "Tribarna", "Nawapally", "Kasba New Market", "Siemens", "Narkel Bagan", "Ruby Hospital", "Laskarr Hut / Tagore Park", "VIP Bazar", "Panchanan Gram", "Uttar Panchanan", "Science City", "Math Pukur", "Metropolitan", "Chingrighata", "Beleghata", "Kadapara", "Appole Hospital", "Mani Square Mall", "Bengal Chemical", "Purbasha", "Hudco", "Ultadanga Bridge", "Lake Town", "Bangur", "Dum Dum Park", "Kestopur", "Baguihati", "Joramandir", "Raghunath Pur", "Tegharia", "Haldiram", "Kaikhali", "Airport No.1", "Airport No.2", "Airport 2.5 No.", "Airport No. 3", "Sarat Collony", "Birati More", "Bankra", "Michel Nagar", "Sukanti Nagar", "B.T. College", "Ghosh Para", "Kata Khel", "Ganganagar", "Furtune City", "Doltala", "New Barrackpore", "Madhyamgram Chowmatha", "Madhyamgram Station"]),
    ("C-28", "Ghasbagan Depot", ["Fly Over BDG", "Satyanarayan Park", "Chitpur Xing", "Mechua", "M.G. Road / C.R. Avenue Xing", "Mahajyoti Sadan", "Vivekananda Road (Girish Park)", "Beadon Street", "Sovabazar", "Rajballav Para", "Shyambazar", "Tala Post Office", "Paik Para", "Chiria More", "Rabindra Bharati", "Sinthee More", "Pal Para", "Tobin Road", "Boonhooghly (ISI)", "Dunlop", "I. A. Bus Stand", "Rathtala", "Kamarhati", "Agarpara Jute Mill", "Panihati", "Sodepur", "Raja Road", "Sukhchar Girja", "Prafulla", "Khardaha PS", "Khardaha S. Road", "Tata Gate", "Titagarh", "Bara Rasta", "Talpukur", "Chiriamore", "Barackpore Court"]),
    ("C-29", "Barasat Depot", ["Colony More", "Larica Housing Estate / Adamas College", "Jagannath Pur", "Kazibari / University", "Metro Diary", "Cocapur", "Subhash Nagar", "Punjabi Khola", "Rangapur", "M. Hut", "Bank More", "Salap Bagnan", "Matarangi", "CSTC Depot", "M.P.R. Park", "Debpukur", "Barakatlia", "Wireless Gate", "Nona Chandan Pukur", "Jafarpur", "Chalam Pukur", "Subhas Colony", "Lal Kuthi", "Disha Eye Hospital", "Barrackpur Station", "Chiria More", "Administration Building", "Police J. College", "Surendranath College", "Kendraya Vid.", "Doglas Sector", "Dhobi ghat", "Mistery ghat", "Barackpore Court", "Mohanpur"]),
    ("NB-2", "Khidirpur Depot", ["Nabanna", "Mandirtola", "Toll Tax", "Hastings", "P.T.S.", "Rabindra Sadan", "Birla", "Jeevandip", "Park Street", "Mayo Road (ESPL)", "Eastern Hotel", "B. B. D. Bag"]),
    ("NB-1", "Rajabazar Depot", ["Nabanna", "Mandirtola", "Toll Tax", "Hastings", "P.T.S.", "Rabindra Sadan", "Birla", "Jeevandip", "Park Street", "Esplanade", "Chandni Chawk", "Wellington Square", "Taltola", "Moulali", "N.R.S. Hospital", "Sealdah / Raja Bazar Depot"]),
    ("E-7 /1", "Rajabazar Depot", ["Esplanade", "P.T.S.", "Hastings", "Toll Tax", "Carry Road", "Belepole", "Bathor", "Bakshara / Jana Ghati", "Santragachi", "Moukhali", "Garpha", "Kona Truck Terminal", "Kona Express Way", "Ankurhati Check Post", "Swarswati Bridge", "Jalan Gate – 1", "Jalan Gate No. 2", "Islampur", "Dhulaguri", "Nirmal Cinema Hall", "Dhulagiri Toll", "Khapar", "Ranihati", "Panipara", "Dhamisha", "Panchla", "Beltala", "Kalitala", "Malpara", "Jamuria Bridge", "Jora Koltala", "Manasa tola", "Neemdighi", "Uluberia Check Post", "CIPT College", "Birshibpur", "Growth Certer", "Pirtola", "Kulgachi", "Mahesh Rabha Bridge", "Library More", "Bagnan", "Antila Bridge", "Nuntia", "Raidighi", "Noyal", "Mohanpur", "Sasati", "Bardhaman", "Dharmatala", "Belpukur Bazar", "Belpukur College", "Hoglashi", "Gobindapur", "Shyampur"]),
    ("E-7", "Tollygunge Depot", ["Esplanade", "P.T.S.", "Hastings", "Toll Tax", "Carry Road", "Belepole", "Betor", "Bakshara / Jana Ghati", "Santragachi", "Moukhali", "Garpha", "Kona Express Way", "Ankurhati Check Post", "Swarswati Bridge", "Jalan Gate – 1", "Jalan Gate No. 2", "Islampur", "Dhulaguri", "Nirmal Cinema Hall", "Dhulagiri Toll", "Khapar", "Ranihati", "Panipara", "Dhamisha", "Panchla", "Beltala", "Kalitala", "Malpara", "Jamuria Bridge", "Jora Koltala", "Manasa tola", "Neemdighi", "Uluberia Check Post", "CIPT College", "Birshibpur", "Growth Certer", "Pirtola", "Kulgachi", "Mahesh Rabha Bridge", "Library More", "Bagnan"]),
    ("E-12", "Rajabazar Depot", ["Esplanade", "P.T.S.", "Hastings", "Toll Plaza", "Carry Road", "Belepole", "Bathor", "Bakshara / Jana Ghati", "Santragachi", "Moukhali", "Khajurtala", "Lal Bari", "Sallap NHXING", "Katlia", "Makardah", "B.D.O. Office", "Domjur Stand", "Dhakin Bari", "Santoshpur", "2 No.", "Sandhya Bazar", "Bargachia", "Patihal", "Munshirhat", "Basantipur", "Pero", "Khalpar", "Khila", "Joynagar", "Rajapur", "Sinty", "Katgola", "Udaynarayanpur"]),
    ("E-13", "Rajabazar Depot", ["Rabindrasadan/Hasting", "P.T.S.", "Hastings", "V.S. Toll Plaza", "Carry Road", "Belepole", "Bakshara", "Santragachi", "Salap", "Makardah", "Domjur Stand", "Dakshini Bari", "Santoshpur", "Bargachia", "Bargachia Level Crossing", "Jagatballavpur", "Ichnagari", "Sitapur", "Prasadpur", "Mohonbati", "Jangipara"]),
    ("E-13/1", "Rajabazar Depot", ["Rabindrasadan/Hasting", "P.T.S.", "Hastings", "V.S. Toll Plaza", "Carry Road", "Belepole", "Bakshara", "Santragachi", "Salap", "Kona", "Chamrail", "Jagadispur", "Kalipur", "Chanditala", "Kolachara", "Kumirmara", "Jangalpara", "Krisnarampur", "Mashat", "Siakhala", "Ujal", "Furfurasharif", "Dhanpota", "Rashpur", "Bosontopur", "Jangipara"]),
    ("E-17", "Barasat Depot", ["Dakbangla", "Madhyamgram", "Airport", "Ultadanga", "EM2", "Toll Plaza", "Santragachi", "Alampur", "Ranikuthi", "Panchla", "Kulgachi", "Uluberia", "Bagnan", "Kalaghat", "Machada", "Nimtonri", "Nanda Kumar", "Narghat", "Chandipur", "Bajkul", "Henmia", "Kalinagar", "Nachinda", "Contai", "Chaul Khola", "Balisai", "Ramnagar", "Digha"]),
    ("E-16", "Belgachia Depot", ["M.G. Road / C.R. Avenue Xing", "R.G.Kar Hospital", "Birati More", "Hridaypur Stn Road", "Shyampukur", "Bangur Avenue", "Satyanarayan Park", "Nagar Bazar", "Beadon Street", "Bangar Hospital", "Madhyamgram", "Doltala", "Gora Bazar", "Airport", "Belgachia", "B.T. College", "Chitpur", "M.G. Road Fly Over", "Ajanta", "HM", "Dakbangla", "Colony More", "Khilkapur", "Mirhati", "Kamdevpur", "Amdanga", "Awal Sidhi", "Adhata", "Cadamara", "Rajberia", "Barajagulia", "Kalyani More", "Rail gate", "KD AD More", "Hospital", "Spring Dell School", "College", "University", "Municipility", "Kalyani"]),
    ("E-14", "Rajabazar Depot", ["V. House", "Khadi Bhaban", "Bowbazar PS", "B.B. C. St. Xing (Bow bazar)", "Medical College", "M.G. Rd Xing", "Mahajyoti Sadan", "Vivekananda Road", "Beadon Street", "Sovabazar", "Rajballavpur / AV School", "Shyambazar", "Tala Post Office", "Paik Para", "Chiriamore", "Rabindra Bharati", "Sinthee More", "Pal Para", "Tobin Road", "Boonhooghly (ISI)", "Dunlop", "Dakshinwar", "Bally Halt", "Dankuni", "Kalipara", "Chanditala", "Kalachara", "Kumir More", "J. Pur", "Masat", "Siakhola", "F. More", "Furfuira"]),
    ("E-18", "Belgachia Depot", ["EM2", "Wellington", "Moulali", "Sealdah Court", "Beleghata", "Stadium", "Chingrighata", "Science City", "Chowbaga", "Buntia", "BIT", "Bamanghata", "Kantalia", "Kolkata Leather Company", "Bhojerhat", "Paglahut", "Boralighat", "N. Muri", "Ghatak Pukur", "Ghosh Para", "Bhusighata", "Baman Pukur", "Minikha", "Malancha", "Matbari", "Siristone", "Bairmari", "Kumir Mora", "Raj bari", "Baraj Ghera", "Sarbenia", "Rampur", "Haldergheni", "Dhamakhali"]),
    ("E-15", "", ["Rabindra Sadan", "P.T.S.", "Toll Tax", "Buxarah", "Santragachi", "Ankurhati Check Post", "Jalan Gate – 1", "Alampur", "Dhulaguri", "Ranihati", "Panchla", "Khalasari", "Manarntola", "Uluberia", "Uluberia Railway Xing", "Carulata", "Jagadispur", "Kalinagar Chow Matha", "Dhula Simla", "Bagnan", "Ichapur", "Ulughata", "Bus Stand", "Bara Garcha Mukh", "Banamali Pur", "Moula", "Kharuberia", "Shyampur", "Laxmibazar", "Cujarpur", "Cuirepore", "Gadiara"]),
    ("C-7", "Khidirpur Depot", ["H.M.Ghosh College", "Ramnagar", "Hindustan Liver", "Khidirpur", "Esplanade", "Howrah Station"]),
    ("C-8", "Tollygunge Depot", ["Tollygunge Metro", "Rasbehari", "Gariahat", "E.M.Bypass", "Nicco Park", "S.D.F. Building", "Chinar Park", "Kaikhali"]),
    ("T-1", "Rajabazar Depot", ["Rajabazar", "B.B.Ganguly Street", "Lenin Sarani", "Esplanade", "Rabindra Sadan", "Vidyasagar Setu", "Andul Stn Road", "Nh6 xing", "Dhulaguri"]),
    ("T-2", "Khidirpur Depot", ["Mandirtola", "Vidyasagar Setu", "Rabindra Sadan", "Park Street", "Esplanade", "B. B. D. Bag"]),
    ("T-3", "Rajabazar Depot", ["Rajabazar", "Sealdah Court", "Pamar Bazar", "Gobindakhatik Road", "Baishali", "Science City", "Leather Complex", "Bhojerhat", "Ghatak Pukur"]),
    ("T-10", "Rajabazar Depot", ["Rajabazar", "Sealdah Court", "Pamar Bazar", "Gobindakhatik Road", "Science City", "Leather Complex", "Bhojerhat", "Ghatak Pukur", "Ghusighata", "Minakha", "Malancha"]),
    ("T-10/1", "Rajabazar Depot", ["Rajabazar", "Sealdah Court", "Pamar Bazar", "Gobindakhatik Road", "Topsia", "Science City", "Leather Complex", "Bhojerhat", "Ghatak Pukur", "Ghusighata", "Minakha", "Malancha", "Chaital Bridge", "Vebia"]),
    ("E-26", "Ghasbagan Depot", ["Howrah Station", "B. B. D. Bag", "Esplanade", "Moulali", "Park Circus", "Science City", "E.M.Bypass", "Kamalgazi", "Rajpur", "Harinavi", "Malancha", "Padma Pukur", "Baruipur"]),
    ("ORD-2", "Ghasbagan Depot", ["B. B. D. Bag", "Khidirpur", "Mominpur", "Behala Chowrasta", "Sakhar Bazar", "Thakurpukur", "Joka"]),
    ("AC-3", "Barasat Depot", ["Barasat", "Madhyamgram", "Airport", "Ultadanga", "Vivekananda Road"]),
    ("AC-4", "Barasat Depot", ["Barasat", "ECO PARK", "Airport", "City Centre 2", "Madhyamgram", "DLF Bldg I", "Karunamoyee"]),
    ("E-19", "Barasat Depot", ["Habra", "Barasat", "Dakshinwar", "Dankuni", "Dhulagarh", "Ranihati", "Bagnan", "Machada", "Digha"]),
    ("E-20", "Barasat Depot", ["Habra", "Barasat", "Dakshinwar", "Dankuni", "Bardhaman", "Durgapur"]),
]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "stop"


# Unique stop names across every route (case/space-insensitive), each given a stable id.
_seen: Dict[str, str] = {}
STOP_IDS: Dict[str, str] = {}   # original name -> id
STOP_NAMES: Dict[str, str] = {}  # id -> canonical display name
for _num, _depot, _stop_list in ROUTES_RAW:
    for _name in _stop_list:
        key = _name.strip().lower()
        if key not in _seen:
            sid = slugify(_name)
            base = sid
            n = 2
            while sid in STOP_NAMES:
                sid = f"{base}-{n}"
                n += 1
            _seen[key] = sid
            STOP_NAMES[sid] = _name.strip()
        STOP_IDS[_name] = _seen[key]

# Filled in during startup once geocoding completes: id -> (lat, lng)
GEO: Dict[str, tuple] = {}
STOPS: Dict[str, dict] = {}      # populated after geocoding: id -> stop_info dict
LOOPS: Dict[str, dict] = {}
BUSES: Dict[str, dict] = {}
GEOCODE_REPORT = {"resolved": 0, "failed": [], "routes_skipped": []}


def haversine_m(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def build_loop(stop_ids: List[str]):
    """A bus runs out along the route and back again: A B C D -> A B C D C B -> (A)."""
    seq = stop_ids + stop_ids[-2:0:-1] if len(stop_ids) > 1 else stop_ids
    pts = [(STOPS[s]["lat"], STOPS[s]["lng"]) for s in seq]
    cum = [0.0]
    for i in range(len(seq)):
        cum.append(cum[-1] + haversine_m(pts[i], pts[(i + 1) % len(seq)]))
    return {"seq": seq, "pts": pts, "cum": cum, "L": max(cum[-1], 1.0)}


def mps(bus) -> float:
    return bus["speed_kmh"] / 3.6 * DEMO_SPEEDUP


def place(bus):
    loop = LOOPS[bus["loop_key"]]
    seq, pts, cum = loop["seq"], loop["pts"], loop["cum"]
    n = len(seq)
    k = bisect_right(cum, bus["s"])
    i = min(max(k - 1, 0), n - 1)
    a, b = pts[i], pts[(i + 1) % n]
    seg = cum[i + 1] - cum[i]
    f = (bus["s"] - cum[i]) / seg if seg else 0
    bus["lat"] = round(a[0] + (b[0] - a[0]) * f, 6)
    bus["lng"] = round(a[1] + (b[1] - a[1]) * f, 6)
    bus["next_stop"] = STOPS[seq[k % n]]["name"]


def snapshot():
    keys = ("id", "number", "route_name", "status", "crowding", "speed_kmh", "lat", "lng", "next_stop")
    return [{k: b[k] for k in keys} for b in BUSES.values()]


def stop_info(sid: str):
    return STOPS[sid]


def plan_trip(origin: str, dest: str):
    options = []
    for bus in BUSES.values():
        loop = LOOPS[bus["loop_key"]]
        seq, cum, L = loop["seq"], loop["cum"], loop["L"]
        n = len(seq)
        occ_a = [i for i, s in enumerate(seq) if s == origin]
        occ_b = [i for i, s in enumerate(seq) if s == dest]
        if not occ_a or not occ_b:
            continue
        best = None
        for ia in occ_a:
            wait_m = (cum[ia] - bus["s"]) % L
            ride_m, ib = min(((cum[j] - cum[ia]) % L, j) for j in occ_b)
            total = wait_m + ride_m
            if best is None or total < best[0]:
                best = (total, wait_m, ride_m, ia, ib)
        _, wait_m, ride_m, ia, ib = best
        idxs, i = [ia], ia
        while i != ib:
            i = (i + 1) % n
            idxs.append(i)
        speed = mps(bus)
        options.append({
            "bus_id": bus["id"], "number": bus["number"], "route_name": bus["route_name"],
            "status": bus["status"], "crowding": bus["crowding"], "next_stop": bus["next_stop"],
            "eta_min": round(wait_m / speed / 60, 1), "ride_min": round(ride_m / speed / 60, 1),
            "total_min": round((wait_m + ride_m) / speed / 60, 1), "stops": len(idxs) - 1,
            "segment": [stop_info(seq[j]) for j in idxs],
        })
    options.sort(key=lambda o: o["total_min"])
    return options


def transfer_hints(origin: str, dest: str):
    routes_by_stop: Dict[str, set] = {}
    for loop_key, loop in LOOPS.items():
        for sid in set(loop["seq"]):
            routes_by_stop.setdefault(sid, set()).add(loop_key.split("#")[0])
    hints = []
    for x in STOPS:
        if x in (origin, dest):
            continue
        first = routes_by_stop.get(origin, set()) & routes_by_stop.get(x, set())
        second = routes_by_stop.get(x, set()) & routes_by_stop.get(dest, set())
        if first and second:
            hints.append({"via": STOPS[x]["name"], "first": sorted(first), "second": sorted(second)})
    hints.sort(key=lambda h: -(len(h["first"]) + len(h["second"])))
    return hints[:3]


def build_network():
    """(Re)build STOPS/LOOPS/BUSES from whatever is currently in GEO. Call after geocoding."""
    STOPS.clear()
    LOOPS.clear()
    BUSES.clear()
    GEOCODE_REPORT["routes_skipped"] = []
    for sid, name in STOP_NAMES.items():
        if sid in GEO:
            lat, lng = GEO[sid]
            STOPS[sid] = {"id": sid, "name": name, "lat": lat, "lng": lng}

    for idx, (number, depot, stop_names) in enumerate(ROUTES_RAW):
        ids, seen_ids = [], set()
        for name in stop_names:
            sid = STOP_IDS[name]
            if sid in STOPS and sid not in seen_ids:  # drop ungeocoded + consecutive dupes
                ids.append(sid)
                seen_ids.add(sid)
        if len(ids) < 2:
            GEOCODE_REPORT["routes_skipped"].append(number)
            continue
        loop_key = f"{number}#{idx}"
        loop = LOOPS[loop_key] = build_loop(ids)
        first_name, last_name = STOPS[ids[0]]["name"], STOPS[ids[-1]]["name"]
        km = loop["L"] / 1000
        # No official per-route speed data is published; vary the demo speed a little
        # by route length so long-haul routes look faster than short shuttle routes.
        speed = max(14, min(32, DEFAULT_KMH + (km - 20) * 0.15))
        for k in (1, 2):
            bus_id = f"{loop_key}-{k}"
            BUSES[bus_id] = {
                "id": bus_id, "loop_key": loop_key, "number": number,
                "route_name": f"{first_name} → {last_name}",
                "status": "ON_TIME" if (idx + k) % 3 else "DELAYED",
                "crowding": ["Low", "Moderate", "High"][(idx + k) % 3],
                "speed_kmh": round(speed, 1),
                "s": (k - 1) * loop["L"] / 2, "lat": 0.0, "lng": 0.0, "next_stop": "",
            }
    for b in BUSES.values():
        place(b)


# ------------------------------------------------------------------ geocoding
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                salt BLOB NOT NULL,
                pw_hash BLOB NOT NULL,
                created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS password_resets(
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS geocode_cache(
                name TEXT PRIMARY KEY,
                lat REAL, lng REAL,
                status TEXT NOT NULL,
                updated_at REAL NOT NULL);
        """)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(users)")]
        if "last_login" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN last_login REAL")


def load_geocode_cache():
    if SEED_PATH.exists():
        try:
            for sid, (lat, lng) in json.loads(SEED_PATH.read_text(encoding="utf-8")).items():
                if sid in STOP_NAMES:
                    GEO[sid] = (lat, lng)
            print(f"[bustrack] loaded {len(GEO)} stop coordinates from {SEED_PATH.name}")
        except Exception as e:  # a bad seed file should never stop the app starting
            print(f"[bustrack] could not read {SEED_PATH.name}: {e}")
    with db() as con:
        for row in con.execute("SELECT * FROM geocode_cache"):
            if row["status"] == "ok":
                GEO[row["name"]] = (row["lat"], row["lng"])


async def geocode_all():
    """Geocode every stop id not already cached, via the Google Geocoding API."""
    to_fetch = [sid for sid in STOP_NAMES if sid not in GEO]
    if not to_fetch:
        return
    if not GOOGLE_MAPS_API_KEY:
        print("[bustrack] GOOGLE_MAPS_API_KEY is not set (check your .env file) - "
              "stops cannot be geocoded, the map will be empty.")
        return

    import httpx  # imported lazily so the rest of the app works even if unused in tests

    sem = asyncio.Semaphore(GEOCODE_CONCURRENCY)
    results = {}

    async def fetch_one(client: "httpx.AsyncClient", sid: str):
        name = STOP_NAME_OVERRIDES.get(STOP_NAMES[sid], STOP_NAMES[sid])
        query = f"{name}, Kolkata, West Bengal, India"
        params = {"address": query, "key": GOOGLE_MAPS_API_KEY, "bounds": KOLKATA_BOUNDS, "region": "in"}
        async with sem:
            try:
                resp = await client.get("https://maps.googleapis.com/maps/api/geocode/json",
                                         params=params, timeout=GEOCODE_TIMEOUT)
                data = resp.json()
            except Exception as e:
                results[sid] = ("error", None, None, str(e))
                return
        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            results[sid] = ("ok", loc["lat"], loc["lng"], None)
        else:
            results[sid] = (data.get("status", "UNKNOWN"), None, None, data.get("error_message"))

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(fetch_one(client, sid) for sid in to_fetch))

    now = time.time()
    with db() as con:
        for sid, (status, lat, lng, err) in results.items():
            con.execute(
                "INSERT INTO geocode_cache(name, lat, lng, status, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, "
                "status=excluded.status, updated_at=excluded.updated_at",
                (sid, lat, lng, status, now))
            if status == "ok":
                GEO[sid] = (lat, lng)
                GEOCODE_REPORT["resolved"] += 1
            else:
                GEOCODE_REPORT["failed"].append({"stop": STOP_NAMES[sid], "reason": status})

    ok = sum(1 for r in results.values() if r[0] == "ok")
    print(f"[bustrack] geocoded {ok}/{len(to_fetch)} new stops "
          f"({len(STOP_NAMES) - len(GEO)} still unresolved out of {len(STOP_NAMES)} total)")


async def regeocode_failed():
    with db() as con:
        con.execute("DELETE FROM geocode_cache WHERE status != 'ok'")
    GEOCODE_REPORT["failed"] = []
    GEOCODE_REPORT["resolved"] = 0
    await geocode_all()
    build_network()


# ------------------------------------------------------------------- auth db
def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def hash_pw(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def new_session(con, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    con.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    con.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?,?,?)",
                (sha(token), user_id, time.time() + SESSION_DAYS * 86400))
    return token


def user_from_token(token: Optional[str]):
    if not token:
        return None
    with db() as con:
        row = con.execute(
            "SELECT u.id, u.name, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ?", (sha(token), time.time())).fetchone()
    return dict(row) if row else None


def bearer(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def current_user(authorization: Optional[str] = Header(None)):
    user = user_from_token(bearer(authorization))
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FAILS: Dict[str, List[float]] = {}


def locked_out(key: str) -> bool:
    now = time.time()
    FAILS[key] = [t for t in FAILS.get(key, []) if now - t < 300]
    return len(FAILS[key]) >= 5


# ------------------------------------------------------------ password reset
RESET_REQUESTS: Dict[str, List[float]] = {}
_bg_tasks: set = set()


def reset_rate_limited(email: str) -> bool:
    now = time.time()
    recent = [t for t in RESET_REQUESTS.get(email, []) if now - t < RESET_WINDOW_SECONDS]
    RESET_REQUESTS[email] = recent
    if len(recent) >= RESET_MAX_REQUESTS:
        return True
    recent.append(now)
    return False


def send_reset_email(to_email: str, name: str, link: str):
    """Send the reset link by SMTP. With no SMTP configured (local development),
    print the link in the server console instead so you can still test the flow."""
    if not SMTP_HOST:
        print(f"\n[bustrack] Password reset link for {to_email} (SMTP not configured):\n  {link}\n")
        return
    msg = EmailMessage()
    msg["Subject"] = "Reset your BusTrack password"
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(
        f"Hi {name},\n\n"
        f"We received a request to reset your BusTrack password. Use this link within "
        f"{RESET_TOKEN_MINUTES} minutes:\n\n{link}\n\n"
        f"If you didn't ask for this, you can ignore this email; your password won't change.\n")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        print(f"[bustrack] Could not send reset email to {to_email}: {e}")


def spawn(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ------------------------------------------------------------------ websocket
class ConnectionManager:
    def __init__(self):
        self.connections: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


async def simulate():
    while True:
        for bus in BUSES.values():
            loop = LOOPS[bus["loop_key"]]
            bus["s"] = (bus["s"] + mps(bus) * TICK_SECONDS) % loop["L"]
            place(bus)
        if manager.connections and BUSES:
            await manager.broadcast({"type": "positions", "buses": snapshot()})
        await asyncio.sleep(TICK_SECONDS)


def ensure_demo_user():
    """If DEMO_EMAIL / DEMO_PASSWORD are set, make sure that account exists.
    Useful on hosts with a temporary disk, where accounts vanish whenever the app restarts."""
    email = os.getenv("DEMO_EMAIL", "").strip().lower()
    pw = os.getenv("DEMO_PASSWORD", "")
    if not email and not pw:
        return
    if not EMAIL_RE.match(email) or not 8 <= len(pw) <= 128:
        print("[bustrack] DEMO_EMAIL / DEMO_PASSWORD ignored: need a valid email and an 8-128 character password")
        return
    with db() as con:
        if con.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return
        salt = secrets.token_bytes(16)
        con.execute("INSERT INTO users(name, email, salt, pw_hash, created_at, last_login) VALUES (?,?,?,?,?,?)",
                    ("Demo User", email, salt, hash_pw(pw, salt), time.time(), time.time()))
    print("[bustrack] demo account ready")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_demo_user()
    load_geocode_cache()
    if os.getenv("GEOCODE_ON_STARTUP", "1") != "0":   # set to 0 on hosted deployments
        await geocode_all()
    build_network()
    print(f"[bustrack] network ready: {len(STOPS)}/{len(STOP_NAMES)} stops geocoded, "
          f"{len(LOOPS)}/{len(ROUTES_RAW)} routes usable, {len(BUSES)} buses")
    app.state.sim = asyncio.create_task(simulate())
    yield
    app.state.sim.cancel()


app = FastAPI(title="BusTrack", lifespan=lifespan)


# ----------------------------------------------------------------------- API
class RegisterIn(BaseModel):
    name: str
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class ForgotIn(BaseModel):
    email: str


class ResetIn(BaseModel):
    token: str
    password: str


class PlanIn(BaseModel):
    origin: str
    destination: str


@app.post("/api/register")
def register(body: RegisterIn):
    name, email = body.name.strip(), body.email.strip().lower()
    if not name or len(name) > 60:
        raise HTTPException(400, "Please enter your name (up to 60 characters)")
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Please enter a valid email address")
    if not 8 <= len(body.password) <= 128:
        raise HTTPException(400, "Password must be 8 to 128 characters")
    salt = secrets.token_bytes(16)
    with db() as con:
        try:
            cur = con.execute("INSERT INTO users(name, email, salt, pw_hash, created_at, last_login) VALUES (?,?,?,?,?,?)",
                              (name, email, salt, hash_pw(body.password, salt), time.time(), time.time()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "An account with this email already exists")
        token = new_session(con, cur.lastrowid)
    return {"token": token, "user": {"name": name, "email": email}}


@app.post("/api/login")
def login(body: LoginIn):
    email = body.email.strip().lower()
    if locked_out(email):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        candidate = hash_pw(body.password, row["salt"] if row else b"\0" * 16)
        if not row or not hmac.compare_digest(candidate, row["pw_hash"]):
            FAILS.setdefault(email, []).append(time.time())
            raise HTTPException(401, "Incorrect email or password")
        FAILS.pop(email, None)
        con.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), row["id"]))
        token = new_session(con, row["id"])
    return {"token": token, "user": {"name": row["name"], "email": row["email"]}}


@app.post("/api/forgot-password")
async def forgot_password(body: ForgotIn):
    """Always answers the same way, so nobody can use this to find out who has an account."""
    generic = {"message": "If an account exists for that email, we've sent a link to reset the password."}
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email) or reset_rate_limited(email):
        return generic
    with db() as con:
        row = con.execute("SELECT id, name FROM users WHERE email = ?", (email,)).fetchone()
        if not row:
            return generic
        token = secrets.token_urlsafe(32)
        con.execute("DELETE FROM password_resets WHERE user_id = ? OR expires_at < ?", (row["id"], time.time()))
        con.execute("INSERT INTO password_resets(token_hash, user_id, expires_at) VALUES (?,?,?)",
                    (sha(token), row["id"], time.time() + RESET_TOKEN_MINUTES * 60))
    # Send in the background so response time doesn't reveal whether the email exists.
    spawn(asyncio.to_thread(send_reset_email, email, row["name"], f"{APP_BASE_URL}/?reset={token}"))
    return generic


@app.post("/api/reset-password")
def reset_password(body: ResetIn):
    if not 8 <= len(body.password) <= 128:
        raise HTTPException(400, "Password must be 8 to 128 characters")
    th = sha(body.token.strip())
    with db() as con:
        row = con.execute("SELECT user_id FROM password_resets WHERE token_hash = ? AND expires_at > ?",
                          (th, time.time())).fetchone()
        if not row:
            raise HTTPException(400, "This reset link is invalid or has expired. Please request a new one.")
        salt = secrets.token_bytes(16)
        con.execute("UPDATE users SET salt = ?, pw_hash = ? WHERE id = ?",
                    (salt, hash_pw(body.password, salt), row["user_id"]))
        con.execute("DELETE FROM password_resets WHERE user_id = ?", (row["user_id"],))
        con.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))  # sign out everywhere
        email = con.execute("SELECT email FROM users WHERE id = ?", (row["user_id"],)).fetchone()["email"]
    FAILS.pop(email, None)
    return {"success": True}


@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None)):
    token = bearer(authorization)
    if token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (sha(token),))
    return {"success": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"name": user["name"], "email": user["email"]}


@app.get("/api/stops")
def get_stops(user: dict = Depends(current_user)):
    return list(STOPS.values())


@app.get("/api/buses")
def get_buses(user: dict = Depends(current_user)):
    return snapshot()


@app.post("/api/plan")
def plan(body: PlanIn, user: dict = Depends(current_user)):
    if body.origin not in STOPS or body.destination not in STOPS:
        raise HTTPException(400, "Unknown stop")
    if body.origin == body.destination:
        raise HTTPException(400, "Starting point and destination must be different")
    options = plan_trip(body.origin, body.destination)
    return {
        "origin": stop_info(body.origin), "destination": stop_info(body.destination),
        "options": options,
        "transfers": [] if options else transfer_hints(body.origin, body.destination),
    }


@app.get("/api/geocode-status")
def geocode_status(user: dict = Depends(current_user)):
    """How many real stops resolved to coordinates, and which ones didn't."""
    return {
        "total_stops": len(STOP_NAMES), "resolved_stops": len(STOPS),
        "total_routes": len(ROUTES_RAW), "usable_routes": len(LOOPS),
        "routes_skipped": GEOCODE_REPORT["routes_skipped"],
        "failed_stops": GEOCODE_REPORT["failed"],
    }


@app.post("/api/geocode-retry")
async def geocode_retry(user: dict = Depends(current_user)):
    """Retry geocoding for any stop that failed last time (e.g. after fixing an override)."""
    await regeocode_failed()
    return await geocode_status(user)  # type: ignore[misc]


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: Optional[str] = Query(None)):
    await ws.accept()
    if not await asyncio.to_thread(user_from_token, token):
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "positions", "buses": snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/config.js")
def config_js():
    """Hands the browser its Maps key from the environment, so the key never sits in the code/git.
    Use a separate, referrer-restricted key here (GOOGLE_MAPS_BROWSER_KEY); falls back to the
    server key so single-key local setups keep working."""
    key = os.getenv("GOOGLE_MAPS_BROWSER_KEY") or GOOGLE_MAPS_API_KEY
    map_id = os.getenv("GOOGLE_MAPS_MAP_ID", "")  # optional: turns on Google's smoother vector map renderer
    return Response(
        f"window.__BUSTRACK_MAPS_KEY = {json.dumps(key)};\n"
        f"window.__BUSTRACK_MAP_ID = {json.dumps(map_id)};\n",
        media_type="application/javascript", headers={"Cache-Control": "no-store"})


# Must come last: serves index.html, app.js, style.css at "/"
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
