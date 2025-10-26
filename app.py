from flask import Flask, request, render_template, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import os, json, requests
from io import BytesIO
import gspread
from google.oauth2 import service_account

# File parsing
import PyPDF2
import docx
try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None

# Gemini client
from google import genai
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# ===== Load Environment Variables =====
load_dotenv()
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP = os.getenv("TWILIO_WHATSAPP_NUMBER")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


# Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Google Sheets using credentials file
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
credentials = service_account.Credentials.from_service_account_file(
    "/etc/secrets/google-key.json", scopes=SCOPES
)
gc = gspread.authorize(credentials)
sheet = gc.open("candidatedata").sheet1

# Twilio Client
client = Client(ACCOUNT_SID, AUTH_TOKEN)

# Flask App
app = Flask(__name__)
received_messages = []  # store messages in memory for UI

# ===== Helper Functions =====
def extract_text_from_file(media_url, media_type):
    try:
        response = requests.get(media_url, auth=HTTPBasicAuth(ACCOUNT_SID, AUTH_TOKEN))
        if 'pdf' in media_type.lower():
            pdf_reader = PyPDF2.PdfReader(BytesIO(response.content))
            text = ""
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            return text
        elif 'word' in media_type.lower():
            doc = docx.Document(BytesIO(response.content))
            return "\n".join([p.text for p in doc.paragraphs])
        elif media_type.startswith("image/") and pytesseract:
            img = Image.open(BytesIO(response.content))
            return pytesseract.image_to_string(img)
        else:
            return ""
    except Exception as e:
        print("❌ File parse error:", e)
        return ""

def extract_details(text):
    prompt = (
        "Extract all people’s details (Name, Email, Phone) from this text.\n"
        "Return strictly as a JSON array like:\n"
        "[{\"Name\":\"...\", \"Email\":\"...\", \"Phone\":\"...\"}]\n\n"
        f"Text:\n{text}"
    )
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        data = response.text.strip()
        try:
            data = data.replace("'", '"')
            start = data.find("[")
            end = data.rfind("]") + 1
            details_list = json.loads(data[start:end])
        except:
            details_list = [{"Name": "", "Email": "", "Phone": ""}]
        
        cleaned = []
        for d in details_list:
            name = d.get("Name","").strip()
            email = d.get("Email","").strip()
            phone = d.get("Phone","").strip()
            cleaned.append({"Name": name, "Email": email, "Phone": phone})
        return cleaned
    except Exception as e:
        print("Gemini parsing error:", e)
        return [{"Name": "", "Email": "", "Phone": ""}]

# ===== Flask Routes =====
@app.route("/incoming", methods=["POST"])
def incoming_message():
    from_number = request.form.get("From")
    message_body = request.form.get("Body") or ""
    num_media = int(request.form.get("NumMedia", 0))

    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        media_type = request.form.get("MediaContentType0")
        message_body = extract_text_from_file(media_url, media_type)

    people = extract_details(message_body)

    for person in people:
        name = person.get("Name","")
        email = person.get("Email","")
        phone = person.get("Phone","")
        if not phone:
            phone = ""
        if name or email or phone:
            try:
                sheet.append_row([name,email,phone])
            except Exception as e:
                print("Sheet append error:", e)
            received_messages.append({"Name":name, "Email":email, "Phone":phone})

    resp = MessagingResponse()
    resp.message("✅ Your data has been received and stored successfully.")
    return str(resp)

# ===== SSE for dynamic UI =====
@app.route("/stream")
def stream():
    def event_stream():
        last_len = 0
        while True:
            if len(received_messages) > last_len:
                new_data = received_messages[last_len:]
                last_len = len(received_messages)
                yield f"data: {json.dumps(new_data)}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/")
def index():
    return render_template("index.html")

# ===== Run Flask =====
if __name__ == "__main__":
    app.run(port=5000, debug=True)

